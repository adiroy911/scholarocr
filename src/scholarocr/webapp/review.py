"""The app's review pass: doubtful elements of an accepted page get a second look through ``agy -p``.

Selection is ``reviewer.select_for_review`` (as ``submit_page_content`` reports it in ``review_suggested``). All
elements selected on a page go into one agy call: a folder with one crop per element (the same crop the source
panel shows) is the only readable path, and the reply is one verdict per crop. One call instead of one per
element because an agy call costs about 8k input tokens per model turn whatever it reads (docs/WEB_APP.md).

Every verdict goes through ``ScholarOCR.submit_review``, so the labels follow ``uncertainty.after_review``.
This module adds what the app needs around it:

* the model must have opened a crop (seen in agy's event stream) for its verdict to count;
* a correction that fails the checks in ``correction_problems``, or a confirmation the model itself doubts, changes
  nothing and is kept as a ``review-rejected:`` revision;
* the element as it was is stored in a ``before_review`` revision, so a change can be shown and undone;
* a failure of any kind leaves the page accepted and records a page note instead of raising.
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError

from ..exceptions import InputError, ScholarOCRError, SubmissionRejected
from ..intelligence.clean_text import rule_clean
from ..intelligence.extractor import _table
from ..intelligence.mathematics import chemistry_issues, latex_issues
from ..intelligence.responses import UncertaintyLabel, XTable
from ..intelligence.schema_utils import to_gemini_schema
from ..intelligence.tables import table_issues
from ..logging_config import get_logger
from ..pipeline import host
from ..pipeline.checkpoints import load_page, save_page
from ..schemas import Element, ElementType, Revision, TableData, Uncertainty
from ..schemas.elements import MATH_TYPES
from ..service import ScholarOCR
from ..utils import now_iso
from . import prompts
from .agy import AgyCancelled, AgyError, AgyRateLimited, AgyRunner

log = get_logger("webapp.review")

NOTE_PREFIX = "review not finished: "
REJECTED_BY = "review-rejected:"
UNDO_BY = "review-undo:"
SNAPSHOT_FIELD = "before_review"
MAX_PROMPT_CANDIDATES_CHARS = 20000  # agy.MAX_PROMPT_CHARS minus the rules; later elements stay unreviewed
MAX_CROP_SIDE = 1400  # as in the source panel
SYNTAX_ISSUES = ("unbalanced", "\\left", "\\end", "unclosed", "stray", "\\frac", "dangling", "empty", "replacement")
MIN_SIMILARITY = 0.5  # a correction changes characters; one that replaces most of a reading is a different reading
MIN_LEN_FOR_SIMILARITY = 20
UNSURE_LABELS = ("high", "unresolved")
_DOLLAR = re.compile(r"(?<!\\)\$")
_INLINE_MATH = re.compile(r"(?<!\\)\$(.+?)(?<!\\)\$", re.S)
_READ_TOOLS = ("read_file",)


class ReviewItem(BaseModel):
    """One verdict in the reply; ``intelligence.responses.ReviewResponse`` plus the crop it is about."""

    crop: str = Field(description="File name of the crop this verdict is about, e.g. crop-1.png.")
    verdict: Literal["confirmed", "corrected", "unreadable"]
    text: str = Field(default="", description="With corrected: the element's complete corrected text.")
    latex: str | None = Field(default=None, description="With corrected, for equations and chemistry: the complete LaTeX.")
    table: XTable | None = Field(default=None, description="With corrected, for tables: the complete table.")
    uncertainty: UncertaintyLabel = Field(default="low", description="Your own doubt about this verdict.")
    notes: list[str] = Field(default_factory=list, description="One short phrase saying what changed.")


class ReviewBatch(BaseModel):
    reviews: list[ReviewItem]


def review_schema(*, with_table: bool) -> dict[str, Any]:
    """The reply's JSON schema. The table part is large, so it is left out unless a table is being checked."""
    schema = to_gemini_schema(ReviewBatch)
    if not with_table:
        schema["properties"]["reviews"]["items"]["properties"].pop("table", None)
    return schema


@dataclass
class ReviewPlan:
    """One document's reviews in a read run. ``stopped``: agy reported throttling, so the other pages' reviews are
    left out and what remains of the quota goes to reading pages."""

    model: str | None
    effort: str | None
    label: str
    stopped: bool = False


# ---------------------------------------------------------------------- checks on a correction
def candidate_text(el: Element) -> str:
    return el.content or ("\n".join(el.items) if el.items else "")


def _same(a: str | None, b: str | None) -> bool:
    return " ".join((a or "").split()) == " ".join((b or "").split())


def _table_cells(table) -> list[list[str]]:
    return [[(c.text or "").strip() for c in row] for row in (table.rows if table else [])]


def is_real_change(el: Element, resp) -> bool:
    """A "correction" that returns what is already stored (spacing aside) is an agreement."""
    if el.type == ElementType.TABLE:
        return resp.table is not None and _table_cells(_table(resp.table)) != _table_cells(el.table)
    if el.type in MATH_TYPES and resp.latex and not _same(resp.latex, el.latex):
        return True
    return bool(resp.text) and not _same(resp.text, candidate_text(el))


def correction_problems(el: Element, resp) -> list[str]:
    """Why a correction must not replace the reading. Empty: it may."""
    problems: list[str] = []
    if resp.uncertainty in UNSURE_LABELS:
        problems.append(f"the model's own doubt about its correction is {resp.uncertainty}")
    if el.type in MATH_TYPES:
        if resp.latex:
            issues = (latex_issues(resp.latex) if el.type == ElementType.EQUATION
                      else chemistry_issues(resp.latex, resp.text or el.content))
            syntax = [i for i in issues if i.startswith(SYNTAX_ISSUES)]
            if syntax:
                problems.append("LaTeX problem: " + "; ".join(syntax))
        elif el.latex:
            problems.append("the text was changed but not the LaTeX")
    elif el.type == ElementType.TABLE:
        new = _table(resp.table) if resp.table is not None else None
        if new is None or not new.rows:
            problems.append("no corrected table")
        else:
            added = [i for i in table_issues(new) if i not in table_issues(el.table)]
            if added:
                problems.append("table problem: " + "; ".join(added))
    else:
        text = resp.text or ""
        old = candidate_text(el)
        if not text.strip():
            problems.append("empty text")
        if len(_DOLLAR.findall(text)) % 2 and not len(_DOLLAR.findall(old)) % 2:
            problems.append("unbalanced $ around inline math")
        else:
            for span in _INLINE_MATH.findall(text):
                syntax = [i for i in latex_issues(span) if i.startswith(SYNTAX_ISSUES)]
                if syntax:
                    problems.append(f"LaTeX problem in ${span[:60]}$: " + "; ".join(syntax))
        if (el.type != ElementType.UNCERTAIN and len(old) >= MIN_LEN_FOR_SIMILARITY
                and difflib.SequenceMatcher(None, old, text).ratio() < MIN_SIMILARITY):
            problems.append("it replaces most of the reading")
    return problems


# ---------------------------------------------------------------------- applying one verdict
def snapshot(el: Element) -> dict[str, Any]:
    return {"content": el.content, "clean_content": el.clean_content, "clean_content_source": el.clean_content_source,
            "latex": el.latex, "table": el.table.model_dump() if el.table else None,
            "items": list(el.items) if el.items is not None else None, "type": el.type.value,
            "uncertainty": el.uncertainty.value, "uncertainty_reasons": list(el.uncertainty_reasons)}


def _reject(svc: ScholarOCR, doc_id: str, eid: str, by: str, reason: str, proposed: dict[str, Any]) -> str:
    page, el = svc.find_element(doc_id, eid)
    el.revisions.append(Revision(at=now_iso(), by=REJECTED_BY + by, field="correction", previous=proposed,
                                 note=reason[:400]))
    save_page(svc.store, page)
    return "rejected"


def apply_verdict(svc: ScholarOCR, doc_id: str, eid: str, item: dict[str, Any], *, by: str) -> str:
    """Store one verdict. Returns confirmed, corrected, unreadable or rejected."""
    payload = {k: v for k, v in item.items() if k != "crop"}
    proposed = {k: payload.get(k) for k in ("text", "latex") if payload.get(k)}
    try:
        resp, _ = host.validate_review(dict(payload))
    except SubmissionRejected as e:
        errors = (e.details or {}).get("errors") or []
        return _reject(svc, doc_id, eid, by, "schema: " + "; ".join(
            f"{p.get('path')}: {p.get('problem')}" for p in errors[:3]), proposed)
    page, el = svc.find_element(doc_id, eid)
    before = snapshot(el)
    if resp.verdict == "confirmed" and resp.uncertainty != "low":
        # the model agrees but doubts it: that does not settle the doubt, so the mark stays
        return _reject(svc, doc_id, eid, by, f"the model confirmed it, but its own doubt is {resp.uncertainty}", proposed)
    if resp.verdict == "corrected":
        if not is_real_change(el, resp):
            payload = {"verdict": "confirmed", "uncertainty": "low",
                       "notes": ["the corrected reading was the same as the stored one"]}
        else:
            problems = correction_problems(el, resp)
            if problems:
                return _reject(svc, doc_id, eid, by, "; ".join(problems), proposed)
            if el.type != ElementType.TABLE:
                payload.pop("table", None)
            if el.type not in MATH_TYPES:
                payload.pop("latex", None)
    try:
        out = svc.submit_review(doc_id, eid, payload, transcriber=by)
    except SubmissionRejected as e:
        return _reject(svc, doc_id, eid, by, e.message, proposed)
    verdict = out["verdict"]
    if verdict == "confirmed":
        return verdict
    page, el = svc.find_element(doc_id, eid)
    if verdict == "corrected":
        if before["items"]:  # the list is shown and exported from its items
            el.items = [ln for ln in el.content.splitlines() if ln.strip()] or el.items
        if el.type == ElementType.UNCERTAIN and "[illegible" not in el.content:
            el.type = ElementType.PARAGRAPH  # the region has been read; it stays marked as changed
    el.revisions.append(Revision(at=now_iso(), by=f"review:{host.HOST_LABEL}:{by}", field=SNAPSHOT_FIELD, previous=before))
    save_page(svc.store, page)
    return verdict


def undo_verdict(svc: ScholarOCR, doc_id: str, eid: str) -> None:
    """Put back the reading from before a correction (or an "unreadable" verdict). The undone values stay in the
    revisions; the element is not reviewed again."""
    page, el = svc.find_element(doc_id, eid)
    info = review_info(el)
    if not info or not info.get("can_undo"):
        raise InputError("this element has no change from the review pass to undo")
    undone = {"content": el.content, "latex": el.latex, "table": el.table.model_dump() if el.table else None}
    snap = info.get("snapshot")
    if snap:
        el.content = snap["content"]
        el.clean_content, el.clean_content_source = snap["clean_content"], snap["clean_content_source"]
        el.latex = snap["latex"]
        el.table = TableData.model_validate(snap["table"]) if snap["table"] else None
        el.items = snap["items"]
        el.type = ElementType(snap["type"])
        el.uncertainty = Uncertainty(snap["uncertainty"])
        el.uncertainty_reasons = list(snap["uncertainty_reasons"])
    else:  # reviewed by a key engine's pipeline: the revisions hold the replaced values
        prev = info["previous"]
        if "content" in prev:
            el.content = prev["content"]
            if el.clean_content is not None:
                el.clean_content, el.clean_content_source = rule_clean(el.content, el.type.value), "rules"
        if "latex" in prev:
            el.latex = prev["latex"]
        if "table" in prev:
            el.table = TableData.model_validate(prev["table"]) if prev["table"] else None
        el.uncertainty = Uncertainty(prev["uncertainty"])
        el.uncertainty_reasons = [r for r in el.uncertainty_reasons if not r.startswith("reviewed: ")]
    el.revisions.append(Revision(at=now_iso(), by=UNDO_BY + "app", field="correction", previous=undone,
                                 note="undone in the app"))
    save_page(svc.store, page)
    svc.repo.update_document(doc_id)


# ---------------------------------------------------------------------- what the page shows
def review_info(el: Element) -> dict[str, Any] | None:
    """The element's last review event, from its revisions. state: confirmed, corrected, unreadable, rejected or
    undone. None when no review pass has looked at it."""
    info: dict[str, Any] | None = None
    previous: dict[str, Any] = {}
    for r in el.revisions:
        if r.by.startswith(UNDO_BY):
            info = {"state": "undone", "undone": r.previous}
        elif r.by.startswith(REJECTED_BY):
            info = {"state": "rejected", "reason": r.note, "proposed": r.previous}
        elif r.by.startswith("review:"):
            if r.field == "uncertainty":
                note = r.note or ""
                verdict, _, rest = note.partition(";")
                state = verdict.removeprefix("verdict=").strip() or "confirmed"
                previous["uncertainty"] = r.previous
                info = {"state": state, "previous": previous, "notes": rest.strip(),
                        "can_undo": state in ("corrected", "unreadable")}
                previous = {}
            elif r.field == SNAPSHOT_FIELD:
                if info is not None:
                    info["snapshot"] = r.previous
            elif r.field in ("content", "latex", "table"):
                previous[r.field] = r.previous
    return info


def _table_as_text(table: dict[str, Any] | None) -> str | None:
    if not table:
        return None
    return "\n".join(" | ".join(str(c.get("text") or "") for c in row) for row in table.get("rows") or [])


def review_view(el: Element) -> dict[str, Any] | None:
    """The browser's part of ``review_info``."""
    info = review_info(el)
    if info is None:
        return None
    out: dict[str, Any] = {"state": info["state"]}
    if info.get("notes"):
        out["notes"] = info["notes"][:300]
    if info["state"] == "rejected":
        out["reason"] = (info.get("reason") or "")[:400]
        proposed = info.get("proposed") or {}
        out["proposed"] = {k: str(v)[:4000] for k, v in proposed.items()}
    elif info["state"] == "undone":
        undone = info.get("undone") or {}
        out["undone"] = {"text": (undone.get("content") or "")[:4000], "latex": undone.get("latex"),
                         "table": _table_as_text(undone.get("table"))}
    elif info.get("can_undo"):
        snap = info.get("snapshot") or {}
        prev = info.get("previous") or {}
        if info["state"] == "corrected":
            text = snap.get("content", prev.get("content"))
            if not text and snap.get("items"):
                text = "\n".join(snap["items"])
            out["original"] = {"text": (text or "")[:4000], "latex": snap.get("latex", prev.get("latex")),
                               "table": _table_as_text(snap.get("table", prev.get("table")))}
        out["can_undo"] = True
    return out


def shown_uncertainty(el: Element) -> str:
    """The mark on the sheet. A confirmed element shows none, even when ``after_review`` keeps it at medium (high
    goes down one step); what stays is what the second look could not settle: an unreadable region."""
    if el.uncertainty != Uncertainty.UNRESOLVED:
        info = review_info(el)
        if info and info["state"] == "confirmed":
            return Uncertainty.LOW.value
    return el.uncertainty.value


def page_note(warnings: list[str]) -> str | None:
    for w in warnings:
        if w.startswith(NOTE_PREFIX):
            return w[len(NOTE_PREFIX):]
    return None


# ---------------------------------------------------------------------- one page's review
def _set_note(svc: ScholarOCR, doc_id: str, n: int, reason: str | None) -> None:
    page = load_page(svc.store, doc_id, n)
    if page is None:
        return
    page.warnings = [w for w in page.warnings if not w.startswith(NOTE_PREFIX)]
    if reason:
        page.warnings.append(NOTE_PREFIX + reason)
    save_page(svc.store, page)


def _prepare(svc: ScholarOCR, doc_id: str, ids: list[str], crops_dir: Path) -> tuple[list[dict], dict[str, str], bool]:
    """Write the crops. Returns (candidates for the prompt, crop name -> element id, any table)."""
    crops_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[dict[str, Any]] = []
    by_crop: dict[str, str] = {}
    with_table = False
    used = 0
    for eid in ids:
        _, el = svc.find_element(doc_id, eid)
        cand: dict[str, Any] = {"type": el.type.value, "text": candidate_text(el)}
        if el.handwritten:
            cand["handwritten"] = True
        if el.latex:
            cand["latex"] = el.latex
        if el.table:
            cand["table"] = el.table.model_dump(include={"rows", "header_rows"})
        size = len(json.dumps(cand, ensure_ascii=False))
        if used + size > MAX_PROMPT_CANDIDATES_CHARS:
            continue
        used += size
        png, _ = svc.get_source_region(doc_id, element_id=eid, max_side=MAX_CROP_SIDE)
        name = f"crop-{len(candidates) + 1}.png"
        (crops_dir / name).write_bytes(png)
        candidates.append({"crop": name, **cand})
        by_crop[name] = eid
        with_table = with_table or el.type == ElementType.TABLE
    return candidates, by_crop, with_table


def _opened(seen: set[str]) -> Callable[[dict], None]:
    def on_event(step: dict) -> None:
        tool = step.get("tool_info") or {}
        if step.get("tool_name") in _READ_TOOLS or tool.get("name") in _READ_TOOLS:
            for v in (tool.get("parameters") or {}).values():
                if isinstance(v, str):
                    seen.add(v.replace("\\", "/").rsplit("/", 1)[-1])

    return on_event


def review_page(runner: AgyRunner, svc: ScholarOCR, doc_id: str, n: int, ids: list[str], plan: ReviewPlan, *,
                run_dir: Path, timeout_s: float, cancel: threading.Event | None,
                on_usage: Callable[..., None] | None = None) -> dict[str, Any]:
    """Review the page's selected elements. Never raises: the page is already accepted. Returns the page's review
    stats; ``state`` is done, partial, failed, rate_limited, stopped or skipped."""
    stats: dict[str, Any] = {"state": "done", "flagged": len(ids), "confirmed": 0, "corrected": 0, "unreadable": 0,
                             "rejected": 0, "not_reviewed": len(ids), "usage": {}, "seconds": 0.0}
    started = time.monotonic()
    reason: str | None = None
    try:
        if plan.stopped:
            stats["state"] = "skipped"
            reason = "Antigravity reported rate limiting earlier in this run"
            return stats
        crops_dir = run_dir / "crops"
        candidates, by_crop, with_table = _prepare(svc, doc_id, ids, crops_dir)
        folder = str(crops_dir.resolve()).replace("\\", "/")
        seen: set[str] = set()
        res = runner.run(prompts.review_prompt(folder, candidates), run_dir=run_dir, model=plan.model,
                         read_dirs=[crops_dir], schema=review_schema(with_table=with_table), timeout_s=timeout_s,
                         cancel=cancel, effort=plan.effort, on_event=_opened(seen))
        stats["usage"] = dict(res.usage)
        stats["agy_seconds"] = res.duration_s
        if on_usage:
            on_usage(usage=res.usage, agy_seconds=res.duration_s, wall_seconds=res.wall_s)
        reply = res.structured.get("reviews") if isinstance(res.structured, dict) else None
        if not isinstance(reply, list):
            stats["state"], reason = "failed", "the model's reply was not in the review format"
            return stats
        by = f"antigravity:{plan.label}"
        answered: set[str] = set()
        for raw in reply:
            name = raw.get("crop") if isinstance(raw, dict) else None
            eid = by_crop.get(name) if isinstance(name, str) else None
            if eid is None or name in answered:
                continue
            answered.add(name)
            if name not in seen:  # a verdict on an image the model never opened says nothing
                continue
            try:
                item = ReviewItem.model_validate(raw).model_dump(exclude_none=True)
            except ValidationError as e:
                problem = e.errors(include_url=False)[0]
                outcome = _reject(svc, doc_id, eid, by, f"schema: {problem.get('loc')}: {problem.get('msg')}",
                                  {k: raw.get(k) for k in ("text", "latex") if raw.get(k)})
            else:
                outcome = apply_verdict(svc, doc_id, eid, item, by=by)
            stats[outcome] += 1
            if outcome != "rejected":
                stats["not_reviewed"] -= 1
        svc.repo.update_document(doc_id)
        if stats["not_reviewed"] > stats["rejected"]:
            stats["state"] = "partial"
            missing = stats["not_reviewed"] - stats["rejected"]
            reason = f"{missing} element{'s' if missing != 1 else ''} got no usable answer from the model"
        return stats
    except AgyCancelled:
        stats["state"], reason = "stopped", "stopped"
        return stats
    except AgyRateLimited as e:
        plan.stopped = True
        stats.update(state="rate_limited", error=e.message[:400])
        reason = "Antigravity reported rate limiting or an exhausted quota"
        return stats
    except AgyError as e:
        stats.update(state="failed", error=e.message[:400])
        reason = e.message[:200]
        return stats
    except ScholarOCRError as e:
        stats.update(state="failed", error=e.message[:400])
        reason = e.message[:200]
        return stats
    except Exception as e:  # noqa: BLE001 - whatever happens here, the page stays accepted
        log.exception("review of %s page %s failed", doc_id, n)
        stats.update(state="failed", error=f"{e.__class__.__name__}: {e}"[:400])
        reason = "an internal error"
        return stats
    finally:
        stats["seconds"] = round(time.monotonic() - started, 1)
        shutil.rmtree(run_dir, ignore_errors=True)
        try:
            _set_note(svc, doc_id, n, reason)
        except Exception:  # noqa: BLE001
            log.exception("could not store the review note of %s page %s", doc_id, n)
