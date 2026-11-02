"""Visual page extraction via Gemini and conversion of the response into IR elements."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

from ..config import Settings
from ..schemas import (BBox, Element, ElementType, FigureData, QualityAssessment, QuestionInfo, Role, Route, SourceRef,
                       TableCell, TableData, Uncertainty)
from ..preprocessing import geometry
from ..utils import element_id
from . import prompts
from .gemini_client import CallInfo, GeminiClient, ImagePart, TextPart
from .responses import PageExtractionResponse, XTable
from .uncertainty import apply_rules


@dataclass
class VisualPageContext:
    document_id: str
    page_id: str
    page_number: int
    source_file: str
    source_page_index: int
    page_width_pt: float
    page_height_pt: float
    original_dpi: float  # dpi of the original page image (px per inch)
    original_size: tuple[int, int]
    processed_size: tuple[int, int]
    matrix: list[list[float]]  # original px -> processed px
    page_image_rel: str | None
    quality: QualityAssessment | None


def model_for_route(settings: Settings, route: Route) -> str:
    return settings.math_model if route == Route.VISUAL_MATH else settings.gemini_model


def encode_image(img: Image.Image, max_bytes: int = 7_000_000) -> ImagePart:
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=6)
    if buf.tell() <= max_bytes:
        return ImagePart(buf.getvalue(), "image/png")
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    return ImagePart(buf.getvalue(), "image/jpeg")


def extract_page(client: GeminiClient, settings: Settings, route: Route, image: Image.Image, *, document_id: str,
                 page_number: int, document_type: str | None, language_hint: str | None, context_hint: str | None,
                 text_layer: str | None) -> tuple[PageExtractionResponse, CallInfo]:
    prompt = prompts.extraction_prompt(route, document_type=document_type, language_hint=language_hint,
                                       context_hint=context_hint, text_layer=text_layer)
    return client.generate_structured(
        purpose=f"extract:{route.value}", model=model_for_route(settings, route), system=prompts.EXTRACTION_SYSTEM,
        parts=[encode_image(image), TextPart(prompt)], schema=PageExtractionResponse,
        prompt_version=prompts.PROMPT_VERSION, document_id=document_id, page_number=page_number,
    )


def box_to_bbox(box: list[int] | None, ctx: VisualPageContext) -> BBox | None:
    """Map a Gemini box_2d ([ymin,xmin,ymax,xmax] in 0-1000 of the processed image) to page points."""
    if not box or len(box) != 4:
        return None
    ymin, xmin, ymax, xmax = (float(v) for v in box)
    if not (0 <= xmin < xmax <= 1000 and 0 <= ymin < ymax <= 1000):
        return None
    pw, ph = ctx.processed_size
    inv = geometry.invert(ctx.matrix)
    x0, y0, x1, y1 = geometry.apply_box(inv, xmin / 1000 * pw, ymin / 1000 * ph, xmax / 1000 * pw, ymax / 1000 * ph)
    k = 72.0 / ctx.original_dpi
    bb = BBox(x0=x0 * k, y0=y0 * k, x1=x1 * k, y1=y1 * k).clamp(ctx.page_width_pt, ctx.page_height_pt)
    return bb if bb.width > 0.5 and bb.height > 0.5 else None


def _table(x: XTable | None) -> TableData | None:
    if x is None:
        return None
    return TableData(
        rows=[[TableCell(text=c.text, latex=c.latex, row_span=max(1, c.row_span), col_span=max(1, c.col_span),
                         is_header=c.is_header, uncertain=c.uncertain) for c in row] for row in x.rows],
        header_rows=max(0, x.header_rows), caption=x.caption, label=x.label,
    )


def convert_response(resp: PageExtractionResponse, ctx: VisualPageContext, *, model: str,
                     method: str = "gemini") -> list[Element]:
    """Turn a page response (from a key provider, MCP sampling or a host-model submission; all use the
    same schema and box convention) into IR elements with boxes in page points."""
    elements: list[Element] = []
    ids: list[str] = []
    for i, _ in enumerate(resp.elements):
        ids.append(element_id(ctx.page_id, i + 1))
    for i, x in enumerate(resp.elements):
        etype = ElementType(x.type)
        bbox = box_to_bbox(x.box_2d, ctx)
        el = Element(
            element_id=ids[i], type=etype, role=Role(x.role), page_id=ctx.page_id, page_number=ctx.page_number,
            reading_order=i + 1, content=x.text, latex=x.latex or None, level=x.level, items=x.items,
            language=x.language or resp.page_language, table=_table(x.table),
            figure=FigureData(**x.figure.model_dump()) if x.figure else None,
            question=QuestionInfo(**x.question.model_dump()) if x.question else None,
            handwritten=x.handwritten or etype == ElementType.HANDWRITING,
            bbox=bbox, bbox_source="model_estimate" if bbox else None,
            uncertainty=Uncertainty(x.uncertainty), uncertainty_reasons=list(x.uncertainty_reasons),
            extraction_method=method, model=model,
            source=SourceRef(document_id=ctx.document_id, page_number=ctx.page_number, source_file=ctx.source_file,
                             source_page_index=ctx.source_page_index, page_image=ctx.page_image_rel),
        )
        if x.box_2d and bbox is None:
            el.uncertainty_reasons.append("invalid bounding box from model")
        ref = x.caption_of if x.caption_of is not None else x.parent_index
        if ref is not None and 0 <= ref < len(ids) and ref != i:
            el.parent_id = ids[ref]
        if etype == ElementType.TABLE and el.table and x.table and x.table.caption and not el.table.caption:
            el.table.caption = x.table.caption
        apply_rules(el, quality=ctx.quality, page_legibility=resp.legibility)
        elements.append(el)
    return elements
