"""Bugs found by exporting and scoring a real Antigravity read of CBSE exam pages (2026-11-04)."""

from __future__ import annotations

import subprocess

import docx as python_docx
import pytest

import scholarocr.export.docx as docx_mod
from scholarocr.evaluation.metrics import evaluate_document, normalize_latex, page_text
from scholarocr.export import ExportContext, export_document
from scholarocr.export.base import (
    ce_to_math,
    marks_in_text,
    marks_label,
    question_label,
    question_lead,
    split_emphasis,
    starts_with_question_number,
)
from scholarocr.schemas import Element, ElementType, QuestionInfo, SourceRef, TableCell, TableData
from tests.fixtures.ir_samples import build_sample


@pytest.mark.parametrize("number,text", [
    ("23.(a)", "Q.23. (a) Find the derivative"), ("28. (a)", "Q.28. (a) An ant"), ("32. (I)", "Q32. (I) Explain"),
    ("33. (I) a)", "Q33. (I) a) A giant"), ("(II) a)", "(II) a) With the help"), ("(i)", "(i) Find the value"),
    ("b)", "b) Write any two"), ("b.", "b. Chloroform"), ("13.", "13."), ("(क)", "(क) आप अनुपमा"),
    ("10(a)", "10(a) Find x"), ("7", "प्रश्न 7. नाम लिखिए"), ("12", "१२. एक वृत्त"), ("१२", "12. x"), ("Q13", "Q13. x"),
    ("4", "4. If"), ("4", "Q4) x"), ("4", "(4) x"), ("6", "Question 6: Define"), ("11", "**11.** Bold"),
])
def test_text_already_starts_with_the_number(number, text):
    assert starts_with_question_number(number, text) and question_lead(number, text) == ""


@pytest.mark.parametrize("number,text", [
    ("4", "If the tangent"), ("4", "41 a"), ("a", "A person standing"), ("a", "a person"), ("I", "I think"),
    ("23.(a)", "Find the derivative"), (None, "4. If"),
])
def test_text_does_not_start_with_the_number(number, text):
    assert not starts_with_question_number(number, text)


def test_question_lead_keeps_only_what_the_text_lacks():
    assert question_lead("17 a.", "a. Nitrogen gas") == "17"
    assert question_lead("33. (I) a)", "a) A giant") == "33. (I)"
    assert question_lead("23.(a)", "(a) Find") == "23"
    assert question_lead("15", "Assertion (A) Glucose") == "15"
    assert question_label("15") == "Q15" and question_label("(i)") == "(i)" and question_label("Q13") == "Q13"


def test_marks_label_and_marks_already_printed():
    assert marks_label("2") == "[2 marks]" and marks_label("2 Marks") == "[2 Marks]" and marks_label("1 Mark") == "[1 Mark]"
    assert marks_in_text("2 Marks", "Find it. [2 Marks]") and marks_in_text("2", "Find it. [2]")
    assert not marks_in_text("2", "Find it.")


@pytest.mark.parametrize("ce,expected", [
    (r"\ce{Fe(OH)3 v}", r"\mathrm{Fe(OH)_{3} \downarrow}"),
    (r"\ce{2Al + 6HCl -> 2AlCl3 + 3H2 ^}", r"\mathrm{2Al + 6HCl \rightarrow 2AlCl_{3} + 3H_{2} \uparrow}"),
    (r"\ce{HC#CH}", r"\mathrm{HC\equiv CH}"),
    (r"\ce{CuSO4*5H2O}", r"\mathrm{CuSO_{4} \cdot 5H_{2}O}"),
    (r"\ce{CuSO4.5H2O}", r"\mathrm{CuSO_{4} \cdot 5H_{2}O}"),
    (r"\ce{0.5H2O}", r"\mathrm{0.5H_{2}O}"),
    (r"\ce{CaCO3 ->[\Delta] CaO}", r"\mathrm{CaCO_{3} \xrightarrow{\Delta} CaO}"),
    (r"\ce{A ->[H2O][heat] B}", r"\mathrm{A \xrightarrow[heat]{H_{2}O} B}"),
    (r"\ce{A <=>[cat] B}", r"\mathrm{A \overset{cat}{\rightleftharpoons} B}"),
    (r"\ce{H2O <=>> H+(aq) + OH-(aq)}", r"\mathrm{H_{2}O \rightleftharpoons H^{+}(aq) + OH^{-}(aq)}"),
])
def test_ce_to_math_more_mhchem(ce, expected):
    assert ce_to_math(ce) == expected


def test_split_emphasis():
    assert split_emphasis("**Reason (R):** Glucose") == [("Reason (R):", "bold"), (" Glucose", None)]
    assert split_emphasis("*Read this.*") == [("Read this.", "italic")]
    assert split_emphasis("2*3*4 and 5 * 6 * 7") == [("2*3*4 and 5 * 6 * 7", None)]


def _element(type_, content, **kw):
    return Element(element_id=f"x-{abs(hash(content))}", type=type_, page_id="p", page_number=1, reading_order=kw.pop("order", 50),
                   content=content, extraction_method="test",
                   source=SourceRef(document_id="d", page_number=1, source_file="f", source_page_index=0), **kw)


def _ctx(tmp_path, *elements):
    document, manifest, doc_dir = build_sample(tmp_path / "docs")
    document.pages[0].elements = [e.model_copy(update={"reading_order": i}) for i, e in enumerate(elements)]
    document.pages = document.pages[:1]
    return ExportContext(document=document, manifest=manifest, doc_dir=doc_dir, out_dir=tmp_path / "out")


def test_docx_real_read_patterns_without_pandoc(tmp_path, monkeypatch):
    monkeypatch.setattr(docx_mod.shutil, "which", lambda name: None)
    table = TableData(header_rows=1, caption="Values of $x^2$",
                      rows=[[TableCell(text="$x$", is_header=True), TableCell(text="खंड – क\\n( अपठित बोध )", is_header=True)],
                            [TableCell(text="$\\ce{H2O}$"), TableCell(latex="y^2")]])
    ctx = _ctx(tmp_path,
               _element(ElementType.PARAGRAPH, "Q.23. (a) Find it.", question=QuestionInfo(number="23.(a)", marks="2")),
               _element(ElementType.PARAGRAPH, "(i) Find. [2 Marks]", question=QuestionInfo(number="(i)", marks="2 Marks")),
               _element(ElementType.PARAGRAPH, "a. Nitrogen gas.", question=QuestionInfo(number="17 a.", marks="1")),
               _element(ElementType.PARAGRAPH, "**Assertion (A)** Glucose *is* sweet."),
               _element(ElementType.TABLE, "", table=table),
               _element(ElementType.CHEMICAL_EQUATION, r"H2 ^", latex=r"\ce{H2 ^}"))
    res = export_document(ctx, "docx")
    doc = python_docx.Document(res.primary_file)
    texts = [p.text for p in doc.paragraphs]
    assert "Q.23. (a) Find it.  [2 marks]" in texts
    assert "(i) Find. [2 Marks]" in texts  # no second label, no "marks marks"
    assert "Q17 a. Nitrogen gas.  [1 marks]" in texts
    para = next(p for p in doc.paragraphs if "Glucose" in p.text)
    assert para.text == "Assertion (A) Glucose is sweet."
    assert [r.text for r in para.runs if r.bold] == ["Assertion (A)"] and [r.text for r in para.runs if r.italic] == ["is"]
    assert r"\ce{H2 ^}" in texts  # fallback shows the source as read
    cells = [c.text for row in doc.tables[0].rows for c in row.cells]
    assert not any("$" in c or "\\n" in c for c in cells), cells
    assert cells[1] == "खंड – क ( अपठित बोध )"
    assert "Values of x²" in texts
    assert res.verification["inline_math"] == 4  # caption, $x$, H2O and the cell's latex
    assert all(r.bold for r in doc.tables[0].rows[0].cells[1].paragraphs[0].runs)


def test_docx_table_math_goes_through_pandoc_path(tmp_path, monkeypatch):
    from lxml import etree

    seen = {}

    def fake_omml(latexes, warnings, inline=None):
        seen["latex"] = list(latexes)
        return [etree.fromstring(f'<m:oMath xmlns:m="{docx_mod.M_NS}"><m:r><m:t>m</m:t></m:r></m:oMath>') for _ in latexes]

    monkeypatch.setattr(docx_mod, "latex_to_omml", fake_omml)
    table = TableData(header_rows=0, rows=[[TableCell(text="$a^2$ is"), TableCell(latex="b^2")]])
    res = export_document(_ctx(tmp_path, _element(ElementType.TABLE, "", table=table)), "docx")
    assert seen["latex"] == ["a^2", "b^2"] and res.verification["inline_math_native"] == 2
    body = python_docx.Document(res.primary_file).element.body
    assert len(list(body.iter(f"{{{docx_mod.M_NS}}}oMath"))) == 2


def test_latex_to_omml_reads_pandoc_output_as_utf8_and_keeps_tags(monkeypatch):
    calls = {}

    def fake_run(cmd, **kw):
        calls.update(kw)
        calls["source"] = open(cmd[-1], encoding="utf-8").read()
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(docx_mod.shutil, "which", lambda name: "pandoc")
    monkeypatch.setattr(docx_mod.subprocess, "run", fake_run)
    warnings: list[str] = []
    assert docx_mod.latex_to_omml([r"E = mc^2 \tag{1}"], warnings) == [None]
    assert calls["encoding"] == "utf-8" and calls["errors"] == "replace"
    assert r"\tag" not in calls["source"] and r"\qquad (1)" in calls["source"]


def test_text_for_search_does_not_double_tables_and_lists():
    table = TableData(header_rows=0, rows=[[TableCell(text="a  b"), TableCell(text="c")]])
    el = _element(ElementType.TABLE, "a b | c", table=table)
    assert el.text_for_search() == "a b | c"
    assert "a  b c" in _element(ElementType.TABLE, "", table=table).text_for_search()
    items = _element(ElementType.LIST, "- one\n- two", items=["one", "two"])
    assert items.text_for_search() == "- one\n- two"
    assert "two" in _element(ElementType.LIST, "intro", items=["one", "two"]).text_for_search()


def test_table_cell_escaped_line_break_but_not_nu():
    assert TableCell(text="a\\n(b)").text == "a (b)"
    assert TableCell(text="$\\nu$").text == "$\\nu$"


def test_scorer_counts_a_table_once_and_finds_inline_math(tmp_path):
    table = TableData(header_rows=0, rows=[[TableCell(text="alpha beta"), TableCell(text="gamma")]])
    ctx = _ctx(tmp_path,
               _element(ElementType.TABLE, "alpha beta | gamma", table=table),
               _element(ElementType.PARAGRAPH, r"Solve $\int_{0}^{1} x \, dx$ and $2\hat{\imath} \geq 1$."))
    assert page_text(ctx.document, 1).startswith("alpha beta gamma\n")
    gt = {"pages": [{"page_number": 1, "text": page_text(ctx.document, 1), "equations": [r"\int_0^1 x dx", r"2\hat{i} \ge 1", "z"]}]}
    m = evaluate_document(ctx.document, gt)["metrics"]
    assert m["cer"] == 0 and m["math_exact_match"] == round(2 / 3, 4)


def test_normalize_latex_spellings():
    assert normalize_latex(r"x_{0}^{2} \geq \dfrac{a}{b}") == normalize_latex(r"x_0^2 \ge \frac{a}{b}")
    assert normalize_latex(r"\overrightarrow{AB} \rightarrow \hat{\imath}") == normalize_latex(r"\vec{AB} \to \hat{i}")
    assert normalize_latex(r"\geqslant") != normalize_latex(r"\ge")
    assert normalize_latex(r"x_{ab}") == "x_{ab}"
