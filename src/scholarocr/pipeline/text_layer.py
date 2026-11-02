"""Deterministic extraction of IR elements from a PDF text layer (no Gemini call)."""

from __future__ import annotations

import re

from ..ingestion.pdf import BULLET_RE, body_font_size, drawing_clusters, extract_text_blocks, page_image_blocks
from ..schemas import BBox, Element, ElementType, FigureData, SourceRef, TableCell, TableData, Uncertainty
from ..utils import element_id

CAPTION_RE = re.compile(r"^(fig(ure)?\.?|table|chart|graph|scheme|plate)\s*[\dIVX]+[.:)]?\s", re.I)
PAGE_NUM_RE = re.compile(r"^(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?$", re.I)


def _clean_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 1.0
    bad = sum(1 for c in chars if c == "�" or 0xE000 <= ord(c) <= 0xF8FF)
    return 1 - bad / len(chars)


def extract_elements(page, *, document_id: str, page_id: str, page_number: int, source_file: str,
                     source_page_index: int, page_image_rel: str | None) -> list[Element]:
    blocks = extract_text_blocks(page)
    body = body_font_size(blocks)
    w, h = page.rect.width, page.rect.height
    # Map unrotated text coordinates to the displayed page (matches rendered images / model boxes).
    rot = page.rotation_matrix if page.rotation else None
    elements: list[Element] = []
    src = SourceRef(document_id=document_id, page_number=page_number, source_file=source_file,
                    source_page_index=source_page_index, page_image=page_image_rel)
    dw, dh = (h, w) if page.rotation in (90, 270) else (w, h)
    n = 0
    for b in blocks:
        x0, y0, x1, y1 = b["bbox"]
        if rot is not None:
            import pymupdf

            r = pymupdf.Rect(x0, y0, x1, y1) * rot
            x0, y0, x1, y1 = r.x0, r.y0, r.x1, r.y1
        text = b["text"].strip()
        if not text:
            continue
        etype, level = _classify_block(b, text, body, y0, y1, dh, page_number)
        n += 1
        el = Element(
            element_id=element_id(page_id, n), type=etype, page_id=page_id, page_number=page_number, reading_order=n,
            content=text, level=level, bbox=BBox(x0=x0, y0=y0, x1=x1, y1=y1).clamp(dw, dh), bbox_source="pdf_text_layer",
            formatting={"font": b["font"], "size": b["size"], "bold": b["bold"], "italic": b["italic"]},
            extraction_method="embedded_text", source=src,
        )
        if etype == ElementType.LIST:
            el.items = [BULLET_RE.sub("", ln).strip() for ln in b["lines"]]
        if _clean_ratio(text) < 1.0:
            el.uncertainty = Uncertainty.MEDIUM
            el.uncertainty_reasons.append("text layer contains unmapped characters")
        elements.append(el)
    for bb in page_image_blocks(page):
        r = bb
        if rot is not None:
            import pymupdf

            rr = pymupdf.Rect(*bb) * rot
            r = (rr.x0, rr.y0, rr.x1, rr.y1)
        n += 1
        elements.append(Element(
            element_id=element_id(page_id, n), type=ElementType.IMAGE, page_id=page_id, page_number=page_number,
            reading_order=n, content="", bbox=BBox(x0=r[0], y0=r[1], x1=r[2], y1=r[3]).clamp(dw, dh),
            bbox_source="pdf_text_layer", extraction_method="embedded_text", source=src.model_copy(),
            uncertainty_reasons=["embedded image not described (deterministic route)"],
        ))
    table_boxes = []
    for table, tb in _find_tables(page):
        table_boxes.append(tb)
        if rot is not None:
            import pymupdf

            rr = pymupdf.Rect(*tb) * rot
            tb = (rr.x0, rr.y0, rr.x1, rr.y1)
        tbox = BBox(x0=tb[0], y0=tb[1], x1=tb[2], y1=tb[3]).clamp(dw, dh)
        inside = [e for e in elements if e.type != ElementType.IMAGE and e.bbox and _center_inside(e.bbox, tbox)]
        for e in inside:
            elements.remove(e)
        n += 1
        elements.append(Element(
            element_id=element_id(page_id, n), type=ElementType.TABLE, page_id=page_id, page_number=page_number,
            reading_order=n, content="", table=table, bbox=tbox, bbox_source="pdf_text_layer",
            extraction_method="embedded_text", source=src.model_copy(),
        ))
    for cb in drawing_clusters(page):
        if any(_overlap(cb, tb) > 0.5 for tb in table_boxes):
            continue
        if rot is not None:
            import pymupdf

            rr = pymupdf.Rect(*cb) * rot
            cb = (rr.x0, rr.y0, rr.x1, rr.y1)
        margin = 14.0  # labels usually sit just outside the strokes
        grown = BBox(x0=cb[0] - margin, y0=cb[1] - margin, x1=cb[2] + margin, y1=cb[3] + margin)
        labels = [e for e in elements if e.type in (ElementType.PARAGRAPH, ElementType.TEXT) and e.bbox
                  and _center_inside(e.bbox, grown) and len(e.content) <= 40]
        xs0 = [cb[0]] + [e.bbox.x0 for e in labels if e.bbox]
        ys0 = [cb[1]] + [e.bbox.y0 for e in labels if e.bbox]
        xs1 = [cb[2]] + [e.bbox.x1 for e in labels if e.bbox]
        ys1 = [cb[3]] + [e.bbox.y1 for e in labels if e.bbox]
        fbox = BBox(x0=min(xs0), y0=min(ys0), x1=max(xs1), y1=max(ys1)).clamp(dw, dh)
        for e in labels:
            elements.remove(e)
        n += 1
        elements.append(Element(
            element_id=element_id(page_id, n), type=ElementType.DIAGRAM, page_id=page_id, page_number=page_number,
            reading_order=n, content="", figure=FigureData(labels=[e.content for e in labels]), bbox=fbox,
            bbox_source="pdf_text_layer", extraction_method="embedded_text", source=src.model_copy(),
            uncertainty_reasons=["vector diagram not described (deterministic route)"],
        ))
    _reorder_by_position(elements)
    link_captions(elements)
    return elements


def link_captions(elements: list[Element]) -> None:
    """Attach each caption to the nearest figure/diagram/image/table directly above or below it."""
    visuals = [e for e in elements if e.type in (ElementType.FIGURE, ElementType.DIAGRAM, ElementType.IMAGE,
                                                 ElementType.TABLE) and e.bbox]
    for cap in (e for e in elements if e.type == ElementType.CAPTION and e.bbox and not e.parent_id):
        best, best_d = None, 1e9
        for v in visuals:
            d = cap.bbox.y0 - v.bbox.y1 if cap.bbox.y0 >= v.bbox.y1 else v.bbox.y0 - cap.bbox.y1
            if -2 <= d < best_d and d < 60:
                best, best_d = v, d
        if best is not None:
            cap.parent_id = best.element_id
            if best.type == ElementType.TABLE and best.table and not best.table.caption:
                best.table.caption = cap.content
            if best.figure is not None and not best.figure.figure_label:
                best.figure.figure_label = cap.content.split(":")[0][:30]


def _overlap(a, b) -> float:
    """Intersection area over the smaller box's area."""
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1.0
    return (x1 - x0) * (y1 - y0) / small


def _center_inside(b: BBox, outer: BBox) -> bool:
    cx, cy = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
    return outer.x0 <= cx <= outer.x1 and outer.y0 <= cy <= outer.y1


def _reorder_by_position(elements: list[Element]) -> None:
    """Keep text-block order (column aware); insert images/tables by vertical position."""
    inserted = {ElementType.IMAGE, ElementType.TABLE, ElementType.DIAGRAM}
    merged = [e for e in sorted(elements, key=lambda e: e.reading_order) if e.type not in inserted]
    for el in sorted((e for e in elements if e.type in inserted), key=lambda e: e.bbox.y0 if e.bbox else 0):
        idx = next((i for i, t in enumerate(merged) if t.bbox and el.bbox and t.bbox.y0 > el.bbox.y0), len(merged))
        merged.insert(idx, el)
    for i, el in enumerate(merged, 1):
        el.reading_order = i
    elements.sort(key=lambda e: e.reading_order)


def _classify_block(b: dict, text: str, body: float, y0: float, y1: float, page_h: float, page_number: int):
    short = len(text) <= 160 and len(b["lines"]) <= 3
    in_header = y1 <= page_h * 0.07
    in_footer = y0 >= page_h * 0.93
    if (in_header or in_footer) and PAGE_NUM_RE.match(text):
        return ElementType.PAGE_NUMBER, None
    if in_header and short:
        return ElementType.HEADER, None
    if in_footer and short:
        return ElementType.FOOTER, None
    if short and CAPTION_RE.match(text):
        return ElementType.CAPTION, None
    ratio = b["size"] / body if body else 1.0
    if short and ratio >= 1.6 and page_number == 1:
        return ElementType.TITLE, 0
    if short and ratio >= 1.25:
        return ElementType.HEADING, 1
    if short and (ratio >= 1.1 or (b["bold"] and len(text) <= 100)):
        return ElementType.SUBHEADING, 2
    if b["mono"]:
        return ElementType.CODE, None
    if len(b["lines"]) >= 2 and all(BULLET_RE.match(ln) for ln in b["lines"]):
        return ElementType.LIST, None
    if b["size"] < body * 0.85 and y0 > page_h * 0.75:
        return ElementType.FOOTNOTE, None
    if re.match(r"^\[\d+\]\s", text):
        return ElementType.BIBLIOGRAPHY_ENTRY, None
    return ElementType.PARAGRAPH, None


def _find_tables(page) -> list[tuple[TableData, tuple[float, float, float, float]]]:
    """Born-digital tables via PyMuPDF's table finder (ruled lines strategy: few false positives).

    Header detection is not attempted (header_rows=0) because the finder cannot tell headers apart.
    """
    out = []
    try:
        found = page.find_tables(strategy="lines")
    except Exception:
        return out
    for t in found.tables:
        rows = t.extract()
        if not rows or len(rows) < 2 or max(len(r) for r in rows) < 2:
            continue
        data = TableData(rows=[[TableCell(text=(c or "").replace("\n", " ").strip()) for c in r] for r in rows],
                         header_rows=0)
        out.append((data, tuple(t.bbox)))
    return out
