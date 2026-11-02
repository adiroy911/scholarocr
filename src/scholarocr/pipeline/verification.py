"""Deterministic quality checks on a stored extraction. No model calls happen here; targeted
model review of flagged elements is orchestrated by the service (``verify(review=True)``).

Scopes: completeness, duplicates, page_order, content, reading_order, math, tables, structure,
uncertainty, traceability, exports.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from ..intelligence.mathematics import chemistry_issues, latex_issues
from ..intelligence.tables import table_issues
from ..schemas import (Document, ElementType, Finding, Manifest, PageStatus, Role, Route, Severity, Uncertainty,
                       VerificationReport)
from ..schemas.elements import HEADING_TYPES, MATH_TYPES, VISUAL_TYPES
from ..storage import ArtifactStore
from ..utils import now_iso

ALL_SCOPES = ["completeness", "duplicates", "page_order", "content", "reading_order", "math", "tables", "structure",
              "uncertainty", "traceability", "exports"]


def verify(document: Document, manifest: Manifest, store: ArtifactStore, *, scope: list[str] | None = None,
           exports: list[dict] | None = None) -> VerificationReport:
    scopes = scope or ALL_SCOPES
    unknown = set(scopes) - set(ALL_SCOPES)
    if unknown:
        raise ValueError(f"Unknown verification scope(s): {sorted(unknown)}; valid: {ALL_SCOPES}")
    f: list[Finding] = []
    pages = {p.page_number: p for p in document.pages}

    def add(check, sev, msg, page=None, el=None, **data):
        f.append(Finding(check=check, severity=sev, message=msg, page_number=page, element_id=el, data=data))

    if "completeness" in scopes:
        for e in manifest.pages:
            p = pages.get(e.page_number)
            if p is None or p.status == PageStatus.PENDING:
                add("completeness", Severity.WARNING, "page not processed", e.page_number)
            elif p.status == PageStatus.FAILED:
                add("completeness", Severity.ERROR, f"page extraction failed: {p.error}", e.page_number)
            elif p.status == PageStatus.BLANK:
                add("completeness", Severity.INFO, "blank page (no content expected)", e.page_number)

    if "duplicates" in scopes:
        for e in manifest.pages:
            if e.duplicate_of:
                add("duplicates", Severity.WARNING,
                    f"page {e.page_number} {'duplicates' if 'duplicate_page' in e.notes else 'may duplicate'} "
                    f"page {e.duplicate_of}", e.page_number, duplicate_of=e.duplicate_of)

    if "page_order" in scopes:
        labels: list[tuple[int, int]] = []
        for p in document.pages:
            label = p.page_label or next((el.content for el in p.elements if el.type == ElementType.PAGE_NUMBER), None)
            m = re.search(r"\d+", label or "")
            if m:
                labels.append((p.page_number, int(m.group())))
        for (pa, la), (pb, lb) in zip(labels, labels[1:]):
            if pb - pa == 1 and lb - la > 1:
                add("page_order", Severity.WARNING,
                    f"printed page numbers jump from {la} to {lb}: {lb - la - 1} page(s) may be missing", pb,
                    printed_before=la, printed_after=lb)
            elif lb < la:
                add("page_order", Severity.WARNING, f"printed page number decreases ({la} -> {lb}): unexpected order", pb)
            elif lb == la and pb != pa:
                add("page_order", Severity.INFO, f"printed page number {lb} repeats", pb)

    if "content" in scopes:
        entries = {e.page_number: e for e in manifest.pages}
        for p in document.pages:
            if p.status != PageStatus.DONE:
                continue
            chars = sum(len(el.text_for_search()) for el in p.elements)
            visuals = sum(1 for el in p.elements if el.type in VISUAL_TYPES)
            if not p.elements:
                add("content", Severity.ERROR, "empty extraction on a non-blank page", p.page_number)
            elif chars < 40 and not visuals and (p.quality.ink_ratio or 0) > 0.03:
                add("content", Severity.WARNING, f"suspiciously short extraction ({chars} chars) for an inked page",
                    p.page_number, chars=chars, ink_ratio=p.quality.ink_ratio)
            e = entries.get(p.page_number)
            if e and p.route == Route.EMBEDDED_TEXT and e.profile.text_chars:
                extracted = sum(len("".join(el.text_for_search().split())) for el in p.elements)
                if extracted < 0.7 * e.profile.text_chars:
                    add("content", Severity.WARNING, f"extracted {extracted} of {e.profile.text_chars} text-layer "
                        "characters", p.page_number)
        if len(document.pages) > 3 and not any(el.type in HEADING_TYPES for el in document.iter_elements()):
            add("content", Severity.INFO, "no headings found in the document")

    if "reading_order" in scopes:
        for p in document.pages:
            orders = [el.reading_order for el in p.elements]
            if len(orders) != len(set(orders)):
                add("reading_order", Severity.ERROR, "duplicate reading_order values", p.page_number)
            elif orders and sorted(orders) != list(range(1, len(orders) + 1)):
                add("reading_order", Severity.WARNING, "reading order has gaps", p.page_number)
            ids = [el.element_id for el in p.elements]
            if len(ids) != len(set(ids)):
                add("reading_order", Severity.ERROR, "duplicate element ids", p.page_number)

    if "math" in scopes:
        for p in document.pages:
            n_math = 0
            for el in p.elements:
                if el.type == ElementType.EQUATION:
                    n_math += 1
                    for issue in latex_issues(el.latex):
                        add("math", Severity.WARNING, f"invalid LaTeX: {issue}", p.page_number, el.element_id)
                elif el.type == ElementType.CHEMICAL_EQUATION:
                    n_math += 1
                    for issue in chemistry_issues(el.latex, el.content):
                        add("math", Severity.WARNING, f"chemistry: {issue}", p.page_number, el.element_id)
            if p.route == Route.VISUAL_MATH and p.status == PageStatus.DONE and n_math == 0 and not any(
                    "$" in el.content for el in p.elements):
                add("math", Severity.WARNING, "page routed as math-heavy but no equations were extracted", p.page_number)

    if "tables" in scopes:
        for el in document.iter_elements():
            if el.type == ElementType.TABLE:
                for issue in table_issues(el.table):
                    add("tables", Severity.WARNING, f"table: {issue}", el.page_number, el.element_id)

    if "structure" in scopes:
        nums: list[tuple[int, str, str | None, int]] = []
        for el in document.iter_elements():
            if el.role == Role.QUESTION and el.question and el.question.number:
                m = re.match(r"\d+", el.question.number.strip())
                if m:
                    nums.append((int(m.group()), el.element_id, el.question.section, el.page_number))
        for (a, _, sa, _), (b, eid, sb, pb) in zip(nums, nums[1:]):
            if sa != sb or b == 1:
                continue  # numbering may restart per section
            if b - a > 1:
                add("structure", Severity.WARNING, f"question numbering jumps from {a} to {b} (missing "
                    f"{', '.join(str(i) for i in range(a + 1, b))}?)", pb, eid)
            elif b == a:
                add("structure", Severity.WARNING, f"question {b} appears twice", pb, eid)
        for el in document.iter_elements():
            if el.type == ElementType.CAPTION and not el.parent_id:
                add("structure", Severity.INFO, "caption not linked to a figure/table", el.page_number, el.element_id)

    if "uncertainty" in scopes:
        dist = Counter(el.uncertainty.value for el in document.iter_elements())
        unresolved = [el for el in document.iter_elements() if el.uncertainty == Uncertainty.UNRESOLVED]
        high = [el for el in document.iter_elements() if el.uncertainty == Uncertainty.HIGH]
        for el in unresolved:
            add("uncertainty", Severity.WARNING, f"unresolved {el.type.value}: {'; '.join(el.uncertainty_reasons)[:200]}",
                el.page_number, el.element_id)
        for el in high:
            add("uncertainty", Severity.WARNING, f"high uncertainty {el.type.value}: "
                f"{'; '.join(el.uncertainty_reasons)[:200]}", el.page_number, el.element_id)
        add("uncertainty", Severity.INFO, f"uncertainty distribution: {dict(dist)}", distribution=dict(dist))

    if "traceability" in scopes:
        doc_dir = store.doc_dir(document.document_id)
        for p in document.pages:
            if p.original_image and not (doc_dir / p.original_image).exists():
                add("traceability", Severity.ERROR, "original page image missing", p.page_number)
            for el in p.elements:
                if el.source.document_id != document.document_id or el.source.page_number != p.page_number:
                    add("traceability", Severity.ERROR, "element source reference mismatch", p.page_number, el.element_id)
                if el.source.region_image and not (doc_dir / el.source.region_image).exists():
                    add("traceability", Severity.ERROR, "region crop missing", p.page_number, el.element_id)
                if el.bbox is None and (el.type in VISUAL_TYPES or el.type in MATH_TYPES or el.type == ElementType.TABLE):
                    add("traceability", Severity.WARNING, f"{el.type.value} has no bounding box", p.page_number,
                        el.element_id)

    if "exports" in scopes:
        n_elements = sum(len(p.elements) for p in document.pages)
        seen: set[str] = set()
        for ex in exports or []:
            if ex["format"] in seen:
                continue  # only the latest export per format
            seen.add(ex["format"])
            path = Path(ex["path"])
            if not path.exists():
                add("exports", Severity.WARNING, f"{ex['format']} export file no longer exists: {path.name}")
                continue
            if ex["format"] == "json":
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    exported = sum(len(pg.get("elements", [])) for pg in data.get("pages", []))
                    if exported != n_elements:
                        add("exports", Severity.WARNING, f"json export is stale ({exported} vs {n_elements} elements); "
                            "re-export")
                except (OSError, ValueError):
                    add("exports", Severity.ERROR, "json export is not valid JSON")
            if document.updated_at and ex["created_at"] < document.updated_at:
                add("exports", Severity.INFO, f"{ex['format']} export predates the latest extraction update")

    stats = {
        "pages": len(manifest.pages),
        "processed_pages": sum(1 for p in document.pages if p.status in (PageStatus.DONE, PageStatus.BLANK)),
        "failed_pages": sum(1 for p in document.pages if p.status == PageStatus.FAILED),
        "elements": sum(len(p.elements) for p in document.pages),
        "elements_by_type": dict(Counter(el.type.value for el in document.iter_elements())),
        "routes": dict(Counter(p.route.value for p in document.pages if p.route)),
    }
    return VerificationReport(document_id=document.document_id, created_at=now_iso(), scope=scopes,
                              passed=not any(x.severity == Severity.ERROR for x in f), findings=f, stats=stats)
