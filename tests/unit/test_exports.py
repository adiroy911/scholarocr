"""Export engine tests. All exports run from a hand-built IR; nothing calls Gemini."""

from __future__ import annotations

import ast
import csv
import shutil
import subprocess
import sys
from pathlib import Path

import docx as python_docx
import numpy as np
import pymupdf
import pytest
from openpyxl import load_workbook
from PIL import Image

import scholarocr.export.docx as docx_mod
import scholarocr.export.searchable_pdf as spdf
from scholarocr.exceptions import ExportError
from scholarocr.export import (
    FORMATS,
    ExportContext,
    document_to_markdown,
    export_document,
)
from scholarocr.export.base import ce_to_math, layout_table
from scholarocr.ingestion.validation import DOCX_TYPE
from scholarocr.schemas import ElementType, ManifestEntry, SourceFile, Uncertainty
from tests.fixtures.ir_samples import (
    AMMONIA,
    DISTINCTIVE,
    QUADRATIC,
    UNPLACED_WORD,
    build_rotated_pdf_sample,
    build_sample,
)

M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"


@pytest.fixture
def sample(tmp_path):
    return build_sample(tmp_path / "docs")


def make_ctx(sample, tmp_path, name: str, **options) -> ExportContext:
    document, manifest, doc_dir = sample
    return ExportContext(document=document, manifest=manifest, doc_dir=doc_dir, out_dir=tmp_path / "out" / name,
                         options=options)


def element(document, type_: ElementType):
    return next(e for p in document.pages for e in p.elements if e.type == type_)


# ------------------------------------------------------------------ dispatch / isolation
def test_unknown_format_raises(sample, tmp_path):
    with pytest.raises(ExportError, match="Unknown export format"):
        export_document(make_ctx(sample, tmp_path, "x"), "rtf")


def test_formats_constant():
    assert FORMATS == ("json", "markdown", "searchable-pdf", "layout-pdf", "docx", "latex", "csv", "xlsx")


def test_export_package_never_touches_intelligence():
    pkg = Path(__file__).resolve().parents[2] / "src" / "scholarocr" / "export"
    for f in pkg.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert "intelligence" not in (node.module or ""), f
            elif isinstance(node, ast.Import):
                assert all("intelligence" not in a.name for a in node.names), f
    code = ("import sys, scholarocr.export; "
            "sys.exit(any(m.startswith('scholarocr.intelligence') for m in sys.modules))")
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


# ------------------------------------------------------------------ JSON
def test_json_roundtrip(sample, tmp_path):
    res = export_document(make_ctx(sample, tmp_path, "json"), "json")
    assert Path(res.primary_file).is_file()
    v = res.verification
    assert v["schema_valid"] and v["elements_expected"] == v["elements_found"] == 19
    assert res.warnings == []


# ------------------------------------------------------------------ Markdown
def test_markdown_export_file(sample, tmp_path):
    res = export_document(make_ctx(sample, tmp_path, "md"), "markdown")
    text = Path(res.primary_file).read_text(encoding="utf-8")
    assert "<!-- page 1 -->" in text and "<!-- page 2 -->" in text
    assert "# Quadratic Equations" in text and "## 1 Introduction" in text and "### 2 Chemistry of Ammonia" in text
    assert f"$$\n{QUADRATIC}\n$$" in text
    assert f"$$\n{AMMONIA}\n$$" in text
    assert "⟦unclear: illegible handwriting in margin⟧" in text
    assert "⟦unclear: 2.l⟧" in text  # uncertain table cell
    assert "**Q3** Calculate the equilibrium constant Kc for the reaction. *[5 marks]*" in text
    assert "| Quantity | Values |  |" in text and "|  | **Trial 1** | **Trial 2** |" in text
    assert "- real coefficients" in text
    assert "*Fig. 1: Reaction rate versus temperature*" in text
    assert "> *Footnote:* 1 The discriminant" in text
    assert "page number" not in text  # furniture omitted by default
    assert "Привет" in text
    v = res.verification
    assert v["page_markers"] == 2 and v["images_linked"] == 1 and v["broken_image_links"] == []
    crop = next(f for f in res.files if f.endswith(".png"))
    assert Path(crop).is_file() and f"](assets/{Path(crop).name})" in text


def test_markdown_function_filters_and_ids(sample):
    document = sample[0]
    only_p2 = document_to_markdown(document, page_numbers={2})
    assert "Quadratic Equations" not in only_p2 and DISTINCTIVE in only_p2
    tables = document_to_markdown(document, element_types={ElementType.TABLE})
    assert "| Quantity |" in tables and DISTINCTIVE not in tables
    with_ids = document_to_markdown(document, include_ids=True)
    eq = element(document, ElementType.EQUATION)
    assert f"<!-- el:{eq.element_id} -->" in with_ids
    assert "*page number: 2*" in document_to_markdown(document, include_furniture=True)
    # figure link falls back to the stored relative crop path without an asset prefix
    fig = element(document, ElementType.FIGURE)
    assert f"]({fig.source.region_image})" in document_to_markdown(document)
    assert document_to_markdown(document) == document_to_markdown(document)  # deterministic


def test_markdown_unresolved_text_never_presented_as_certain(sample):
    document = sample[0]
    para = element(document, ElementType.PARAGRAPH)
    para.uncertainty = Uncertainty.UNRESOLVED
    md = document_to_markdown(document)
    assert "⟦unclear: A quadratic equation has degree two." in md
    eq = element(document, ElementType.EQUATION)
    eq.uncertainty = Uncertainty.UNRESOLVED
    eq.uncertainty_reasons = ["superscript unreadable"]
    assert f"⟦unclear: superscript unreadable⟧\n$$\n{QUADRATIC}" in document_to_markdown(document)


# ------------------------------------------------------------------ searchable PDF
def test_searchable_pdf_preserves_pages_and_adds_text(sample, tmp_path):
    _, manifest, doc_dir = sample
    res = export_document(make_ctx(sample, tmp_path, "pdf"), "searchable-pdf")
    v = res.verification
    assert v["page_count"] == 2 and v["page_count_ok"] and v["page_sizes_match"] and v["appearance_preserved"]
    assert v["text_extractable"] is True
    assert v["overall_recall"] >= 0.9
    assert v["elements_without_position"] == 1
    assert any("1 elements without position" in w for w in res.warnings)

    with pymupdf.open(res.primary_file) as out, pymupdf.open(doc_dir / "source" / "00_digital.pdf") as src:
        assert out.page_count == 2
        assert abs(out[0].rect.width - src[0].rect.width) < 1 and abs(out[0].rect.height - src[0].rect.height) < 1
        e2 = manifest.pages[1]
        assert abs(out[1].rect.width - e2.width_pt) < 1 and abs(out[1].rect.height - e2.height_pt) < 1
        # page 1 came from the text layer: copied as-is, not doubled
        p1 = out[0].get_text()
        assert p1.count("Quadratic Equations") == 1
        assert out[0].get_pixmap(dpi=50).samples == src[0].get_pixmap(dpi=50).samples
        # page 2: original scan image kept, invisible text extractable (incl. Unicode)
        assert out[1].get_images()
        p2 = out[1].get_text()
        for word in (DISTINCTIVE, "Привет", "θ", "Kc", UNPLACED_WORD, "Trial"):
            assert word in p2, word
        rendered = out[1].get_pixmap(dpi=150, alpha=False)
        got = np.frombuffer(rendered.samples, dtype=np.uint8).reshape(rendered.height, rendered.width, 3)
        scan = np.asarray(Image.open(doc_dir / "source" / "01_scan.png").convert("RGB"))
        assert got.shape == scan.shape
        assert float(np.abs(got.astype(int) - scan.astype(int)).mean()) < 2.0  # text layer is invisible


def test_searchable_pdf_places_text_in_bbox(sample, tmp_path):
    document = sample[0]
    res = export_document(make_ctx(sample, tmp_path, "pdf"), "searchable-pdf")
    para = next(e for e in document.pages[1].elements if DISTINCTIVE in e.content)
    with pymupdf.open(res.primary_file) as out:
        words = [w for w in out[1].get_text("words") if w[4] == DISTINCTIVE]
    assert len(words) == 1
    x0, y0, x1, y1 = words[0][:4]
    b = para.bbox
    assert b.x0 - 1 <= x0 and x1 <= b.x1 + 1 and b.y0 - 1 <= y0 and y1 <= b.y1 + 1


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_searchable_pdf_rotated_page(tmp_path, rotation):
    document, manifest, doc_dir = build_rotated_pdf_sample(tmp_path / "docs", rotation)
    ctx = ExportContext(document=document, manifest=manifest, doc_dir=doc_dir, out_dir=tmp_path / "out")
    res = export_document(ctx, "searchable-pdf")
    assert res.verification["overall_recall"] == 1.0 and res.verification["page_sizes_match"]
    bbox = document.pages[0].elements[0].bbox
    with pymupdf.open(res.primary_file) as out:
        page = out[0]
        assert page.rotation == rotation
        lines = [ln for b in page.get_text("dict")["blocks"] for ln in b.get("lines", [])]
        assert lines
        for ln in lines:
            shown = pymupdf.Rect(ln["bbox"]) * page.rotation_matrix  # displayed coordinates
            assert bbox.x0 - 1 <= shown.x0 and shown.x1 <= bbox.x1 + 1
            assert bbox.y0 - 1 <= shown.y0 and shown.y1 <= bbox.y1 + 1
            assert shown.width > shown.height  # reads horizontally as displayed


def test_searchable_pdf_docx_pages(sample, tmp_path):
    document, manifest, doc_dir = sample
    docx_src = SourceFile(index=2, original_path="/in/notes.docx", filename="notes.docx", stored_path="source/02_notes.docx",
                          sha256="0" * 64, size_bytes=1, media_type=DOCX_TYPE, page_count=1)
    entry = ManifestEntry(page_number=3, page_id="p3", source_file="notes.docx", source_file_index=2,
                          source_page_index=0, width_pt=612, height_pt=792, content_hash="d")
    mixed = manifest.model_copy(update={"sources": [*manifest.sources, docx_src], "pages": [*manifest.pages, entry]})
    res = export_document(ExportContext(document=document, manifest=mixed, doc_dir=doc_dir, out_dir=tmp_path / "m"),
                          "searchable-pdf")
    assert res.verification["page_count"] == 2
    assert any("DOCX" in w for w in res.warnings)
    only_docx = mixed.model_copy(update={"sources": [docx_src.model_copy(update={"index": 0})],
                                         "pages": [entry.model_copy(update={"source_file_index": 0})]})
    with pytest.raises(ExportError, match="not applicable"):
        export_document(ExportContext(document=document, manifest=only_docx, doc_dir=doc_dir, out_dir=tmp_path / "d"),
                        "searchable-pdf")


def test_searchable_pdf_helvetica_fallback_is_reported(sample, tmp_path, monkeypatch):
    monkeypatch.setattr(spdf, "FONT_CANDIDATES", ())
    monkeypatch.setattr(spdf, "FONT_SEARCH_DIRS", ())
    ctx = make_ctx(sample, tmp_path, "helv")
    ctx.text_layer_font = tmp_path / "missing.ttf"
    res = export_document(ctx, "searchable-pdf")
    assert res.verification["text_layer_font"] == "helv"
    assert any("Configured text-layer font not found" in w for w in res.warnings)
    assert any("Helvetica" in w and "Latin-1" in w for w in res.warnings)
    assert any("lacks glyphs" in w and "П" in w for w in res.warnings)
    with pymupdf.open(res.primary_file) as out:
        assert DISTINCTIVE in out[1].get_text()


def test_fit_text_fills_box():
    lf = spdf.load_layer_font(None, [])
    lines, size = spdf.fit_text("one two three four five six seven eight nine ten", lf, 100, 40)
    assert len(lines) > 1 and size > 1
    assert all(lf.font.text_length(ln, fontsize=size) <= 100 + 1e-6 for ln in lines)
    assert len(lines) * size * lf.line_factor <= 40 + 1e-6


# ------------------------------------------------------------------ DOCX
def test_docx_structure(sample, tmp_path):
    res = export_document(make_ctx(sample, tmp_path, "docx"), "docx")
    v = res.verification
    assert v["headings"] == 3 and v["tables"] == 1 and v["images"] == 1 == v["images_expected"]
    d = python_docx.Document(res.primary_file)
    texts = [p.text for p in d.paragraphs]
    assert "Page 1" in texts and "Page 2" in texts
    styles = {p.text: p.style.name for p in d.paragraphs}
    assert styles["Quadratic Equations"] == "Title" and styles["1 Introduction"] == "Heading 1"
    assert styles["2 Chemistry of Ammonia"] == "Heading 2"
    assert styles["real coefficients"] == "List Bullet"
    assert any("[unclear: illegible handwriting in margin]" in t for t in texts)
    assert any(t.startswith("Q3 ") and t.endswith("[5 marks]") for t in texts)
    assert any("Привет" in t for t in texts)
    table = d.tables[0]
    assert table.cell(0, 0)._tc is table.cell(1, 0)._tc  # row span merged
    assert table.cell(0, 1)._tc is table.cell(0, 2)._tc  # column span merged
    assert table.cell(0, 1).text == "Values" and table.cell(2, 0).text == "Mass (g)"


def test_docx_native_equations(sample, tmp_path):
    if shutil.which("pandoc") is None:
        pytest.skip("pandoc not installed")
    res = export_document(make_ctx(sample, tmp_path, "docx"), "docx")
    v = res.verification
    assert v["equations_expected"] == 2 and v["native_equations"] == 2 and v["equations_fallback_latex"] == 0
    body = python_docx.Document(res.primary_file).element.body
    assert len(list(body.iter(f"{{{M_NS}}}oMathPara"))) == 2
    assert res.warnings == []


def test_docx_equation_fallbacks(sample, tmp_path, monkeypatch):
    document = sample[0]
    if shutil.which("pandoc") is not None:  # one unparsable equation falls back, the other stays native
        element(document, ElementType.EQUATION).latex = r"\frac{a}{"
        res = export_document(make_ctx(sample, tmp_path, "bad"), "docx")
        assert res.verification["native_equations"] == 1 and res.verification["equations_fallback_latex"] == 1
        assert any("could not be converted" in w for w in res.warnings)
        assert any(r"\frac{a}{" in p.text for p in python_docx.Document(res.primary_file).paragraphs)
    monkeypatch.setattr(docx_mod.shutil, "which", lambda name: None)
    res = export_document(make_ctx(sample, tmp_path, "nopandoc"), "docx")
    assert res.verification["native_equations"] == 0 and res.verification["equations_fallback_latex"] == 2
    assert any("pandoc not found" in w for w in res.warnings)


# ------------------------------------------------------------------ LaTeX
def test_latex_source(sample, tmp_path):
    res = export_document(make_ctx(sample, tmp_path, "tex", compile=False), "latex")
    tex = Path(res.primary_file).read_text(encoding="utf-8")
    assert tex.startswith(r"\documentclass") and r"\usepackage{fontspec}" in tex
    assert r"\setmainfont" not in tex and res.verification["main_font"] == "Latin Modern Roman"
    assert res.verification["compiled"] is None and res.verification["compile_skipped"]
    assert f"\\[\n{QUADRATIC}\n\\]" in tex
    assert r"\section*{1 Introduction}" in tex and r"\subsection*{2 Chemistry of Ammonia}" in tex
    assert r"Physical Chemistry \& Kinetics\_2 (100\% revised)" in tex
    assert r"\begin{thebibliography}" in tex and r"\bibitem{ref1}" in tex
    assert "% ---- source page 2 ----" in tex
    assert r"\textbf{[unclear: illegible handwriting in margin]}" in tex
    assert r"\multicolumn{2}{l}{\textbf{Values}}" in tex
    assert r"\caption*{Fig. 1: Reaction rate versus temperature}" in tex
    assert tex.count("Fig. 1: Reaction rate versus temperature") == 1  # caption folded into the figure
    assert r"\emph{[5 marks]}" in tex
    fig = next(f for f in res.files if f.endswith(".png"))
    assert Path(fig).is_file() and f"{{assets/{Path(fig).name}}}" in tex
    if r"\usepackage[version=4]{mhchem}" in tex:
        assert AMMONIA in tex
    else:
        assert r"\rightleftharpoons" in tex and any("mhchem" in w for w in res.warnings)


def test_latex_rejects_unsafe_font_option(sample, tmp_path):
    with pytest.raises(ExportError, match="main_font"):
        export_document(make_ctx(sample, tmp_path, "tex", main_font=r"X}\input{/etc/passwd"), "latex")


latex_engine = pytest.mark.skipif(shutil.which("xelatex") is None, reason="xelatex not installed")


@pytest.mark.latex
@latex_engine
def test_latex_compiles_with_default_font(sample, tmp_path):
    res = export_document(make_ctx(sample, tmp_path, "tex"), "latex")
    v = res.verification
    assert v["engine"] == "xelatex" and v["compiled"] is True, v.get("log_tail")
    assert Path(v["pdf"]).is_file() and v["main_font_verified"] is True
    assert any("LMRoman" in f for f in v["fonts_in_pdf"])
    assert "П" in v["missing_characters"]  # Latin Modern has no Cyrillic: reported, not hidden
    with pymupdf.open(v["pdf"]) as pdf:
        assert pdf.page_count >= 1 and "Quadratic Equations" in pdf[0].get_text()


@pytest.mark.latex
@latex_engine
def test_latex_compiles_with_unicode_main_font(sample, tmp_path):
    res = export_document(make_ctx(sample, tmp_path, "tex", main_font="DejaVu Serif"), "latex")
    v = res.verification
    assert v["compiled"] is True, v.get("log_tail")
    assert v["main_font"] == "DejaVu Serif" and v["main_font_verified"] is True
    assert any("DejaVuSerif" in f for f in v["fonts_in_pdf"]) and v["missing_characters"] == []
    with pymupdf.open(v["pdf"]) as pdf:
        assert "Привет" in "".join(p.get_text() for p in pdf)


@pytest.mark.latex
@latex_engine
@pytest.mark.latex
def test_latex_compile_failure_is_reported(sample, tmp_path):
    if shutil.which("xelatex") is None:
        pytest.skip("xelatex not installed")
    # Syntactically balanced but undefined macro: passes the static check, fails compilation.
    element(sample[0], ElementType.EQUATION).latex = r"\undefinedmacroxyz{a}"
    res = export_document(make_ctx(sample, tmp_path, "tex"), "latex")
    v = res.verification
    assert v["compiled"] is False and v["pdf"] is None and v["log_tail"]
    assert any("compilation failed" in w for w in res.warnings)


@pytest.mark.latex
def test_latex_invalid_math_is_isolated_and_still_compiles(sample, tmp_path):
    if shutil.which("xelatex") is None:
        pytest.skip("xelatex not installed")
    element(sample[0], ElementType.EQUATION).latex = r"\frac{a}{"
    res = export_document(make_ctx(sample, tmp_path, "tex"), "latex")
    assert res.verification["compiled"] is True
    assert any("invalid LaTeX" in w for w in res.warnings)
    assert "[invalid LaTeX, shown verbatim]" in Path(res.primary_file).read_text(encoding="utf-8")


# ------------------------------------------------------------------ CSV / XLSX
def test_csv_export(sample, tmp_path):
    res = export_document(make_ctx(sample, tmp_path, "csv"), "csv")
    assert Path(res.primary_file).name == "table_2_1.csv"
    with open(res.primary_file, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows == [["Quantity", "Values", ""], ["", "Trial 1", "Trial 2"], ["Mass (g)", "1.25", "1.30"],
                    ["Volume (L)", "2.0", "2.l"]]
    assert res.verification["all_ok"] and res.verification["files"][0]["rows"] == 4


def test_xlsx_export(sample, tmp_path):
    res = export_document(make_ctx(sample, tmp_path, "xlsx"), "xlsx")
    assert res.verification["all_ok"] and res.verification["tables"] == 1
    wb = load_workbook(res.primary_file)
    assert wb.sheetnames == ["Index", "T2_1"]
    idx = [c.value for c in wb["Index"][2]]
    assert idx[:7] == ["T2_1", element(sample[0], ElementType.TABLE).element_id, 2, "Table 1", "Measured values", 4, 3]
    ws = wb["T2_1"]
    assert {str(r) for r in ws.merged_cells.ranges} == {"A1:A2", "B1:C1"}
    assert ws["A1"].value == "Quantity" and ws["A1"].font.bold and ws["B2"].font.bold
    assert ws["B3"].value == "1.25" and not ws["B3"].font.bold
    assert ws["C4"].fill.fgColor.rgb.endswith("FFFF99") and ws["C4"].comment is not None


@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
def test_table_exports_require_tables(sample, tmp_path, fmt):
    document = sample[0]
    for page in document.pages:
        page.elements = [e for e in page.elements if e.type != ElementType.TABLE]
    with pytest.raises(ExportError, match="Document contains no tables"):
        export_document(make_ctx(sample, tmp_path, fmt), fmt)


# ------------------------------------------------------------------ helpers
def test_ce_to_math():
    out = ce_to_math(AMMONIA)
    assert out == r"\mathrm{N_{2} + 3H_{2} \rightleftharpoons 2NH_{3}}"
    assert ce_to_math(r"\ce{SO4^2-}") == r"\mathrm{SO_{4}^{2-}}"
    assert ce_to_math(r"\ce{Na+ + Cl- -> NaCl}") == r"\mathrm{Na^{+} + Cl^{-} \rightarrow NaCl}"
    assert ce_to_math(r"\ce{NO3- + Fe^{3+}}") == r"\mathrm{NO_{3}^{-} + Fe^{3+}}"
    assert ce_to_math(r"x = \cent") == r"x = \cent"  # not a \ce command


def test_layout_table_row_and_col_spans(sample):
    table = element(sample[0], ElementType.TABLE).table
    layout = layout_table(table)
    assert (layout.n_rows, layout.n_cols, layout.header_rows) == (4, 3, 2)
    assert layout.owner[1][0] is layout.anchor_at(0, 0)
    assert layout.text_grid()[1] == ["", "Trial 1", "Trial 2"]


@pytest.mark.parametrize("fmt", FORMATS)
def test_every_format_writes_absolute_existing_files(sample, tmp_path, monkeypatch, fmt):
    document, manifest, doc_dir = sample
    monkeypatch.chdir(tmp_path)
    ctx = ExportContext(document=document, manifest=manifest, doc_dir=doc_dir, out_dir=Path("rel_out") / fmt,
                        options={"compile": False})
    res = export_document(ctx, fmt)
    assert res.format == fmt and res.primary_file in res.files
    assert all(Path(f).is_absolute() and Path(f).is_file() for f in res.files)
    assert res.verification
