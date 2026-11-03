"""The SVG sanitiser (malicious input) and the cleaned-text guard and normaliser. No model calls."""

from __future__ import annotations

import pytest

from scholarocr.exceptions import InputError
from scholarocr.intelligence.clean_text import check_clean, resolve_clean, rule_clean
from scholarocr.svg_sanitize import extract_svg, sanitize_svg

OK_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">'
          '<defs><marker id="ar" markerWidth="6" markerHeight="6" refX="3" refY="3" orient="auto">'
          '<path d="M0 0 L6 3 L0 6 z" fill="#111"/></marker></defs>'
          '<rect x="5" y="5" width="60" height="50" fill="none" stroke="#111" stroke-width="2"/>'
          '<line x1="65" y1="30" x2="150" y2="30" stroke="#111" marker-end="url(#ar)"/>'
          '<text x="20" y="35" font-size="14">T<tspan dy="4" font-size="9">A</tspan></text></svg>')


# ---------------------------------------------------------------------------------------------- SVG
def test_clean_svg_passes_unchanged_in_substance():
    out = sanitize_svg(OK_SVG)
    assert out.removed == [] and out.texts == ["T", "A"]
    assert 'marker-end="url(#ar)"' in out.svg and out.svg.startswith("<svg")


@pytest.mark.parametrize("payload, removed", [
    ('<script>alert(1)</script>', "element <script>"),
    ('<style>rect{fill:url(http://evil/x)}</style>', "element <style>"),
    ('<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject>', "element <foreignObject>"),
    ('<image href="http://evil/x.png" width="10" height="10"/>', "element <image>"),
    ('<a href="javascript:alert(1)"><text x="1" y="1">x</text></a>', "element <a>"),
    ('<use href="#ar"/>', "element <use>"),
    ('<animate attributeName="x" to="javascript:alert(1)"/>', "element <animate>"),
    ('<set attributeName="onload" to="alert(1)"/>', "element <set>"),
    ('<rect x="1" y="1" width="5" height="5" onclick="alert(1)"/>', "attribute 'onclick' on <rect>"),
    ('<rect x="1" y="1" width="5" height="5" style="fill:red"/>', "attribute 'style' on <rect>"),
    ('<rect x="1" y="1" width="5" height="5" class="c"/>', "attribute 'class' on <rect>"),
    ('<line x1="0" y1="0" x2="5" y2="5" stroke="url(http://evil/p)"/>', "value of 'stroke' on <line>"),
    ('<line x1="0" y1="0" x2="5" y2="5" marker-end="url(data:image/svg+xml,x)"/>', "value of 'marker-end' on <line>"),
    ('<path d="M0 0 L5 5" transform="expression(alert(1))"/>', "value of 'transform' on <path>"),
    ('<path d="javascript:alert(1)"/>', "value of 'd' on <path>"),
    ('<rect xmlns:xl="http://www.w3.org/1999/xlink" xl:href="#x" x="1" y="1" width="1" height="1"/>',
     "attribute 'href' on <rect>"),
])
def test_malicious_parts_are_removed(payload, removed):
    out = sanitize_svg(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">{payload}</svg>')
    assert removed in out.removed
    low = out.svg.lower()
    for bad in ("<script", "javascript:", "onclick", "onload", "<style", "foreignobject", "<image", "http://evil",
                "expression(", "<a ", "<use", "<animate", "<set", "data:"):
        assert bad not in low


@pytest.mark.parametrize("raw, why", [
    ('<!DOCTYPE svg [<!ENTITY x "y">]><svg viewBox="0 0 1 1">&x;</svg>', "DOCTYPE"),
    ('<?xml version="1.0"?><?xml-stylesheet href="http://evil/s.css"?><svg viewBox="0 0 1 1"/>', "stylesheets"),
    ('<svg viewBox="0 0 1 1"><?php echo 1; ?></svg>', "processing instructions"),
    ('<html><body/></html>', "root element"),
    ('<svg><path d="M0 0"/></svg>', "viewBox"),
    ('<svg viewBox="0 0 1 1"><g>', "well-formed"),
    ("", "empty"),
])
def test_dangerous_documents_are_refused(raw, why):
    with pytest.raises(InputError, match=why):
        sanitize_svg(raw)


def test_size_and_element_limits():
    with pytest.raises(InputError, match="larger"):
        sanitize_svg('<svg viewBox="0 0 1 1">' + "<g/>" * 120_000 + "</svg>")
    with pytest.raises(InputError, match="elements"):
        sanitize_svg('<svg viewBox="0 0 1 1">' + '<line x1="0" y1="0" x2="1" y2="1"/>' * 4100 + "</svg>")


def test_extract_svg_from_a_chatty_reply():
    assert extract_svg("Here you go:\n```svg\n" + OK_SVG + "\n```\nDone.") == OK_SVG
    assert extract_svg("no drawing here") is None


# ---------------------------------------------------------------------------------------------- clean text
@pytest.mark.parametrize("content, clean", [
    ("A stable temperature gradient as a Function of position", "A stable temperature gradient as a function of position"),
    ("increases with time But the rate $\\frac{dT}{dt}$ slows", "increases with time. But the rate $\\frac{dT}{dt}$ slows"),
    ("with time.But the rate", "with time. But the rate"),
    ("heat flows , slowly", "heat flows, slowly"),
    ("the temperatre rises", "the temperature rises"),  # one-letter slip of the pen in a long word
    ("Conduction: (Metallic Rod)", "Conduction: (metallic rod)"),
])
def test_guard_accepts_casing_punctuation_spacing_and_slips(content, clean):
    ok, why = check_clean(content, clean)
    assert ok, why


@pytest.mark.parametrize("content, clean, why", [
    ("the rate is 5 m/s", "the rate is 6 m/s", "numbers"),
    ("the rate is 5 m/s", "the rate is 5 km/s", "words"),  # unit changed
    ("$T_A > T_B$ hence", "$T_A < T_B$ hence", "math"),
    ("$T_A > T_B$ hence", "T_A > T_B hence", "math"),  # math unwrapped
    ("area A of the rod", "area a of the rod", "case"),  # a variable is not a capitalisation slip
    ("DNA is copied", "Dna is copied", "case"),  # acronym
    ("the pH value", "the Ph value", "case"),  # mixed-case token
    ("heat flows in", "heat flows into", "words"),
    ("heat flows", "heat moves", "words"),  # a different word
    ("heat flows", "heat flows quickly", "words"),  # added word
    ("Heat flows from A to B", "Heat flows from B to A", "words"),  # reordered
    ("x = 2y + 3", "x = 2y - 3", "symbols"),
    ("[illegible: 2 words] remains", "remains", "markup"),
    ("as written", "", "empty"),
])
def test_guard_rejects_meaning_changes(content, clean, why):
    ok, reason = check_clean(content, clean)
    assert not ok and why in reason, reason


def test_rule_normaliser_examples_and_safety():
    assert rule_clean("A stable gradient as a Function of position") == "A stable gradient as a function of position"
    assert rule_clean("increases with time But the rate") == "increases with time. But the rate"
    assert rule_clean("heat flows by Newton Law, as Fourier said") == "heat flows by Newton law, as Fourier said"
    assert rule_clean("Steady State :-", "heading") == "Steady State :-"  # headings: spacing only
    assert rule_clean("$T_A$ Is Hot") == "$T_A$ Is hot"  # after math nothing is assumed about the previous word
    # whatever it does passes the guard
    for t in ("Rate of Change of temperature with Position", "the Area A and the Length l", "x Is 5 But y Is 6"):
        assert check_clean(t, rule_clean(t))[0]


def test_resolve_prefers_a_valid_model_version_and_falls_back_to_rules():
    assert resolve_clean("as a Function of x", "as a function of x", "paragraph") == (
        "as a function of x", "model", None)
    clean, source, why = resolve_clean("rate is 5 m/s", "rate is 6 m/s", "paragraph")
    assert (clean, source) == ("rate is 5 m/s", "rules") and "numbers" in why
    assert resolve_clean("as a Function of x", None, "handwriting")[:2] == ("as a function of x", "rules")
    assert resolve_clean("E = mc^2", "E = mc^2", "equation") == (None, None, None)  # math elements: none
