"""Hand-built IR samples for export tests (no OCR, no Gemini).

``build_sample`` creates a document directory with preserved sources, page renders and a
figure crop, plus a Document/Manifest pair covering every element type the exporters
handle. Page 1 is a digital PDF page (elements read from the text layer); page 2 is a PNG
scan (elements read by the model, with estimated boxes).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from scholarocr.schemas import (
    BBox,
    Document,
    Element,
    ElementType,
    FigureData,
    Manifest,
    ManifestEntry,
    Page,
    PageStatus,
    QuestionInfo,
    Role,
    SourceFile,
    SourceRef,
    TableCell,
    TableData,
    Uncertainty,
)
from scholarocr.schemas.document import Route

DOC_ID = "doc_0123456789abcdef"
SCAN_DPI = 150
SCAN_PX = (1240, 1754)  # A4 at 150 dpi
PDF_SIZE = (595.0, 842.0)
QUADRATIC = r"\frac{-b \pm \sqrt{b^2-4ac}}{2a}"
AMMONIA = r"\ce{N2 + 3H2 <=> 2NH3}"
UNICODE_TEXT = "Unicode symbols Δ, θ, ∫ and the greeting Привет appear here."
DISTINCTIVE = "Photosynthesis"
UNPLACED_WORD = "Zymurgy"

_FONT_FILES = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf",
               "/Library/Fonts/Arial Unicode.ttf")


def _font(size: int) -> ImageFont.ImageFont:
    for f in _FONT_FILES:
        if Path(f).is_file():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _page_id(n: int) -> str:
    return f"{DOC_ID}-p{n:04d}"


class _Elements:
    """Small factory assigning ids/reading order per page."""

    def __init__(self, page_number: int, source_file: str, method: str, bbox_source: str):
        self.page_number = page_number
        self.source_file = source_file
        self.method = method
        self.bbox_source = bbox_source
        self.items: list[Element] = []

    def eid(self, index: int) -> str:
        return f"{_page_id(self.page_number)}-e{index:03d}"

    def add(self, type_: ElementType, content: str = "", bbox: tuple[float, float, float, float] | None = None,
            region_image: str | None = None, **kw) -> Element:
        idx = len(self.items)
        el = Element(
            element_id=self.eid(idx), type=type_, page_id=_page_id(self.page_number), page_number=self.page_number,
            reading_order=idx, content=content, bbox=BBox(x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3]) if bbox else None,
            bbox_source=self.bbox_source if bbox else None, extraction_method=self.method,
            source=SourceRef(document_id=DOC_ID, page_number=self.page_number, source_file=self.source_file,
                             source_page_index=0, page_image=f"pages/{self.page_number:04d}/original.png",
                             region_image=region_image),
            **kw,
        )
        self.items.append(el)
        return el


def _make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    p = doc.new_page(width=PDF_SIZE[0], height=PDF_SIZE[1])
    p.insert_text((72, 90), "Quadratic Equations", fontsize=22, fontname="hebo")
    p.insert_text((72, 130), "1 Introduction", fontsize=16, fontname="hebo")
    p.insert_textbox(pymupdf.Rect(72, 145, 523, 200),
                     "A quadratic equation has degree two. Its roots follow from the quadratic formula.", fontsize=11)
    p.insert_text((200, 230), "x = (-b ± sqrt(b^2 - 4ac)) / 2a", fontsize=12)
    p.insert_text((90, 270), "- real coefficients", fontsize=11)
    p.insert_text((90, 288), "- leading coefficient non-zero", fontsize=11)
    p.insert_text((72, 780), "1 The discriminant decides the nature of the roots.", fontsize=8)
    p.insert_text((290, 815), "1", fontsize=9)
    doc.save(path)
    doc.close()


def _make_scan(path: Path) -> None:
    img = Image.new("RGB", SCAN_PX, "white")
    d = ImageDraw.Draw(img)
    s = SCAN_DPI / 72.0
    d.text((72 * s, 60 * s), "2 Chemistry of Ammonia", fill="black", font=_font(36))
    d.text((72 * s, 100 * s), f"{DISTINCTIVE} converts light energy into chemical energy.", fill="black", font=_font(22))
    d.text((72 * s, 125 * s), UNICODE_TEXT, fill="black", font=_font(22))
    d.text((150 * s, 160 * s), "N2 + 3H2 <=> 2NH3", fill="black", font=_font(26))
    d.rectangle((72 * s, 200 * s, 400 * s, 290 * s), outline="black", width=3)  # table
    d.rectangle((72 * s, 320 * s, 330 * s, 480 * s), outline="black", width=3)  # figure frame
    d.line((90 * s, 460 * s, 310 * s, 340 * s), fill="black", width=4)
    d.text((72 * s, 520 * s), "3. Calculate Kc for the reaction. [5]", fill="black", font=_font(22))
    d.text((72 * s, 780 * s), "* Measured at STP.", fill="black", font=_font(16))
    img.save(path, dpi=(SCAN_DPI, SCAN_DPI))


def build_sample(tmp_dir: Path) -> tuple[Document, Manifest, Path]:
    """Create the sample document directory and return (document, manifest, doc_dir)."""
    doc_dir = Path(tmp_dir) / DOC_ID
    (doc_dir / "source").mkdir(parents=True, exist_ok=True)
    pdf_path = doc_dir / "source" / "00_digital.pdf"
    scan_path = doc_dir / "source" / "01_scan.png"
    _make_pdf(pdf_path)
    _make_scan(scan_path)

    p1_dir, p2_dir = doc_dir / "pages" / "0001", doc_dir / "pages" / "0002"
    (p2_dir / "regions").mkdir(parents=True, exist_ok=True)
    p1_dir.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(pdf_path) as pdf:
        pdf[0].get_pixmap(dpi=72).save(p1_dir / "original.png")
    scan = Image.open(scan_path)
    scan.save(p2_dir / "original.png")

    # ---------------- page 1: digital PDF, text-layer extraction
    e1 = _Elements(1, "digital.pdf", "embedded_text", "pdf_text_layer")
    e1.add(ElementType.TITLE, "Quadratic Equations", (72, 70, 300, 95), level=1)
    e1.add(ElementType.HEADING, "1 Introduction", (72, 115, 200, 135), level=1, role=Role.SECTION)
    e1.add(ElementType.PARAGRAPH, "A quadratic equation has degree two. Its roots follow from the quadratic formula.",
           (72, 145, 523, 175))
    e1.add(ElementType.EQUATION, "x = (-b ± sqrt(b^2 - 4ac)) / 2a", (200, 218, 400, 235), latex=QUADRATIC)
    e1.add(ElementType.LIST, "", (90, 258, 300, 292), items=["real coefficients", "leading coefficient non-zero"])
    e1.add(ElementType.FOOTNOTE, "1 The discriminant decides the nature of the roots.", (72, 772, 330, 785))
    e1.add(ElementType.PAGE_NUMBER, "1", (288, 806, 296, 818))

    # ---------------- page 2: PNG scan, model extraction (estimated boxes)
    s = 72.0 / SCAN_DPI  # pt per px
    e2 = _Elements(2, "scan.png", "gemini", "model_estimate")
    e2.add(ElementType.SUBHEADING, "2 Chemistry of Ammonia", (72, 58, 330, 80), level=2)
    e2.add(ElementType.PARAGRAPH, f"{DISTINCTIVE} converts light energy into chemical energy.\n{UNICODE_TEXT}",
           (72, 98, 560, 140), language="en")
    e2.add(ElementType.CHEMICAL_EQUATION, "N2 + 3H2 ⇌ 2NH3", (150, 158, 360, 180), latex=AMMONIA)
    table = TableData(
        header_rows=2, label="Table 1", caption="Measured values",
        rows=[
            [TableCell(text="Quantity", row_span=2, is_header=True), TableCell(text="Values", col_span=2, is_header=True)],
            [TableCell(text="Trial 1", is_header=True), TableCell(text="Trial 2", is_header=True)],
            [TableCell(text="Mass (g)"), TableCell(text="1.25"), TableCell(text="1.30")],
            [TableCell(text="Volume (L)"), TableCell(text="2.0"), TableCell(text="2.l", uncertain=True)],
        ],
    )
    e2.add(ElementType.TABLE, "", (72, 200, 400, 290), table=table, uncertainty=Uncertainty.MEDIUM)
    fig_id = e2.eid(len(e2.items))
    crop_rel = f"pages/0002/regions/{fig_id}.png"
    scan.crop((int(72 / s), int(320 / s), int(330 / s), int(480 / s))).save(doc_dir / crop_rel)
    e2.add(ElementType.FIGURE, "", (72, 320, 330, 480), region_image=crop_rel,
           figure=FigureData(description="Graph of reaction rate against temperature", labels=["Rate", "T (K)"],
                             figure_label="Fig. 1", kind="graph"))
    e2.add(ElementType.CAPTION, "Fig. 1: Reaction rate versus temperature", (72, 484, 330, 498), parent_id=fig_id)
    e2.add(ElementType.PARAGRAPH, "Calculate the equilibrium constant Kc for the reaction.", (72, 515, 520, 535),
           role=Role.QUESTION, question=QuestionInfo(number="3", marks="5"))
    e2.add(ElementType.UNCERTAIN, "", (72, 560, 400, 600), uncertainty=Uncertainty.UNRESOLVED,
           uncertainty_reasons=["illegible handwriting in margin"])
    e2.add(ElementType.PARAGRAPH, f"{UNPLACED_WORD} appendix note without a position.")
    e2.add(ElementType.BIBLIOGRAPHY_ENTRY, "[1] Atkins, P. Physical Chemistry & Kinetics_2 (100% revised). Oxford, 2018.",
           (72, 700, 520, 715))
    e2.add(ElementType.FOOTNOTE, "* Measured at STP.", (72, 776, 200, 790))
    e2.add(ElementType.PAGE_NUMBER, "2", (288, 806, 296, 818))

    w2, h2 = SCAN_PX[0] * 72.0 / SCAN_DPI, SCAN_PX[1] * 72.0 / SCAN_DPI
    now = "2026-11-01T00:00:00+00:00"
    pages = [
        Page(page_id=_page_id(1), document_id=DOC_ID, page_number=1, width_pt=PDF_SIZE[0], height_pt=PDF_SIZE[1],
             status=PageStatus.DONE, route=Route.EMBEDDED_TEXT, original_image="pages/0001/original.png",
             elements=e1.items),
        Page(page_id=_page_id(2), document_id=DOC_ID, page_number=2, width_pt=w2, height_pt=h2,
             status=PageStatus.DONE, route=Route.VISUAL_MATH, original_image="pages/0002/original.png",
             elements=e2.items, model="gemini-test"),
    ]
    document = Document(document_id=DOC_ID, title="Sample Science Notes", original_filenames=["digital.pdf", "scan.png"],
                        page_count=2, created_at=now, extraction_version="test", schema_version="1", status="done",
                        pages=pages)
    sources = [
        SourceFile(index=0, original_path="/input/digital.pdf", filename="digital.pdf",
                   stored_path="source/00_digital.pdf", sha256=_sha(pdf_path), size_bytes=pdf_path.stat().st_size,
                   media_type="application/pdf", page_count=1),
        SourceFile(index=1, original_path="/input/scan.png", filename="scan.png", stored_path="source/01_scan.png",
                   sha256=_sha(scan_path), size_bytes=scan_path.stat().st_size, media_type="image/png", page_count=1),
    ]
    entries = [
        ManifestEntry(page_number=1, page_id=_page_id(1), source_file="digital.pdf", source_file_index=0,
                      source_page_index=0, width_pt=PDF_SIZE[0], height_pt=PDF_SIZE[1], content_hash="h1"),
        ManifestEntry(page_number=2, page_id=_page_id(2), source_file="scan.png", source_file_index=1,
                      source_page_index=0, width_pt=w2, height_pt=h2, image_dpi=float(SCAN_DPI), content_hash="h2"),
    ]
    return document, Manifest(document_id=DOC_ID, sources=sources, pages=entries), doc_dir


def build_rotated_pdf_sample(tmp_dir: Path, rotation: int = 90) -> tuple[Document, Manifest, Path]:
    """A one-page scanned PDF (no text layer) with /Rotate set and one model-read paragraph.

    The element bbox is in displayed (rotated) coordinates, as the IR specifies."""
    doc_dir = Path(tmp_dir) / DOC_ID
    (doc_dir / "source").mkdir(parents=True, exist_ok=True)
    pdf_path = doc_dir / "source" / "00_rotated.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page(width=300, height=500)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 100), False)
    pix.clear_with(200)
    page.insert_image(page.rect, pixmap=pix)
    page.set_rotation(rotation)
    disp_w, disp_h = page.rect.width, page.rect.height
    pdf.save(pdf_path)
    pdf.close()
    el = _Elements(1, "rotated.pdf", "gemini", "model_estimate")
    el.add(ElementType.PARAGRAPH, "Rotated searchable sentence", (40, 50, 260, 80))
    document = Document(
        document_id=DOC_ID, original_filenames=["rotated.pdf"], page_count=1, created_at="2026-11-01T00:00:00+00:00",
        extraction_version="test", schema_version="1",
        pages=[Page(page_id=_page_id(1), document_id=DOC_ID, page_number=1, width_pt=disp_w, height_pt=disp_h,
                    status=PageStatus.DONE, elements=el.items)],
    )
    manifest = Manifest(
        document_id=DOC_ID,
        sources=[SourceFile(index=0, original_path="/input/rotated.pdf", filename="rotated.pdf",
                            stored_path="source/00_rotated.pdf", sha256=_sha(pdf_path),
                            size_bytes=pdf_path.stat().st_size, media_type="application/pdf", page_count=1)],
        pages=[ManifestEntry(page_number=1, page_id=_page_id(1), source_file="rotated.pdf", source_file_index=0,
                             source_page_index=0, width_pt=disp_w, height_pt=disp_h, rotation=rotation,
                             content_hash="r1")],
    )
    return document, manifest, doc_dir
