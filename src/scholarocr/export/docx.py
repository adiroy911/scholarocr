"""DOCX export: editable structure (headings, lists, native tables, region crops, native equations).

Equations become native Office Math (OMML) by converting all of them with one ``pandoc``
call and lifting the ``m:oMathPara``/``m:oMath`` elements out of pandoc's output. Inline
``$...$`` math inside text goes through the same call and becomes inline OMML. pandoc is
optional: without it (or for a formula pandoc cannot parse) display equations are inserted as
LaTeX source in a monospace run, inline math as readable text (``x²``, ``γ₀``, ``SO₄²⁻``), and a
warning is recorded.
"""

from __future__ import annotations

import copy
import re
from typing import Any
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

from ..schemas import Element, ElementType, Page
from ..schemas.elements import FURNITURE_TYPES, HEADING_TYPES, MATH_TYPES, VISUAL_TYPES
from .base import (
    ExportContext,
    ExportResult,
    ce_to_math,
    export_variant_context,
    figure_image_path,
    figure_preference,
    redraw_note,
    heading_level,
    is_unclear,
    latex_to_readable,
    layout_table,
    marks_label,
    math_latex,
    ordered_elements,
    ordered_pages,
    plain_text,
    split_inline_math,
    starts_with_question_number,
    svg_to_png,
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
MATH_FONT = "Cambria Math"


# ------------------------------------------------------------------ equations via pandoc
def latex_to_omml(latexes: list[str], warnings: list[str], inline: list[bool] | None = None
                  ) -> list[etree._Element | None]:
    """Convert LaTeX to OMML with a single pandoc run; None marks a failure. ``inline[i]`` marks inline
    math (``$...$``, becomes ``m:oMath``) instead of a display equation (``$$...$$``, ``m:oMathPara``)."""
    results: list[etree._Element | None] = [None] * len(latexes)
    if not latexes:
        return results
    inline = inline or [False] * len(latexes)
    pandoc = shutil.which("pandoc")
    if pandoc is None:
        n_inline = sum(1 for x in inline if x)
        n_display = len(latexes) - n_inline
        what = ", ".join(x for x in (f"{n_display} equation(s) inserted as LaTeX source" if n_display else "",
                                     f"{n_inline} inline formula(s) as plain text" if n_inline else "") if x)
        warnings.append(f"pandoc not found: {what}, not native Word equations (install pandoc for native "
                        "equations)")
        return results
    usable = [i for i, tex in enumerate(latexes) if "$" not in tex]
    parts = [f"{EQ_MARKER}{i}\n\n" + (f"${' '.join(latexes[i].split())}$" if inline[i]
                                       else f"$${' '.join(latexes[i].split())}$$") for i in usable]
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
        self.inline_math: list[tuple[Any, str]] = []  # (placeholder run, LaTeX) for $...$ inside text
        self.inline_native = 0
        self.images = 0
        self.include_furniture = ctx.flag("include_furniture", False)
        self.figure_prefer = figure_preference(ctx)
        self._tmp = tempfile.TemporaryDirectory(prefix="scholarocr-docx-")

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
                self._rich(h, text)
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
        text = plain_text(el)
        if q and q.number and not starts_with_question_number(q.number, text):
            p.add_run(f"Q{q.number} ").bold = True
        if is_unclear(el):
            self._unclear_run(p, text or unclear_text(el))
        else:
            for i, line in enumerate(text.split("\n")):
                if i:
                    p.add_run().add_break()
                self._rich(p, line, italic=italic, size=size)
        if q and q.marks and marks_label(q.marks):
            p.add_run(f"  {marks_label(q.marks)}").italic = True

    def _rich(self, p: Paragraph, line: str, *, italic: bool = False, size: float | None = None) -> None:
        """Runs for one line of text; inline $...$ math becomes a placeholder filled by insert_equations."""
        for segment, is_math in split_inline_math(line):
            run = p.add_run(segment)
            run.italic = italic or None
            if size:
                run.font.size = Pt(size)
            if is_math:
                self.inline_math.append((run, segment))

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
                self._rich(p, " ".join(item.split()))

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

    def _text_width_pt(self) -> float:
        sec = self.doc.sections[-1]
        try:
            return float((sec.page_width - sec.left_margin - sec.right_margin) / 12700)  # EMU -> pt
        except TypeError:
            return MAX_IMAGE_WIDTH_PT

    def _figure(self, page: Page, el: Element) -> None:
        self._note_if_unresolved(el)
        path = figure_image_path(self.ctx.doc_dir, el, self.figure_prefer)
        if note := redraw_note(el, self.figure_prefer):
            self.warnings.append(note)
        if path is None and (el.source.region_image or (el.figure and (el.figure.image_clean or el.figure.svg))):
            self.warnings.append(f"Figure image missing on disk for {el.element_id}")
        if path is not None and path.suffix.lower() == ".svg":
            try:  # Word cannot take SVG through python-docx: insert a high-resolution rendering
                path = svg_to_png(path, Path(self._tmp.name) / f"{el.element_id}.png")
            except Exception as e:  # noqa: BLE001 - fall back to the cleaned crop
                self.warnings.append(f"redrawn figure {el.element_id} could not be rendered ({e.__class__.__name__}); "
                                     "used the cleaned crop")
                path = figure_image_path(self.ctx.doc_dir, el, "clean")
        if path is not None:
            # Same share of the text width as the figure had of the source page width.
            text_w = self._text_width_pt()
            if el.bbox is not None and el.bbox.width > 0 and page.width_pt > 0:
                width_pt = el.bbox.width / page.width_pt * text_w
            else:
                with Image.open(path) as im:
                    width_pt = im.width * 72.0 / float(im.info.get("dpi", (FALLBACK_IMAGE_DPI,))[0] or FALLBACK_IMAGE_DPI)
            self.doc.add_picture(str(path), width=Pt(max(36.0, min(width_pt, text_w))))
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
        inline_tex = [ce_to_math(tex) if r"\ce" in tex else tex for _, tex in self.inline_math]
        omml = latex_to_omml([tex for _, tex in self.equations] + inline_tex, self.warnings,
                             inline=[False] * len(self.equations) + [True] * len(inline_tex))
        inline_nodes = omml[len(self.equations):]
        omml = omml[:len(self.equations)]
        for (run, tex), node in zip(self.inline_math, inline_nodes):
            if node is not None:
                run._r.addprevious(copy.deepcopy(node))
                run._r.getparent().remove(run._r)
                self.inline_native += 1
            else:  # readable fallback: x², γ₀, SO₄²⁻ instead of raw LaTeX with dollar signs
                run.text = latex_to_readable(tex)
                run.font.name = MATH_FONT
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
    ctx, variant = export_variant_context(ctx)
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
    builder._tmp.cleanup()

    reopened = docx.Document(str(out))
    styles = [p.style.name for p in reopened.paragraphs if p.style is not None]
    body = reopened.element.body
    verification = {
        "headings": sum(1 for s in styles if s.startswith("Heading") or s == "Title"),
        "tables": len(reopened.tables),
        "images": len(reopened.inline_shapes),
        "images_expected": builder.images,
        "equations_expected": len(builder.equations),
        "native_equations": sum(1 for _ in body.iter(f"{{{M_NS}}}oMath")) - builder.inline_native,
        "equations_fallback_latex": fallback,
        "inline_math": len(builder.inline_math),
        "inline_math_native": builder.inline_native,
        "page_markers": markers,
        "text_variant": variant,
        "figure_variant": builder.figure_prefer,
    }
    if verification["native_equations"] != native:
        warnings.append(f"Native equation count after reopening ({verification['native_equations']}) "
                        f"differs from inserted ({native})")
    return ExportResult(format="docx", primary_file=str(out), files=[str(out)], warnings=warnings,
                        verification=verification)
