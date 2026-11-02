"""Targeted second-pass review of flagged elements, using a crop of the ORIGINAL page image.

Only selected elements are reviewed (never whole documents), which keeps cost bounded.
"""

from __future__ import annotations

from PIL import Image

from ..config import Settings
from ..schemas import Element, ElementType, Revision, Uncertainty
from ..schemas.elements import MATH_TYPES, MODEL_METHODS
from ..utils import now_iso
from . import prompts
from .extractor import _table, encode_image
from .gemini_client import GeminiClient, TextPart
from .responses import ReviewResponse
from .uncertainty import after_review

REVIEWABLE = MATH_TYPES | {ElementType.TABLE, ElementType.HANDWRITING, ElementType.PARAGRAPH, ElementType.TEXT,
                           ElementType.HEADING, ElementType.SUBHEADING, ElementType.TITLE, ElementType.LIST,
                           ElementType.CAPTION, ElementType.UNCERTAIN}
MAX_REVIEWS_PER_PAGE = 8


def select_for_review(elements: list[Element], *, detail: str) -> list[Element]:
    """Flagged = medium/high uncertainty (or unresolved 'uncertain' regions, which get one more
    look). With detail="high", every equation and table is reviewed too. Requires a bbox."""
    chosen = []
    for el in elements:
        if el.bbox is None or el.type not in REVIEWABLE or el.extraction_method not in MODEL_METHODS:
            continue
        flagged = el.uncertainty in (Uncertainty.MEDIUM, Uncertainty.HIGH) or el.type == ElementType.UNCERTAIN
        if detail == "high" and (el.type in MATH_TYPES or el.type == ElementType.TABLE):
            flagged = True
        if flagged:
            chosen.append(el)
    rank = {Uncertainty.UNRESOLVED: 0, Uncertainty.HIGH: 1, Uncertainty.MEDIUM: 2, Uncertainty.LOW: 3}
    chosen.sort(key=lambda e: (rank[e.uncertainty], e.reading_order))
    return chosen[:MAX_REVIEWS_PER_PAGE]


def crop_region(page_image: Image.Image, el: Element, dpi: float, pad_pt: float = 6.0) -> Image.Image:
    assert el.bbox is not None
    k = dpi / 72.0
    x0 = max(0, int((el.bbox.x0 - pad_pt) * k))
    y0 = max(0, int((el.bbox.y0 - pad_pt) * k))
    x1 = min(page_image.width, int((el.bbox.x1 + pad_pt) * k) + 1)
    y1 = min(page_image.height, int((el.bbox.y1 + pad_pt) * k) + 1)
    return page_image.crop((x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)))


def review_element(client: GeminiClient, settings: Settings, el: Element, crop: Image.Image, *, document_id: str) -> str:
    """Review one element in place. Returns the verdict."""
    candidate = {"type": el.type.value, "text": el.content}
    if el.latex:
        candidate["latex"] = el.latex
    if el.table:
        candidate["table"] = el.table.model_dump(include={"rows", "header_rows"})
    focus = "handwriting" if el.handwritten else el.type.value
    resp, info = client.generate_structured(
        purpose=f"review:{el.type.value}", model=settings.gemini_review_model, system=prompts.REVIEW_SYSTEM,
        parts=[encode_image(crop), TextPart(prompts.review_prompt(focus, candidate))], schema=ReviewResponse,
        prompt_version=prompts.PROMPT_VERSION, document_id=document_id, page_number=el.page_number,
    )
    apply_review(el, resp, by=f"review:{info.model}")
    return resp.verdict


def apply_review(el: Element, resp: ReviewResponse, *, by: str) -> None:
    """Apply a review verdict in place (shared by the key-provider reviewer and host-mode submit_review).
    Replaced values are kept in ``revisions``; the label follows ``uncertainty.after_review``."""
    ts = now_iso()
    if resp.verdict == "corrected":
        if resp.text and resp.text != el.content:
            el.revisions.append(Revision(at=ts, by=by, field="content", previous=el.content))
            el.content = resp.text
        if resp.latex and resp.latex != el.latex:
            el.revisions.append(Revision(at=ts, by=by, field="latex", previous=el.latex))
            el.latex = resp.latex
        if resp.table and el.type == ElementType.TABLE:
            el.revisions.append(Revision(at=ts, by=by, field="table", previous=el.table.model_dump() if el.table else None))
            new = _table(resp.table)
            if new and el.table:
                new.caption, new.label = new.caption or el.table.caption, new.label or el.table.label
            el.table = new
    new_label = after_review(el.uncertainty, resp.verdict, Uncertainty(resp.uncertainty))
    el.revisions.append(Revision(at=ts, by=by, field="uncertainty", previous=el.uncertainty.value,
                                 note=f"verdict={resp.verdict}" + (f"; {'; '.join(resp.notes)[:300]}" if resp.notes else "")))
    el.uncertainty = new_label
    el.uncertainty_reasons.append(f"reviewed: {resp.verdict}")
