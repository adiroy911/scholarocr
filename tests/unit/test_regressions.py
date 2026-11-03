"""Regression tests for the problems found in the 2026-11-03 Antigravity runs (fixed in 0.3.0).

Each test names the run finding it guards. No model is called.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import docx as python_docx
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

import scholarocr.export.docx as docx_mod
from scholarocr.exceptions import AccessDeniedError, SubmissionRejected
from scholarocr.export import ExportContext, export_document
from scholarocr.export.base import latex_to_readable, split_inline_math, starts_with_question_number
from scholarocr.ingestion.loader import image_signature
from scholarocr.intelligence import prompts
from scholarocr.intelligence.extractor import VisualPageContext, convert_response, page_doubt_note
from scholarocr.intelligence.mathematics import chemistry_issues
from scholarocr.intelligence.responses import PageExtractionResponse, XElement
from scholarocr.pipeline import host
from scholarocr.pipeline.routing import decide_route
from scholarocr.schemas import ManifestEntry, PageProfile, ProcessingOptions, Route, Uncertainty
from scholarocr.security import PathPolicy
from tests.fixtures.ir_samples import build_sample

ROOT = Path(__file__).resolve().parents[2]
FONT = ROOT / "tests" / "fixtures" / "fonts" / "DejaVuSans.ttf"


def clean_digital_page() -> Image.Image:
    """A 150 dpi render of a text page: thin anti-aliased strokes that vanish below grey 128 at 400 px."""
    font = ImageFont.truetype(str(FONT), 13)
    im = Image.new("RGB", (1275, 1650), "white")
    d = ImageDraw.Draw(im)
    for i in range(40):
        d.text((120, 120 + i * 34), "The tangent at P meets the axis in T, and gamma_0 = 0.5 for all x.",
                fill=(30, 30, 30), font=font)
    return im


# ---------------------------------------------------------------------------------------------- 1. blank route
def test_clean_digital_page_is_not_blank():
    im = clean_digital_page()
    small = im.convert("L")
    small.thumbnail((400, 400))
    assert (np.asarray(small) < 128).mean() < 0.0004  # what the old fixed-threshold check measured
    _, _, blank, ink = image_signature(im)
    assert not blank and ink > 0.01


def test_blank_pages_are_still_blank():
    assert image_signature(Image.new("RGB", (1275, 1650), "white"))[2]
    rng = np.random.default_rng(0)
    paper = (np.full((1650, 1275), 240, np.uint8) + rng.integers(0, 8, (1650, 1275)).astype(np.uint8))
    assert image_signature(Image.fromarray(paper))[2]  # scanner texture is not ink


def test_force_visual_bypasses_the_blank_route():
    entry = ManifestEntry(page_number=1, page_id="p1", source_file="x.png", source_file_index=0,
                          source_page_index=0, width_pt=612, height_pt=792, content_hash="h",
                          profile=PageProfile(is_scanned=True, image_coverage=1.0, is_blank=True))
    assert decide_route(entry, ProcessingOptions(), "image/png")[0] == Route.BLANK
    route, _ = decide_route(entry, ProcessingOptions(force_visual=True), "image/png")
    assert route == Route.VISUAL_GENERAL


# ---------------------------------------------------------------------------------------------- 2. doubt
def test_legacy_legibility_maps_to_page_doubt_and_does_not_inflate_elements():
    resp = PageExtractionResponse.model_validate(
        {"legibility": "high", "elements": [{"type": "paragraph", "text": "clean", "uncertainty": "low"}]})
    assert resp.page_doubt == "high"
    ctx = VisualPageContext(document_id="d", page_id="d-p0001", page_number=1, source_file="x.png",
                            source_page_index=0, page_width_pt=612, page_height_pt=792, original_dpi=72,
                            original_size=(612, 792), processed_size=(612, 792),
                            matrix=[[1, 0, 0], [0, 1, 0]], page_image_rel=None, quality=None)
    [el] = convert_response(resp, ctx, model="host")
    assert el.uncertainty == Uncertainty.LOW and not any("legibility" in r for r in el.uncertainty_reasons)
    assert page_doubt_note(resp) == ["model note: overall page doubt reported as high"]
    assert "page_doubt" in PageExtractionResponse.model_json_schema()["properties"]
    assert "legibility" not in PageExtractionResponse.model_json_schema()["properties"]


def test_host_accepts_doubt_synonyms_and_warns_on_legibility():
    resp, warnings = host.validate_submission({"legibility": "low", "elements": [
        {"type": "paragraph", "text": "a", "doubt": "none"},
        {"type": "paragraph", "text": "b", "uncertainty": "Medium"}]})
    assert [x.uncertainty for x in resp.elements] == ["low", "medium"]
    assert any("old name of page_doubt" in w for w in warnings)


def test_brief_explains_doubt_calibration():
    rules = prompts.HOST_RULES
    for phrase in ("DOUBT", "you would bet the transcription is exact", "a specific character or word could differ",
                   "a guess among alternatives", "do not blanket-label"):
        assert phrase in rules, phrase
    assert 'page_doubt="low|medium|high|unresolved"' in prompts.HOST_SHAPE and "legibility" not in prompts.HOST_SHAPE
    assert prompts.HOST_PROMPT_VERSION != "host-2026-11-03.2"


# ---------------------------------------------------------------------------------------------- 3. shell commands
def test_rules_forbid_shell_and_reading_plugin_files():
    agents = (ROOT / "rules" / "AGENTS.md").read_text(encoding="utf-8")
    for text in (prompts.HOST_RULES, agents):
        assert "shell commands" in text and 'get_page_image(instructions="full")' in text
        assert "plugin" in text and "notes" in text and "blank" in text and "web" in text


def test_headless_settings_example_denies_commands():
    path = ROOT / "antigravity" / "headless-settings.example.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert "mcp(scholarocr_scholarocr/*)" in doc["permissions"]["allow"]
    assert any(r.startswith("command(") for r in doc["permissions"]["deny"])
    # Not part of the plugin payload: skills/ and rules/ are loaded by Antigravity.
    assert not list((ROOT / "skills").rglob("*.json")) and not list((ROOT / "rules").rglob("*.json"))


# ---------------------------------------------------------------------------------------------- 4. chemistry examples
def test_shape_examples_are_valid_elements_and_cover_chemistry():
    examples = [json.loads(line) for line in prompts.HOST_SHAPE.splitlines() if line.startswith("{")]
    for ex in examples:
        XElement.model_validate(ex)
    texts = json.dumps(examples)
    assert r"\\ce{H2 + O2 -> H2O}" in texts and r"\\ce{N2 + 3H2 <=> 2NH3}" in texts and r"\\ce{SO4^2-}" in texts
    cap = next(e for e in examples if e["type"] == "caption")
    assert examples[cap["caption_of"]]["type"] == "figure"
    # The validator accepts the examples as one page.
    host.validate_submission({"elements": examples})
    assert len(prompts.HOST_RULES + prompts.HOST_SHAPE) < 4800  # keep the brief compact


# ---------------------------------------------------------------------------------------------- 5. malformed elements
def test_malformed_elements_get_path_problem_fix():
    with pytest.raises(SubmissionRejected) as e:
        host.validate_submission({"elements": [0, 1, 2]})
    errors = e.value.details["errors"]
    assert [x["path"] for x in errors] == ["elements[0]", "elements[1]", "elements[2]"]
    assert all("fix" in x and "must be an object" in x["problem"] for x in errors)
    with pytest.raises(SubmissionRejected) as e:
        host.validate_submission({"elements": "[{not json"})
    assert e.value.details["errors"][0]["path"] == "elements"
    resp, _ = host.validate_submission({"elements": json.dumps([{"type": "paragraph", "text": "x"}])})
    assert resp.elements[0].text == "x"


# ---------------------------------------------------------------------------------------------- 6. reaction signs
@pytest.mark.parametrize("ce", [r"\ce{2H2 + O2 = 2H2O}", r"\ce{A + B <- C}", r"\ce{N2 + 3H2 <=> 2NH3}",
                                r"\ce{A + B <=>> C}", r"\ce{A + B <<=> C}", "\\ce{A + B ⟶ C}"])
def test_reaction_signs_are_accepted(ce):
    assert chemistry_issues(ce, "") == []


def test_reaction_without_sign_still_flagged():
    assert chemistry_issues(r"\ce{A + B C}", "") == ["reaction without an arrow"]


# ---------------------------------------------------------------------------------------------- 7. text layer
def test_text_layer_note_asks_for_latex_inline_math():
    brief = prompts.host_page_brief(Route.VISUAL_MATH, full=False, document_type=None, language_hint=None,
                                    context_hint=None, text_layer="T max and γ0")
    assert "do not copy Unicode math from the text layer; write inline math as $...$ LaTeX" in brief.replace(
        "Do not", "do not")


# ---------------------------------------------------------------------------------------------- 8. DOCX
def test_question_prefix_and_inline_math_helpers():
    assert starts_with_question_number("4", "4. If the tangent") and starts_with_question_number("4", "Q4) x")
    assert not starts_with_question_number("4", "If the tangent") and not starts_with_question_number("4", "41 a")
    assert split_inline_math("cost $5 and $10, and $x^2$.") == [("cost $5 and $10, and ", False), ("x^2", True),
                                                                (".", False)]
    assert latex_to_readable(r"\gamma_0") == "γ₀" and latex_to_readable(r"T_{\max}") == "Tₘₐₓ"
    assert latex_to_readable(r"\ce{SO4^2-}") == "SO₄²⁻"


def _docx_ctx(tmp_path, name):
    document, manifest, doc_dir = build_sample(tmp_path / "docs")
    q = next(e for p in document.pages for e in p.elements if e.question and e.question.number == "3")
    q.content = "3. If $x^2 = 4$, find $\\gamma_0$."
    return ExportContext(document=document, manifest=manifest, doc_dir=doc_dir, out_dir=tmp_path / name)


def test_docx_no_double_question_number_and_readable_inline_math(tmp_path, monkeypatch):
    monkeypatch.setattr(docx_mod.shutil, "which", lambda name: None)
    res = export_document(_docx_ctx(tmp_path, "nopandoc"), "docx")
    texts = [p.text for p in python_docx.Document(res.primary_file).paragraphs]
    line = next(t for t in texts if "find" in t)
    assert not line.startswith("Q3") and line.startswith("3. If x² = 4, find γ₀.")
    assert "$" not in line
    assert res.verification["inline_math"] == 2 and res.verification["inline_math_native"] == 0
    assert any("inline formula(s) as plain text" in w for w in res.warnings)


def test_docx_inline_math_becomes_native_with_pandoc(tmp_path, monkeypatch):
    from lxml import etree

    def fake_omml(latexes, warnings, inline=None):
        out = []
        for tex, is_inline in zip(latexes, inline or [False] * len(latexes)):
            math = f'<m:oMath xmlns:m="{docx_mod.M_NS}"><m:r><m:t>{tex}</m:t></m:r></m:oMath>'
            out.append(etree.fromstring(math if is_inline else
                                        f'<m:oMathPara xmlns:m="{docx_mod.M_NS}">{math}</m:oMathPara>'))
        return out

    monkeypatch.setattr(docx_mod, "latex_to_omml", fake_omml)
    res = export_document(_docx_ctx(tmp_path, "pandoc"), "docx")
    assert res.verification["inline_math_native"] == 2 and res.verification["native_equations"] == 2
    body = python_docx.Document(res.primary_file).element.body
    para = next(p for p in body.iter(f"{{{docx_mod.W_NS}}}p")
                if "find" in "".join(t.text or "" for t in p.iter(f"{{{docx_mod.W_NS}}}t")))
    assert len(list(para.iter(f"{{{docx_mod.M_NS}}}oMath"))) == 2
    assert "$" not in "".join(t.text or "" for t in para.iter(f"{{{docx_mod.W_NS}}}t"))


# ---------------------------------------------------------------------------------------------- 10. split questions
def test_rule_9_covers_question_continuations():
    rule9 = re.search(r"\n9\. (.+?)\n10\.", prompts.HOST_RULES, re.S).group(1)
    assert "continues a question" in rule9 and "parent_index" in rule9 and "never repeat a number" in rule9


# ---------------------------------------------------------------------------------------------- 11. access_denied
def test_access_denied_names_env_file_and_says_append(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "page.png").write_bytes(b"x")
    env_file = tmp_path / "custom.env"
    with pytest.raises(AccessDeniedError) as e:
        PathPolicy([allowed], 10, env_file=env_file).check_file(outside / "page.png")
    msg = e.value.message
    assert str(env_file) in msg and "APPEND" in msg and f"{allowed.resolve()}{os.pathsep}{outside}" in msg
    with pytest.raises(AccessDeniedError) as e:  # no env file (remote server, web app): generic advice
        PathPolicy([allowed], 10).check_file(outside / "page.png")
    assert "SCHOLAROCR_ALLOWED_DIRS" in e.value.message and "env" not in e.value.message.split("ALLOWED_DIRS")[1]


def test_settings_record_the_env_file(tmp_path):
    from scholarocr.config import Settings

    f = tmp_path / "x.env"
    assert Settings.from_env({"SCHOLAROCR_ENV_FILE": str(f), "SCHOLAROCR_DATA_DIR": str(tmp_path)}).env_file == f


def test_agents_md_access_denied_advice():
    agents = (ROOT / "rules" / "AGENTS.md").read_text(encoding="utf-8")
    assert "APPEND" in agents and "SCHOLAROCR_ENV_FILE" in agents and "keeping the existing entries" in agents
    assert "element_id" in agents and "get_source_region" in agents and "compact=true" in agents


def test_element_ids_from_findings_hint():
    agents = (ROOT / "rules" / "AGENTS.md").read_text(encoding="utf-8")
    assert "`verify_document` finding" in agents



def test_fair_page_does_not_blanket_raise_handwriting():
    # c2 re-run: the model labelled 26 of 36 elements low, the "handwriting on degraded page" rule made 34 medium.
    from scholarocr.intelligence.uncertainty import apply_rules
    from scholarocr.schemas import Element, ElementType, QualityAssessment, SourceRef

    def hw(grade):
        el = Element(element_id="d-p0001-e001", type=ElementType.HANDWRITING, page_id="d-p0001", page_number=1,
                     reading_order=1, content="a_0 = 0", handwritten=True,
                     source=SourceRef(document_id="d", page_number=1, source_file="x.jpg", source_page_index=0))
        apply_rules(el, quality=QualityAssessment(grade=grade, issues=["low_resolution"]))
        return el.uncertainty

    assert hw("fair") == Uncertainty.LOW
    assert hw("poor") == Uncertainty.MEDIUM
