"""DOCX export: editable structure (headings, lists, native tables, region crops, native equations).

Equations become native Office Math (OMML) by converting all of them with one ``pandoc``
call and lifting the ``m:oMathPara``/``m:oMath`` elements out of pandoc's output. Without
pandoc, or for an equation pandoc cannot parse, the LaTeX source is inserted verbatim in a
monospace run and a warning is recorded.
"""

from __future__ import annotations

import copy
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml import etree
from PIL import Image

from ..exceptions import ExportError
from ..schemas import Element, ElementType, Page
from ..schemas.elements import FURNITURE_TYPES, HEADING_TYPES, MATH_TYPES, VISUAL_TYPES
from .base import (
    ExportContext,
    ExportResult,
    ce_to_math,
    heading_level,
    is_unclear,
    layout_table,
    marks_label,
    math_latex,
    ordered_elements,
    ordered_pages,
    plain_text,
    region_image,
    unclear_text,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
EQ_MARKER = "SCHOLAROCREQ"
PANDOC_TIMEOUT_S = 120
MAX_IMAGE_WIDTH_PT = 432.0  # 6 inches
FALLBACK_IMAGE_DPI = 150.0
GREY = RGBColor(0x80, 0x80, 0x80)
MONO = "Courier New"


# ------------------------------------------------------------------ equations via pandoc
def latex_to_omml(latexes: list[str], warnings: list[str]) -> list[etree._Element | None]:
    """Convert LaTeX equations to OMML with a single pandoc run; None marks a failure."""
    results: list[etree._Element | None] = [None] * len(latexes)
    if not latexes:
        return results
    pandoc = shutil.which("pandoc")
    if pandoc is None:
        warnings.append(f"pandoc not found: {len(latexes)} equation(s) inserted as LaTeX source, not native equations")
        return results
    usable = [i for i, tex in enumerate(latexes) if "$" not in tex]
    parts = [f"{EQ_MARKER}{i}\n\n$${' '.join(latexes[i].split())}$$" for i in usable]
    with tempfile.TemporaryDirectory(prefix="scholarocr-eq-") as td:
        src, dst = Path(td) / "eq.md", Path(td) / "eq.docx"
        src.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
        try:
            proc = subprocess.run([pandoc, "-f", "markdown", "-t", "docx", "-o", str(dst), str(src)],
                                  capture_output=True, text=True, timeout=PANDOC_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            warnings.append("pandoc timed out converting equations; equations inserted as LaTeX source")
            return results
        if proc.returncode != 0 or not dst.is_file():
            warnings.append(f"pandoc failed (exit {proc.returncode}); equations inserted as LaTeX source")
            return results
        with zipfile.ZipFile(dst) as z:
            xml = z.read("word/document.xml")
    body = etree.fromstring(xml).find(f"{{{W_NS}}}body")
    current: int | None = None
    marker = re.compile(rf"^{EQ_MARKER}(\d+)$")
    for p in body.iter(f"{{{W_NS}}}p"):
        maths = p.findall(f"{{{M_NS}}}oMathPara") or p.findall(f"{{{M_NS}}}oMath")
        if not maths:
            m = marker.match("".join(t.text or "" for t in p.iter(f"{{{W_NS}}}t")).strip())
            current = int(m.group(1)) if m else None
            continue
        if current is not None and current < len(results):
            results[current] = maths[0]
        current = None
    return results


# ------------------------------------------------------------------ builders
class _DocxBuilder:
    def __init__(self, ctx: ExportContext, warnings: list[str]):
        self.ctx = ctx
        self.warnings = warnings
        self.doc = docx.Document()
        self.equations: list[tuple[Paragraph, str]] = []
        self.images = 0
        self.include_furniture = ctx.flag("include_furniture", False)

    # -- run helpers
    def _unclear_run(self, p: Paragraph, text: str) -> None:
        run = p.add_run(f"[unclear: {' '.join(text.split())}]")
        run.bold = True
        run.font.highlight_color = WD_COLOR_INDEX.YELLOW

    def _small(self, text: str, *, italic: bool = False, grey: bool = False, size: float = 8) -> Paragraph:
        p = self.doc.add_paragraph()
        run = p.add_run(text)
        run.font.size = Pt(size)
        run.italic = italic
        if grey:
            run.font.color.rgb = GREY
        return p

    def _note_if_unresolved(self, el: Element) -> None:
        if is_unclear(el):
            self._unclear_run(self.doc.add_paragraph(), "; ".join(el.uncertainty_reasons) or "reading not verified")

    # -- elements
    def page_marker(self, page: Page) -> None:
        self._small(f"Page {page.page_number}", grey=True)

    def add(self, page: Page, el: Element) -> None:
        t = el.type
        if t in FURNITURE_TYPES and not self.include_furniture:
            return
        if t == ElementType.UNCERTAIN:
            self._unclear_run(self.doc.add_paragraph(), unclear_text(el))
        elif t in HEADING_TYPES:
            text = " ".join(el.content.split())
            h = self.doc.add_heading("", level=min(heading_level(el), 9))
            if is_unclear(el):
                self._unclear_run(h, text or unclear_text(el))
            else:
                h.add_run(text)
        elif t in MATH_TYPES:
            self._equation(el)
        elif t == ElementType.TABLE:
            self._table(el)
        elif t in VISUAL_TYPES:
            self._figure(page, el)
        elif t == ElementType.LIST:
            self._list(el)
        elif t == ElementType.CODE:
            self._note_if_unresolved(el)
            p = self.doc.add_paragraph()
            for i, line in enumerate(el.content.rstrip().split("\n")):
                if i:
                    p.add_run().add_break()
                p.add_run(line).font.name = MONO
        elif t == ElementType.CAPTION:
            p = self.doc.add_paragraph(style="Caption")
            self._text_runs(p, el, italic=True)
        elif t == ElementType.FOOTNOTE:
            self._text_runs(self.doc.add_paragraph(), el, italic=True, size=8)
        elif t in FURNITURE_TYPES:
            self._small(f"[{t.value.replace('_', ' ')}] {' '.join(el.content.split())}", grey=True)
        else:
            self._text_runs(self.doc.add_paragraph(), el)

    def _text_runs(self, p: Paragraph, el: Element, *, italic: bool = False, size: float | None = None) -> None:
        q = el.question
        if q and q.number:
            p.add_run(f"Q{q.number} ").bold = True
        text = plain_text(el)
        if is_unclear(el):
            self._unclear_run(p, text or unclear_text(el))
        else:
            for i, line in enumerate(text.split("\n")):
                if i:
                    p.add_run().add_break()
                run = p.add_run(line)
                run.italic = italic or None
                if size:
                    run.font.size = Pt(size)
        if q and q.marks and marks_label(q.marks):
            p.add_run(f"  {marks_label(q.marks)}").italic = True

    def _list(self, el: Element) -> None:
        items = [i for i in (el.items if el.items else plain_text(el).split("\n")) if i.strip()]
        q = el.question
        if q and (q.number or q.marks):
            p = self.doc.add_paragraph()
            if q.number:
                p.add_run(f"Q{q.number}").bold = True
            if q.marks and marks_label(q.marks):
                p.add_run(f"  {marks_label(q.marks)}").italic = True
        for item in items:
            p = self.doc.add_paragraph(style="List Bullet")
            if is_unclear(el):
                self._unclear_run(p, item)
            else:
                p.add_run(" ".join(item.split()))

    def _equation(self, el: Element) -> None:
        self._note_if_unresolved(el)
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        latex = math_latex(el)
        if latex is None:
            p.add_run(" ".join(el.content.split()))
            return
        self.equations.append((p, ce_to_math(latex) if r"\ce" in latex else latex))

    def _table(self, el: Element) -> None:
        self._note_if_unresolved(el)
        if el.table is None:
            self._text_runs(self.doc.add_paragraph(), el)
            return
        head = " ".join(x for x in (el.table.label, el.table.caption) if x)
        if head:
            self.doc.add_paragraph(head, style="Caption")
        layout = layout_table(el.table)
        if layout.n_rows == 0 or layout.n_cols == 0:
            self._unclear_run(self.doc.add_paragraph(), "empty table")
            return
        table: Table = self.doc.add_table(rows=layout.n_rows, cols=layout.n_cols)
        table.style = "Table Grid"
        for pc in layout.cells:
            cell = table.cell(pc.row, pc.col)
            if pc.row_span > 1 or pc.col_span > 1:
                cell = cell.merge(table.cell(pc.row + pc.row_span - 1, pc.col + pc.col_span - 1))
            para = cell.paragraphs[0]
            text = " ".join(pc.cell.text.split())
            if pc.cell.uncertain:
                self._unclear_run(para, text or "cell")
                continue
            if text:
                run = para.add_run(text)
            elif pc.cell.latex:
                run = para.add_run(pc.cell.latex)
                run.font.name = MONO
            else:
                continue
            run.bold = pc.row < layout.header_rows or pc.cell.is_header or None
        for r in range(layout.header_rows):
            tr_pr = table.rows[r]._tr.get_or_add_trPr()
            hdr = OxmlElement("w:tblHeader")
            hdr.set(qn("w:val"), "true")
            tr_pr.append(hdr)

    def _figure(self, page: Page, el: Element) -> None:
        self._note_if_unresolved(el)
        rel = region_image(page, el)
        path = None
        if rel:
            try:
                path = self.ctx.resolve(rel)
            except ExportError as e:
                self.warnings.append(str(e))
            if path is not None and not path.is_file():
                self.warnings.append(f"Region image missing on disk: {rel}")
                path = None
        if path is not None:
            width_pt = el.bbox.width if el.bbox is not None and el.bbox.width > 0 else None
            if width_pt is None:
                with Image.open(path) as im:
                    width_pt = im.width * 72.0 / float(im.info.get("dpi", (FALLBACK_IMAGE_DPI,))[0] or FALLBACK_IMAGE_DPI)
            self.doc.add_picture(str(path), width=Pt(min(width_pt, MAX_IMAGE_WIDTH_PT)))
            self.images += 1
        fig = el.figure
        desc = " ".join((fig.description if fig and fig.description else el.content).split())
        p = self.doc.add_paragraph()
        label = (fig.figure_label if fig and fig.figure_label else None) or ("" if path else f"[{el.type.value}]")
        if label:
            p.add_run(label + (" " if desc else "")).bold = True
        if desc:
            p.add_run(desc).italic = True
        if fig and fig.labels:
            self._small("Labels: " + ", ".join(" ".join(x.split()) for x in fig.labels), italic=True, size=9)

    # -- finish
    def insert_equations(self) -> tuple[int, int]:
        omml = latex_to_omml([tex for _, tex in self.equations], self.warnings)
        native = failed = 0
        for (p, tex), node in zip(self.equations, omml):
            if node is not None:
                p._p.append(copy.deepcopy(node))
                native += 1
            else:
                run = p.add_run(tex)
                run.font.name = MONO
                failed += 1
        if failed and shutil.which("pandoc") is not None:
            self.warnings.append(f"{failed} equation(s) could not be converted to native Word equations; "
                                 "inserted as LaTeX source")
        return native, failed


def export_docx(ctx: ExportContext) -> ExportResult:
    warnings: list[str] = []
    builder = _DocxBuilder(ctx, warnings)
    markers = ctx.flag("page_markers", True)
    if ctx.document.title:
        builder.doc.core_properties.title = ctx.document.title
    for page in ordered_pages(ctx.document):
        if markers:
            builder.page_marker(page)
        for el in ordered_elements(page):
            builder.add(page, el)
    native, fallback = builder.insert_equations()
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    out = ctx.out_dir / "document.docx"
    builder.doc.save(str(out))

    reopened = docx.Document(str(out))
    styles = [p.style.name for p in reopened.paragraphs if p.style is not None]
    body = reopened.element.body
    verification = {
        "headings": sum(1 for s in styles if s.startswith("Heading") or s == "Title"),
        "tables": len(reopened.tables),
        "images": len(reopened.inline_shapes),
        "images_expected": builder.images,
        "equations_expected": len(builder.equations),
        "native_equations": sum(1 for _ in body.iter(f"{{{M_NS}}}oMath")),
        "equations_fallback_latex": fallback,
        "page_markers": markers,
    }
    if verification["native_equations"] != native:
        warnings.append(f"Native equation count after reopening ({verification['native_equations']}) "
                        f"differs from inserted ({native})")
    return ExportResult(format="docx", primary_file=str(out), files=[str(out)], warnings=warnings,
                        verification=verification)
