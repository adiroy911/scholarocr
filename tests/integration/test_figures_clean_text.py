"""Host mode end to end on a synthetic ruled page: boxes refined to the ink, figure crop and cleaned crop, cleaned
text produced and guarded, text_variant in get_document_content, SVG redraw submission and the MCP tools.
No model is called (host mode: the test plays the client's model)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp import Client

from scholarocr.exceptions import InputError, ModelUnavailableError
from scholarocr.mcp.server import build_server
from scholarocr.pipeline.checkpoints import load_page
from scholarocr.service import ScholarOCR
from tests.unit.test_layout_refine import H, W, ruled_page

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 120" onload="alert(1)"><script>alert(2)</script>'
       '<rect x="10" y="10" width="100" height="80" fill="none" stroke="#111"/>'
       '<line x1="110" y1="50" x2="280" y2="50" stroke="#111"/><text x="20" y="55">T<tspan dy="4">A</tspan></text></svg>')


def box(px):  # pixel box (x0, y0, x1, y1) of the synthetic page -> box_2d in 0-1000
    x0, y0, x1, y1 = px
    return [round(y0 / H * 1000), round(x0 / W * 1000), round(y1 / H * 1000), round(x1 / W * 1000)]


@pytest.fixture
def doc(settings, tmp_path):
    img, _ = ruled_page()
    path = tmp_path / "notes.png"
    img.save(path, dpi=(200, 200))
    svc = ScholarOCR(settings)
    r = svc.process(path)
    assert r["awaiting_transcription"] == [1]
    payload = {"page_language": "en", "elements": [
        {"type": "handwriting", "handwritten": True, "text": "Heat flows as a Function of x But slowly",
         "clean_text": "Heat flows as a function of x. But slowly", "box_2d": box((205, 132, 1215, 186))},
        {"type": "figure", "figure": {"description": "A box with an arrow to the right", "labels": ["T_A", "x"]},
         "box_2d": box((535, 540, 1080, 700))},
        {"type": "handwriting", "handwritten": True, "text": "the rod is 5 m Long",
         "clean_text": "the rod is 6 m long", "box_2d": box((210, 815, 1080, 870))},  # changes a number: refused
    ]}
    out = svc.submit_page_content(r["document_id"], 1, payload, transcriber="test")
    assert out["accepted"]
    return svc, r["document_id"]


def test_boxes_refined_and_figure_images_saved(doc):
    svc, doc_id = doc
    page = load_page(svc.store, doc_id, 1)
    text, fig, below = page.elements
    assert {e.bbox_source for e in page.elements} == {"refined"}
    assert all(e.bbox_model is not None and e.bbox != e.bbox_model for e in page.elements)
    assert fig.source.region_image == f"pages/0001/figures/{fig.element_id}.png"
    assert fig.figure.image_clean == f"pages/0001/figures/{fig.element_id}.clean.png"
    for rel in (fig.source.region_image, fig.figure.image_clean):
        assert (svc.store.doc_dir(doc_id) / rel).is_file()
    # the figure grew to the T_A label left of its model box, and stopped before the text line under it
    assert fig.bbox.x0 < fig.bbox_model.x0 and fig.bbox.y1 < below.bbox.y0
    png, meta = svc.get_source_region(doc_id, element_id=fig.element_id)
    assert meta["bbox_source"] == "refined"


def test_clean_content_from_the_model_is_guarded(doc):
    svc, doc_id = doc
    text, _, below = load_page(svc.store, doc_id, 1).elements
    assert text.content == "Heat flows as a Function of x But slowly"  # as written, untouched
    assert (text.clean_content, text.clean_content_source) == ("Heat flows as a function of x. But slowly", "model")
    # the model's version changed a number: refused, the rule normaliser used instead
    assert below.clean_content == "the rod is 5 m long" and below.clean_content_source == "rules"
    assert "numbers" in below.formatting["clean_text_rejected"]


def test_text_variant_in_content(doc):
    svc, doc_id = doc
    auto = svc.get_content(doc_id, fmt="text")  # handwritten document: cleaned by default
    assert auto["text_variant"] == "clean" and "function of x. But" in auto["content"]
    faithful = svc.get_content(doc_id, fmt="markdown", text_variant="faithful")
    assert "a Function of x But slowly" in faithful["content"]
    js = svc.get_content(doc_id, fmt="json")
    el = js["pages"][0]["elements"][0]
    assert el["content"] != el["clean_content"]
    with pytest.raises(InputError):
        svc.get_content(doc_id, text_variant="fancy")


def test_redraw_svg_is_sanitised_and_exported(doc, tmp_path):
    svc, doc_id = doc
    fig = load_page(svc.store, doc_id, 1).elements[1]
    with pytest.raises(ModelUnavailableError):
        svc.redraw_figure(doc_id, fig.element_id)  # host mode: the client's model draws it
    png, info = svc.figure_redraw_request(doc_id, fig.element_id)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and "WITHOUT CHANGING A THING" in info["instructions"]
    assert info["view_box"] == f"0 0 {info['image_size_px'][0]} {info['image_size_px'][1]}"
    # This SVG is not a copy of the drawing: refused with feedback, nothing stored, one retry allowed ...
    out = svc.submit_figure_svg(doc_id, fig.element_id, "Here it is:\n" + SVG)
    assert not out["accepted"] and out["retry_allowed"] and not out["faithful"] and out["feedback"]
    assert "element <script>" in out["removed"] and "attribute 'onload' on <svg>" in out["removed"]
    assert load_page(svc.store, doc_id, 1).elements[1].figure.svg is None
    # ... and after the second failure it is kept for inspection but marked not faithful
    out = svc.submit_figure_svg(doc_id, fig.element_id, SVG)
    assert not out["accepted"] and not out["retry_allowed"] and out["svg"]
    stored = (svc.store.doc_dir(doc_id) / out["svg"]).read_text(encoding="utf-8")
    assert "script" not in stored and "onload" not in stored
    after = load_page(svc.store, doc_id, 1).elements[1]
    assert after.figure.svg == out["svg"] and after.source.region_image == fig.source.region_image  # crop kept
    assert after.figure.svg_fidelity["passed"] is False and 0 <= after.figure.svg_fidelity["score"] < 1
    md = svc.export(doc_id, "markdown", options={"figure_variant": "redrawn"})
    text = Path(md["primary_file"]).read_text(encoding="utf-8")
    assert f"assets/{fig.element_id}.svg" not in text and f"assets/{fig.element_id}.clean.png" in text
    assert any("not faithful" in w for w in md["warnings"])
    assert svc.figure_crop(doc_id, fig.element_id, "svg")[1] == "image/svg+xml"  # still viewable in the app
    with pytest.raises(InputError):
        svc.submit_figure_svg(doc_id, fig.element_id, "<html/>")
    with pytest.raises(InputError, match="not a figure"):
        svc.submit_figure_svg(doc_id, load_page(svc.store, doc_id, 1).elements[0].element_id, SVG)


async def test_mcp_redraw_tools_and_variants(doc):
    svc, doc_id = doc
    fig = load_page(svc.store, doc_id, 1).elements[1]
    async with Client(build_server(svc)) as c:
        r = await c.call_tool("redraw_figure", {"document_id": doc_id, "element_id": fig.element_id})
        kinds = [getattr(x, "type", None) for x in r.content]
        assert "image" in kinds and any("submit_figure_svg" in getattr(x, "text", "") for x in r.content)
        r = await c.call_tool("submit_figure_svg", {"document_id": doc_id, "element_id": fig.element_id, "svg": SVG})
        assert not r.is_error
        body = json.loads(r.content[0].text)
        assert body["accepted"] is False and body["retry_allowed"] is True and body["feedback"]  # not a copy
        r = await c.call_tool("get_document_content", {"document_id": doc_id, "text_variant": "faithful"})
        body = json.loads(r.content[0].text)
        assert body["text_variant"] == "faithful" and "a Function of x" in body["content"]
        r = await c.call_tool("export_document", {"document_id": doc_id, "format": "markdown",
                                                  "text_variant": "clean", "figure_variant": "redrawn"})
        body = json.loads(r.content[0].text)
        assert body["verification"]["text_variant"] == "clean" and body["verification"]["figure_variant"] == "svg"


def _make_old(svc, doc_id):
    """Turn the stored page into what a version without refinement, crops and cleaned text left behind."""
    from scholarocr.pipeline.checkpoints import save_page

    page = load_page(svc.store, doc_id, 1)
    for el in page.elements:
        if el.bbox_model is not None:
            el.bbox, el.bbox_model = el.bbox_model, None
        el.bbox_source = "model_estimate"
        el.clean_content = el.clean_content_source = None
        el.source.region_image = None
        if el.figure:
            el.figure.image_clean = None
    page.layout_refined_at = None
    save_page(svc.store, page)
    return page


def test_update_layout_of_an_old_document(doc):
    svc, doc_id = doc
    old = _make_old(svc, doc_id)
    status = svc.layout_outdated(doc_id)
    assert status["outdated"] and status["counts"] == {"boxes": 3, "figures": 1, "clean_text": 2}
    res = svc.refine_document(doc_id)
    assert res["totals"] == {"refined": 3, "kept_model_box": 0, "figures": 1, "figures_cleaned": 1,
                             "clean_text_added": 2}
    page = load_page(svc.store, doc_id, 1)
    assert page.layout_refined_at and {e.bbox_source for e in page.elements} == {"refined"}
    text, fig, below = page.elements
    assert fig.source.region_image and fig.figure.image_clean
    # cleaned text from the rule normaliser (no model): same words and numbers, a mid-sentence capital lowered
    assert text.clean_content_source == "rules" and text.content == old.elements[0].content
    assert text.clean_content.lower().replace(".", "").split() == text.content.lower().split()
    assert "a function of x" in text.clean_content and "5 m" in below.clean_content
    assert svc.layout_outdated(doc_id)["outdated"] is False
    again = svc.refine_document(doc_id)  # idempotent; existing cleaned text is kept
    assert again["totals"]["clean_text_added"] == 0 and load_page(svc.store, doc_id, 1).elements[0].clean_content \
        == text.clean_content


async def test_update_layout_mcp_tool(doc, settings):
    svc, doc_id = doc
    _make_old(svc, doc_id)
    async with Client(build_server(svc)) as c:
        r = await c.call_tool("update_layout", {"document_id": doc_id})
        data = json.loads(r.content[0].text)
    assert data["totals"]["refined"] == 3 and data["totals"]["clean_text_added"] == 2
