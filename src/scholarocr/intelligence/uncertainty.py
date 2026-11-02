"""How uncertainty labels are assigned. This module is the single source of truth and is
mirrored in docs/QUALITY_ASSURANCE.md.

Labels are ordinal (low < medium < high < unresolved). They are NOT calibrated probabilities.

1. Start from the extractor's own label (Gemini's self-report, or "low" for deterministic
   text-layer extraction whose characters are clean).
2. Escalate (never de-escalate) with deterministic evidence:
   - element type "uncertain"                                   -> unresolved
   - LaTeX fails syntax checks (math/chemistry)                 -> at least high
   - table structure issues (ragged rows, uncertain cells, ...) -> at least medium
   - page legibility reported "high"/"unresolved" by the model  -> at least medium
   - page quality grade "poor" and the element is math, table,
     or handwriting                                             -> at least medium
   - handwritten element on a page graded "fair"/"poor"         -> at least medium
   - visual element (figure/diagram/table/equation) with no
     usable bounding box                                        -> reason recorded (traceability)
   - text-layer characters that are not clean (U+FFFD, private
     use)                                                       -> at least medium
3. Targeted review (second model pass on the source crop) may then change the label:
   - "confirmed": lowered by one step, but never below low; unresolved stays unresolved
   - "corrected": content replaced (previous value kept in ``revisions``); label becomes
     max(reviewer label, medium) because the two passes disagreed
   - "unreadable": unresolved
"""

from __future__ import annotations

from ..schemas import Element, ElementType, QualityAssessment, Uncertainty
from ..schemas.elements import MATH_TYPES, UNCERTAINTY_RANK, VISUAL_TYPES, max_uncertainty
from .mathematics import chemistry_issues, latex_issues
from .tables import table_issues


def escalate(el: Element, level: Uncertainty, reason: str) -> None:
    before = el.uncertainty
    el.uncertainty = max_uncertainty(el.uncertainty, level)
    if reason not in el.uncertainty_reasons and (el.uncertainty != before or UNCERTAINTY_RANK[level] > 0):
        el.uncertainty_reasons.append(reason)


def apply_rules(el: Element, *, quality: QualityAssessment | None, page_legibility: str | None = None) -> None:
    if el.type == ElementType.UNCERTAIN:
        escalate(el, Uncertainty.UNRESOLVED, "region could not be read")
    if el.type == ElementType.EQUATION:
        for issue in latex_issues(el.latex):
            escalate(el, Uncertainty.HIGH, f"latex: {issue}")
        if not el.latex:
            escalate(el, Uncertainty.MEDIUM, "equation without LaTeX")
    if el.type == ElementType.CHEMICAL_EQUATION:
        for issue in chemistry_issues(el.latex, el.content):
            escalate(el, Uncertainty.HIGH if issue.startswith(("unbalanced", "\\left", "\\end", "unclosed", "stray"))
                     else Uncertainty.MEDIUM, f"chemistry: {issue}")
    if el.type == ElementType.TABLE:
        for issue in table_issues(el.table):
            escalate(el, Uncertainty.MEDIUM, f"table: {issue}")
    if page_legibility in ("high", "unresolved"):
        escalate(el, Uncertainty.MEDIUM, f"page legibility reported {page_legibility}")
    grade = quality.grade if quality else None
    if grade == "poor" and (el.type in MATH_TYPES or el.type == ElementType.TABLE or el.handwritten):
        escalate(el, Uncertainty.MEDIUM, "poor page quality")
    if el.handwritten and grade in ("fair", "poor"):
        escalate(el, Uncertainty.MEDIUM, "handwriting on degraded page")
    if el.bbox is None and (el.type in VISUAL_TYPES or el.type in MATH_TYPES or el.type == ElementType.TABLE):
        if "no bounding box" not in el.uncertainty_reasons:
            el.uncertainty_reasons.append("no bounding box")


def after_review(current: Uncertainty, verdict: str, reviewer_label: Uncertainty) -> Uncertainty:
    if verdict == "unreadable":
        return Uncertainty.UNRESOLVED
    if verdict == "corrected":
        return max_uncertainty(reviewer_label, Uncertainty.MEDIUM)
    if current == Uncertainty.UNRESOLVED:
        return current
    order = [Uncertainty.LOW, Uncertainty.MEDIUM, Uncertainty.HIGH]
    return order[max(0, order.index(current) - 1)]
