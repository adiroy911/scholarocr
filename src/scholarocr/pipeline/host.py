"""Host mode: the MCP client's own model does the vision work, the server does everything else.

For a page that needs visual OCR the pipeline does all deterministic work (render, document-region
crop, quality assessment, deskew/rotation/upscale/denoise) and then, instead of calling a model,
stores a ``HostTask`` next to the page and marks the page ``awaiting_transcription``. The client's model
fetches the processed page image (``get_page_image``), transcribes it into the same response schema the
key providers use (``PageExtractionResponse``: elements with ``box_2d`` in 0-1000 of that image), and
submits it (``submit_page_content``). The submission is validated strictly here; a rejected submission
returns every problem with its JSON path and a fix, and nothing is stored. An accepted one is converted
with the same code as provider output (box mapping through the inverse preprocessing transform,
deterministic uncertainty rules, structure annotation, crops), so read/verify/export work unchanged.
"""

from __future__ import annotations

import difflib
import io
import json
from typing import Any

from PIL import Image
from pydantic import BaseModel, Field, ValidationError

from ..exceptions import SubmissionRejected
from ..intelligence.mathematics import chemistry_issues, latex_issues
from ..intelligence.responses import (PageExtractionResponse, ReviewResponse, XCell, XElement, XFigure, XQuestion,
                                      XTable)
from ..schemas import Route
from ..storage import ArtifactStore
from ..utils import now_iso

HOST_LABEL = "host"
HOST_METHOD = "host_model"
MAX_ELEMENTS = 400
MAX_TEXT_LAYER_CHARS = 2500
MAX_ROTATIONS = 2

TEXT_TYPES = {"title", "heading", "subheading", "paragraph", "text", "handwriting", "caption", "footnote", "header",
              "footer", "page_number", "code", "bibliography_entry", "reference", "uncertain"}
MATH_TYPES = {"equation", "chemical_equation"}
VISUAL_TYPES = {"figure", "diagram", "image"}

# Field names models commonly use instead of ours -> the field they meant.
ALIASES = {
    "bbox": "box_2d", "box": "box_2d", "bounding_box": "box_2d", "boundingbox": "box_2d", "box2d": "box_2d",
    "coordinates": "box_2d", "content": "text", "transcription": "text", "value": "text", "kind": "type",
    "element_type": "type", "confidence": "uncertainty", "certainty": "uncertainty", "uncertain": "uncertainty",
    "reasons": "uncertainty_reasons", "lang": "language", "children": "items", "list_items": "items",
    "caption_for": "caption_of", "parent": "parent_index", "header": "is_header", "rowspan": "row_span",
    "colspan": "col_span", "cells": "rows", "headers": "header_rows",
}


class HostTask(BaseModel):
    """Everything needed to turn a host submission into IR later (``pages/NNNN/host_task.json``)."""

    route: Route
    reason: str
    dpi: float
    original_size: tuple[int, int]
    processed_size: tuple[int, int]
    matrix: list[list[float]]  # original px -> processed px
    crop_box: tuple[int, int, int, int] | None = None
    text_layer: str | None = None
    hints: dict[str, Any] = Field(default_factory=dict)  # document_type, language_hint, context_hint
    review: bool = True
    detail: str = "standard"
    rotations: int = 0
    created_at: str = Field(default_factory=now_iso)


def task_path(store: ArtifactStore, document_id: str, page_number: int):
    return store.page_dir(document_id, page_number) / "host_task.json"


def save_task(store: ArtifactStore, document_id: str, page_number: int, task: HostTask) -> None:
    store.write_json(task_path(store, document_id, page_number), task)


def load_task(store: ArtifactStore, document_id: str, page_number: int) -> HostTask | None:
    p = task_path(store, document_id, page_number)
    if not p.exists():
        return None
    return HostTask.model_validate_json(p.read_text(encoding="utf-8"))


def encode_for_host(img: Image.Image, max_side: int, max_bytes: int = 1_500_000) -> tuple[bytes, str, tuple[int, int]]:
    """PNG (or JPEG when PNG is too large) scaled so the longest side is at most ``max_side``.
    Boxes are normalised, so scaling never affects coordinates."""
    img = img.copy()
    img.thumbnail((max_side, max_side))
    rgb = img.convert("RGB")
    buf = io.BytesIO()
    rgb.save(buf, format="PNG", optimize=True)
    if buf.tell() <= max_bytes:
        return buf.getvalue(), "image/png", rgb.size
    grey = rgb.convert("L")
    buf = io.BytesIO()
    grey.save(buf, format="PNG", optimize=True)
    if buf.tell() <= max_bytes:
        return buf.getvalue(), "image/png", rgb.size
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=88)
    return buf.getvalue(), "image/jpeg", rgb.size


# ---------------------------------------------------------------------------------------------- validation
def _known(model: type[BaseModel]) -> set[str]:
    return set(model.model_fields)


KNOWN = {"element": _known(XElement), "table": _known(XTable), "cell": _known(XCell), "figure": _known(XFigure),
         "question": _known(XQuestion), "page": _known(PageExtractionResponse) | {"legibility"}}
# Element doubt labels: "none" is accepted for "low" (no doubt), and "doubt" as the field name.
DOUBT_SYNONYMS = {"none": "low", "no": "low", "clear": "low", "certain": "low"}


def _suggest(key: str, known: set[str]) -> str | None:
    alias = ALIASES.get(key.lower())
    if alias and alias in known:
        return alias
    close = difflib.get_close_matches(key, sorted(known), n=1, cutoff=0.75)
    return close[0] if close else None


def _short(v: Any, n: int = 80) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str) if not isinstance(v, str) else repr(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def _path(loc: tuple) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "(root)"


class _Problems:
    def __init__(self):
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def add(self, path: str, problem: str, fix: str | None = None) -> None:
        if len(self.errors) < 60:
            self.errors.append({"path": path, "problem": problem, **({"fix": fix} if fix else {})})


def _check_unknown(obj: Any, kind: str, path: str, probs: _Problems) -> None:
    if not isinstance(obj, dict):
        return
    known = KNOWN[kind]
    for k in obj:
        if k not in known:
            hint = _suggest(str(k), known)
            probs.add(f"{path}.{k}", "unknown field", f"use {hint!r}" if hint else f"allowed fields: {sorted(known)}")
    if kind == "element":
        if isinstance(obj.get("table"), dict):
            _check_unknown(obj["table"], "table", f"{path}.table", probs)
            rows = obj["table"].get("rows")
            if isinstance(rows, list):
                for ri, row in enumerate(rows):
                    if isinstance(row, list):
                        for ci, cell in enumerate(row):
                            _check_unknown(cell, "cell", f"{path}.table.rows[{ri}][{ci}]", probs)
        for sub in ("figure", "question"):
            if isinstance(obj.get(sub), dict):
                _check_unknown(obj[sub], sub, f"{path}.{sub}", probs)


def _normalise(payload: dict[str, Any]) -> dict[str, Any]:
    """Lenient, lossless fixes: numeric strings in box_2d, and one cell row passed as a flat list."""
    for el in payload.get("elements") or []:
        if not isinstance(el, dict):
            continue
        if "doubt" in el and "uncertainty" not in el:
            el["uncertainty"] = el.pop("doubt")
        if isinstance(el.get("uncertainty"), str):
            label = el["uncertainty"].strip().lower()
            el["uncertainty"] = DOUBT_SYNONYMS.get(label, label)
        box = el.get("box_2d")
        if isinstance(box, list) and all(isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().lstrip("-").replace(".", "", 1).isdigit())
                                         for v in box):
            el["box_2d"] = [int(round(float(v))) for v in box]
        tbl = el.get("table")
        if isinstance(tbl, dict) and isinstance(tbl.get("rows"), list):
            tbl["rows"] = [[{"text": c} if isinstance(c, str) else c for c in row] if isinstance(row, list) else row
                           for row in tbl["rows"]]
    return payload


def validate_submission(payload: dict[str, Any]) -> tuple[PageExtractionResponse, list[str]]:
    """Validate a host submission strictly. Returns (response, warnings) or raises ``SubmissionRejected``
    with every problem found (path, problem, fix). Nothing is partially accepted."""
    probs = _Problems()
    if isinstance(payload.get("elements"), str):  # some clients send the array as a JSON string
        try:
            parsed = json.loads(payload["elements"])
        except ValueError as e:
            probs.add("elements", f"a string that is not valid JSON ({e.msg} at character {e.pos})",
                      "pass elements as a JSON array of element objects, not a string")
            _raise(probs)
        payload = {**payload, "elements": parsed}
    if not isinstance(payload.get("elements"), list):
        probs.add("elements", f"must be a list of element objects, got {_short(payload.get('elements'))}",
                  "pass elements=[{\"type\": \"paragraph\", \"text\": \"...\"}, ...]")
        _raise(probs)
    payload = _normalise(payload)
    if payload.get("legibility") is not None:
        probs.warnings.append(f"'legibility' is the old name of page_doubt, a level of DOUBT (low = clean page); "
                              f"stored as page_doubt={payload['legibility']!r}. It does not change element labels.")
    elements = payload["elements"]
    if len(elements) > MAX_ELEMENTS:
        probs.add("elements", f"{len(elements)} elements (max {MAX_ELEMENTS})", "merge consecutive lines of one paragraph")
    _check_unknown(payload, "page", "(root)", probs)
    for i, el in enumerate(elements):
        if not isinstance(el, dict):
            probs.add(f"elements[{i}]", f"must be an object, got {_short(el)}",
                      'each element is an object such as {"type": "paragraph", "text": "...", "box_2d": [...]}; '
                      "see get_page_image(instructions='full')")
            continue
        _check_unknown(el, "element", f"elements[{i}]", probs)
    if any(not isinstance(el, dict) for el in elements):
        _raise(probs)  # pydantic would only repeat "valid dictionary" for each of them
    try:
        resp = PageExtractionResponse.model_validate(payload)
    except ValidationError as e:
        for err in e.errors(include_url=False):
            loc = tuple(err.get("loc") or ())
            fix = None
            if err.get("type") == "literal_error":
                fix = err.get("msg", "").replace("Input should be ", "use one of: ")
            probs.add(_path(loc), f"{err.get('msg')} (got {_short(err.get('input'))})", fix)
        _raise(probs)
    # Unknown fields are reported together with the semantic problems below, so one round of fixes suffices.

    n = len(resp.elements)
    for i, x in enumerate(resp.elements):
        p = f"elements[{i}]"
        if x.box_2d is not None:
            b = x.box_2d
            if len(b) != 4:
                probs.add(f"{p}.box_2d", f"needs 4 numbers, got {len(b)}", "use [ymin, xmin, ymax, xmax] in 0-1000")
            elif not all(0 <= v <= 1000 for v in b):
                probs.add(f"{p}.box_2d", f"values must be within 0-1000, got {b}",
                          "normalise to the image: value = pixel / image_size * 1000")
            elif not (b[0] < b[2] and b[1] < b[3]):
                probs.add(f"{p}.box_2d", f"needs ymin < ymax and xmin < xmax, got {b}",
                          "order is [ymin, xmin, ymax, xmax] (y first)")
        for name in ("caption_of", "parent_index"):
            ref = getattr(x, name)
            if ref is not None and not (0 <= ref < n and ref != i):
                probs.add(f"{p}.{name}", f"{ref} is not the index of another element (0-{n - 1})",
                          "use the 0-based position of the target element in this elements list")
        if x.type in TEXT_TYPES and not x.text.strip() and x.type != "page_number":
            probs.add(f"{p}.text", f"empty text for a {x.type} element",
                      "transcribe the text, or use type 'uncertain' with text describing what is unreadable")
        if x.type == "table":
            if x.table is None or not x.table.rows:
                probs.add(f"{p}.table", "table element without rows",
                          "give table={rows:[[{text:...}, ...], ...], header_rows:n}, or use another type")
        if x.type == "list" and not (x.items or x.text.strip()):
            probs.add(f"{p}.items", "list element without items or text", "give items=[...] (one string per item)")
        if x.type in VISUAL_TYPES and (x.figure is None or not x.figure.description.strip()):
            probs.warnings.append(f"{p}: {x.type} without figure.description (kept; described later or not at all)")
        if x.type in MATH_TYPES:
            issues = (latex_issues(x.latex) if x.type == "equation" else chemistry_issues(x.latex, x.text))
            syntax = [s for s in issues if s.startswith(("unbalanced", "\\left", "\\end", "unclosed", "stray", "\\frac",
                                                          "dangling", "empty"))]
            if syntax and x.uncertainty in ("low", "medium"):
                probs.add(f"{p}.latex", "LaTeX syntax problem: " + "; ".join(syntax),
                          "fix the LaTeX; if the source itself is ambiguous, keep it and set uncertainty "
                          "to 'high' (or use type 'uncertain')")
            elif issues:
                probs.warnings.append(f"{p}: {'; '.join(issues)} (uncertainty raised)")
            if x.type == "equation" and not x.latex:
                probs.warnings.append(f"{p}: equation without latex (uncertainty raised to medium)")
    if resp.rotation_detected not in (0, 90, 180, 270):
        probs.add("rotation_detected", f"must be 0, 90, 180 or 270, got {resp.rotation_detected}")
    if not resp.elements:
        probs.warnings.append("no elements submitted: the page is stored as having no content")
    if probs.errors:
        _raise(probs)
    return resp, probs.warnings


def _raise(probs: _Problems) -> None:
    raise SubmissionRejected(
        f"submission rejected: {len(probs.errors)} problem(s); nothing was stored. Fix them and call "
        "submit_page_content again with the complete page.",
        details={"errors": probs.errors, "warnings": probs.warnings[:20],
                 "format": "box_2d=[ymin,xmin,ymax,xmax] integers 0-1000 of the page image; see get_page_image"
                           "(instructions='full') for the element shape"})


class HostReview(BaseModel):
    """What ``submit_review`` accepts: a ReviewResponse plus an optional figure description."""

    verdict: str
    text: str = ""
    latex: str | None = None
    table: XTable | None = None
    figure: XFigure | None = None
    uncertainty: str = "low"
    notes: list[str] = Field(default_factory=list)


def validate_review(payload: dict[str, Any]) -> tuple[ReviewResponse, XFigure | None]:
    probs = _Problems()
    allowed = {"verdict", "text", "latex", "table", "figure", "uncertainty", "notes"}
    for k in payload:
        if k not in allowed:
            hint = _suggest(k, allowed)
            probs.add(k, "unknown field", f"use {hint!r}" if hint else f"allowed: {sorted(allowed)}")
    try:
        hr = HostReview.model_validate(payload)
        resp = ReviewResponse.model_validate({k: v for k, v in payload.items() if k in ReviewResponse.model_fields})
    except ValidationError as e:
        for err in e.errors(include_url=False):
            probs.add(_path(tuple(err.get("loc") or ())), f"{err.get('msg')} (got {_short(err.get('input'))})")
        raise SubmissionRejected(f"review rejected: {len(probs.errors)} problem(s)", details={"errors": probs.errors}) from None
    if probs.errors:
        raise SubmissionRejected(f"review rejected: {len(probs.errors)} problem(s)", details={"errors": probs.errors})
    if resp.verdict == "corrected" and not (resp.text or resp.latex or resp.table or hr.figure):
        raise SubmissionRejected("review rejected: verdict 'corrected' needs the corrected text, latex or table",
                                 details={"errors": [{"path": "text", "problem": "missing correction"}]})
    return resp, hr.figure
