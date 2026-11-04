"""What the browser receives about a document: pages with structured elements, verification, exports.

Everything here is data (strings, numbers). The page escapes all of it when it builds the DOM; nothing is
pre-rendered as HTML on the server.
"""

from __future__ import annotations

import shutil

from typing import Any

from ..export.base import document_is_handwritten, resolve_text_variant
from ..schemas import ElementType, PageStatus
from ..service import ScholarOCR
from .review import NOTE_PREFIX, page_note, review_view, shown_uncertainty

MAX_ELEMENTS_PER_PAGE = 400
MAX_TEXT = 20000
SPREADSHEET_TYPES = {ElementType.TABLE}


def _cut(s: str | None, n: int = MAX_TEXT) -> str | None:
    if s is None:
        return None
    return s if len(s) <= n else s[:n] + "…"


def element_view(el) -> dict[str, Any]:
    d: dict[str, Any] = {"id": el.element_id, "type": el.type.value, "role": el.role.value,
                         "text": _cut(el.content) or "", "uncertainty": el.uncertainty.value,
                         "mark": shown_uncertainty(el), "has_source": True}
    review = review_view(el)
    if review is not None:
        d["review"] = review
    if el.latex:
        d["latex"] = _cut(el.latex, 4000)
    if el.level is not None:
        d["level"] = el.level
    if el.items:
        d["items"] = [_cut(i, 2000) for i in el.items[:200]]
    if el.table is not None:
        d["table"] = {"header_rows": el.table.header_rows, "caption": _cut(el.table.caption, 500),
                      "label": el.table.label,
                      "rows": [[{"text": _cut(c.text, 2000) or "", "latex": _cut(c.latex, 1000), "row_span": c.row_span,
                                 "col_span": c.col_span, "is_header": c.is_header, "uncertain": c.uncertain}
                                for c in row[:60]] for row in el.table.rows[:300]]}
    if el.clean_content is not None and el.clean_content != el.content:
        d["clean_text"] = _cut(el.clean_content)
    if el.figure is not None:
        d["figure"] = {"description": _cut(el.figure.description, 4000), "labels": el.figure.labels[:60],
                       "kind": el.figure.kind, "figure_label": el.figure.figure_label,
                       # which stored images exist: the app shows the cleaned crop, toggles to the original
                       "images": {"clean": bool(el.figure.image_clean), "original": bool(el.source.region_image),
                                  "svg": bool(el.figure.svg)}}
        fid = el.figure.svg_fidelity
        if el.figure.svg and fid:  # shown next to "Redrawn"; a failed check is labelled "not faithful"
            d["figure"]["svg_fidelity"] = {"score": fid.get("score"), "passed": bool(fid.get("passed")),
                                           "recall": fid.get("recall"), "precision": fid.get("precision"),
                                           "labels_missing": (fid.get("labels_missing") or [])[:10]}
    if el.question is not None and (el.question.number or el.question.marks):
        d["question"] = {"number": el.question.number, "marks": el.question.marks}
    reasons = [r for r in el.uncertainty_reasons if not r.startswith("reviewed: ")]  # the review field says it
    if reasons:
        d["reasons"] = [_cut(r, 300) for r in reasons[:4]]
    if el.handwritten:
        d["handwritten"] = True
    if el.parent_id:
        d["parent_id"] = el.parent_id
    if el.language:
        d["language"] = el.language
    return d


def document_view(svc: ScholarOCR, document_id: str, text_pref: str = "auto") -> dict[str, Any]:
    """``text_pref`` is the saved setting (auto | clean | faithful); the view says which variant to show."""
    doc = svc.get_document(document_id)
    counts = {"low": 0, "medium": 0, "high": 0, "unresolved": 0}  # as marked on the sheet
    reviews = {"confirmed": 0, "corrected": 0, "unreadable": 0, "rejected": 0, "undone": 0}
    pages = []
    has_tables = False
    for p in sorted(doc.pages, key=lambda x: x.page_number):
        els = sorted(p.elements, key=lambda e: e.reading_order)
        for e in els:
            shown = shown_uncertainty(e)
            counts[shown] = counts.get(shown, 0) + 1
            has_tables = has_tables or e.type in SPREADSHEET_TYPES
        entry: dict[str, Any] = {"page_number": p.page_number, "status": p.status.value,
                                 "route": p.route.value if p.route else None, "label": p.page_label,
                                 "model": p.model, "elements": [element_view(e) for e in els[:MAX_ELEMENTS_PER_PAGE]]}
        if len(els) > MAX_ELEMENTS_PER_PAGE:
            entry["truncated"] = len(els) - MAX_ELEMENTS_PER_PAGE
        if p.error:
            entry["error"] = str(p.error)[:400]
        for ev in entry["elements"]:
            if "review" in ev:
                reviews[ev["review"]["state"]] = reviews.get(ev["review"]["state"], 0) + 1
        note = page_note(p.warnings)
        if note:
            entry["review_note"] = note
        warnings = [w for w in p.warnings if not w.startswith(("model note:", NOTE_PREFIX))][:5]
        notes = [w[len("model note: "):] for w in p.warnings if w.startswith("model note:")][:5]
        if warnings:
            entry["warnings"] = warnings
        if notes:
            entry["notes"] = notes
        pages.append(entry)
    done = sum(1 for p in doc.pages if p.status in (PageStatus.DONE, PageStatus.BLANK))
    formats = ["markdown", "docx", "latex", "searchable-pdf", "layout-pdf", "json"] + (["csv", "xlsx"] if has_tables else [])
    plan = svc.resume_plan(document_id, doc)
    return {"document_id": document_id, "title": doc.title, "filenames": doc.original_filenames,
            "page_count": doc.page_count, "pages_done": done, "status": doc.status, "pages": pages,
            "uncertainty": counts, "reviews": reviews, "export_formats": formats if done else [],
            "native_equations": shutil.which("pandoc") is not None,  # Word export: OMML needs pandoc
            "handwritten": document_is_handwritten(doc),
            "text_variant": resolve_text_variant(doc, text_pref),
            # read before box refinement, figure crops or cleaned text existed: offer "Update layout"
            "layout_update": svc.layout_outdated(document_id, doc),
            # pages not accepted yet: offer "Resume reading", or say that the stored source is gone
            "resume": {"unread": plan["unread"], "source_missing": plan["source_missing"]} if plan["unread"] else None}


def verification_view(report) -> dict[str, Any]:
    findings = [{"check": f.check, "severity": f.severity.value, "message": f.message[:400],
                 "page": f.page_number, "element_id": f.element_id} for f in report.findings[:80]]
    return {"passed": report.passed, "counts": report.counts(), "findings": findings}


def chat_context(svc: ScholarOCR, document_ids: list[str], max_chars: int) -> tuple[str, bool, list[int]]:
    """Extracted content for grounding a chat reply: ``=== <file> page N ===`` blocks, newest document first.

    Returns (context, truncated, pages included of the first document)."""
    parts: list[str] = []
    used = 0
    truncated = False
    first_pages: list[int] = []
    for i, doc_id in enumerate(document_ids):
        try:
            manifest = svc.load_manifest(doc_id)
        except Exception:
            continue
        name = ", ".join(s.filename for s in manifest.sources)[:120]
        for entry in manifest.pages:
            try:
                res = svc.get_content(doc_id, pages=str(entry.page_number), fmt="markdown", include_ids=False,
                                      max_chars=max_chars)
            except Exception:
                continue
            body = (res.get("content") or "").strip()
            if not body:
                if res.get("awaiting_transcription"):
                    body = "(this page has not been read yet)"
                else:
                    continue
            block = f"=== {name} | page {entry.page_number} ===\n{body}\n"
            if used + len(block) > max_chars:
                room = max_chars - used
                if room > 400:
                    parts.append(block[:room] + "\n[... truncated ...]\n")
                    if i == 0:
                        first_pages.append(entry.page_number)
                truncated = True
                return "\n".join(parts), truncated, first_pages
            parts.append(block)
            used += len(block)
            if i == 0:
                first_pages.append(entry.page_number)
    return "\n".join(parts), truncated, first_pages
