"""Host mode: no model key; the MCP client's own model transcribes pages that need vision.

No test here calls a model: there is no key (tests/conftest.py strips them and pins host mode) and no
transport, except where a FakeTransport is injected on purpose to check it is NOT used.
"""

import io
import json

import pytest
from mcp import Client
from PIL import Image

from scholarocr.config import Settings
from scholarocr.exceptions import InputError, ModelUnavailableError, SubmissionRejected
from scholarocr.mcp.server import build_server
from scholarocr.pipeline import host
from scholarocr.pipeline.checkpoints import load_page
from scholarocr.schemas import PageStatus, ProcessingOptions
from scholarocr.service import ScholarOCR
from tests.fixtures.corpus import images_to_pdf, text_page_image
from tests.fixtures.fake_gemini import FakeTransport, default_page


def valid_page(text: str = "Q1. Solve x^2 + 5x + 6 = 0. [2]") -> dict:
    page = default_page(text)
    page["elements"][3]["latex"] = "\\int_0^1 x^2 \\, dx = \\frac{1}{3}"  # the fake's LaTeX is deliberately broken
    return page


@pytest.fixture
def svc(settings) -> ScholarOCR:
    s = ScholarOCR(settings)
    assert s.mode == "host"
    return s


# ---------------------------------------------------------------------------------------------- mode selection
@pytest.mark.parametrize("env, effective", [
    ({}, "host"),
    # default is host: exported keys alone never switch on a paid provider
    ({"GEMINI_API_KEY": "g"}, "host"),
    ({"OPENROUTER_API_KEY": "o"}, "host"),
    ({"GEMINI_API_KEY": "g", "OPENROUTER_API_KEY": "o"}, "host"),
    ({"SCHOLAROCR_PROVIDER": "gemini", "GEMINI_API_KEY": "g"}, "gemini"),
    ({"SCHOLAROCR_PROVIDER": "gemini", "GOOGLE_API_KEY": "g"}, "gemini"),
    ({"SCHOLAROCR_PROVIDER": "auto", "GEMINI_API_KEY": "g", "OPENROUTER_API_KEY": "o"}, "gemini"),  # auto prefers Gemini
    ({"SCHOLAROCR_PROVIDER": "openrouter", "GEMINI_API_KEY": "g", "OPENROUTER_API_KEY": "o"}, "openrouter"),
    ({"SCHOLAROCR_PROVIDER": "host", "GEMINI_API_KEY": "g", "OPENROUTER_API_KEY": "o"}, "host"),  # explicit host wins
    ({"SCHOLAROCR_PROVIDER": "gemini"}, "host"),  # requested provider without its key: host mode + note
    ({"SCHOLAROCR_PROVIDER": "openrouter", "GEMINI_API_KEY": "g"}, "host"),
    ({"SCHOLAROCR_PROVIDER": "auto", "OPENROUTER_API_KEY": "o"}, "openrouter"),
])
def test_provider_precedence(env, effective, tmp_path):
    s = Settings.from_env({**env, "SCHOLAROCR_DATA_DIR": str(tmp_path)})
    assert s.effective_provider == effective
    if effective == "host":
        assert s.api_key is None
    if env.get("SCHOLAROCR_PROVIDER") in ("gemini", "openrouter") and effective == "host":
        assert "host mode" in s.provider_note
    with pytest.raises(ValueError):
        Settings(provider="sampling")


def test_mode_reported_by_service(settings, tmp_path):
    assert ScholarOCR(settings).mode_info()["mode"] == "host"
    keyed = ScholarOCR(settings.model_copy(update={"gemini_api_key": "fake", "data_dir": tmp_path / "k"}))
    assert keyed.mode == "gemini" and keyed.info()["mode"] == "gemini"
    note = ScholarOCR(settings.model_copy(update={"provider": "openrouter", "data_dir": tmp_path / "n"})).mode_info()
    assert note["mode"] == "host" and "OPENROUTER_API_KEY" in note["note"]


def test_explicit_host_never_uses_a_transport(settings, corpus):
    t = FakeTransport()
    s = ScholarOCR(settings.model_copy(update={"provider": "host"}), transport=t)
    r = s.process(corpus["scanned_poor.pdf"])
    assert r["mode"] == "host" and r["awaiting_transcription"] == [1]
    assert t.calls == []


# ---------------------------------------------------------------------------------------------- deterministic pages
def test_digital_pdf_needs_no_submission(svc, corpus):
    r = svc.process(corpus["clean_digital.pdf"])
    assert r["status"] == "completed" and r["mode"] == "host"
    assert "awaiting_transcription" not in r and r["summary"]["routes"] == {"embedded_text": 2}
    data, mime, info = svc.host_page_image(r["document_id"])
    assert data is None and info["status"] == "nothing_to_transcribe"
    assert "Introduction to Algebra" in svc.get_content(r["document_id"], fmt="text")["content"]
    assert svc.usage(r["document_id"])["totals"]["calls"] == 0


def test_mixed_pdf_only_visual_page_waits(svc, corpus):
    r = svc.process(corpus["mixed.pdf"])
    assert r["summary"]["routes"] == {"embedded_text": 2, "visual_general": 1}
    waiting = r["awaiting_transcription"]
    assert len(waiting) == 1
    content = svc.get_content(r["document_id"])
    assert content["awaiting_transcription"] == waiting and content["content"]  # text pages readable already


# ---------------------------------------------------------------------------------------------- the loop
def test_full_host_loop(svc, corpus):
    r = svc.process(corpus["math_paper.pdf"], )
    doc = r["document_id"]
    assert r["status"] == "awaiting_transcription"
    job = svc.job_status(r["job_id"])
    assert job["status"] == "awaiting_transcription" and "awaiting transcription" in job["stage"]

    data, mime, info = svc.host_page_image(doc)
    img = Image.open(io.BytesIO(data))
    assert mime in ("image/png", "image/jpeg") and info["page_number"] == 1
    assert max(img.size) <= svc.settings.host_image_max_side
    page = load_page(svc.store, doc, 1)
    assert list(img.size) == list(page.processed_image_size) or max(img.size) == svc.settings.host_image_max_side
    assert "box_2d" in info["instructions"] and "submit_page_content" in info["instructions"]
    assert '"type":"table"' in info["instructions"]  # full instructions on the first page

    out = svc.submit_page_content(doc, 1, valid_page(), transcriber="antigravity")
    assert out["accepted"] and out["elements"] == 9 and out["pages_waiting"] == 0
    assert out["uncertainty"]["unresolved"] == 1
    assert any(x["type"] == "uncertain" for x in out.get("review_suggested", []))

    page = load_page(svc.store, doc, 1)
    assert page.status == PageStatus.DONE and page.model == "host:antigravity"
    assert all(e.extraction_method == "host_model" for e in page.elements)
    eq = next(e for e in page.elements if e.type.value == "equation")
    assert eq.bbox is not None and eq.bbox_source == "model_estimate"
    assert 0 <= eq.bbox.x0 < eq.bbox.x1 <= page.width_pt and 0 <= eq.bbox.y0 < eq.bbox.y1 <= page.height_pt
    assert eq.source.region_image  # crops saved like any extraction
    caption = next(e for e in page.elements if e.type.value == "caption")
    assert caption.parent_id == page.elements[5].element_id

    # Everything downstream works unchanged.
    assert svc.document_summary(doc)["status"] == "processed"
    md = svc.get_content(doc)["content"]
    assert "Mathematics" in md and "\\frac{1}{3}" in md and "<!-- el:" in md
    png, meta = svc.get_source_region(doc, element_id=eq.element_id)
    assert png[:4] == b"\x89PNG" and meta["bbox_pt"]
    report = svc.verify(doc)
    assert report.counts()["error"] == 0
    for fmt in ("json", "markdown", "searchable-pdf", "docx"):
        assert svc.export(doc, fmt)["primary_file"]
    assert svc.usage(doc)["totals"]["calls"] == 0  # no server-side model call at any point


def test_multi_page_one_image_per_call(svc, tmp_path):
    pdf = images_to_pdf([text_page_image([f"Sheet {i} handwritten line"] * 6) for i in range(1, 4)], tmp_path / "3.pdf")
    r = svc.process(pdf, wait=True)
    doc = r["document_id"]
    assert r["awaiting_transcription"] == [1, 2, 3]
    seen = []
    for expected in (1, 2, 3):
        data, _, info = svc.host_page_image(doc)
        assert info["page_number"] == expected and data
        if expected == 1:
            assert '"type":"table"' in info["instructions"]
        else:  # short reminder after the first submission keeps the context small
            assert '"type":"table"' not in info["instructions"] and "Same rules" in info["instructions"]
        out = svc.submit_page_content(doc, expected, {"elements": [
            {"type": "paragraph", "text": f"Sheet {expected} handwritten line", "box_2d": [100, 100, 150, 900]}]})
        seen.append(out["next_waiting_page"])
    assert seen == [2, 3, None]
    assert svc.job_status(r["job_id"])["status"] == "awaiting_transcription"  # the job record is historical
    assert svc.document_summary(doc)["status"] == "processed"
    # Re-running ocr_document reuses the host transcriptions (same mode, same cache key).
    again = svc.process(pdf, wait=True)
    assert again["status"] == "completed" and svc.job_status(again["job_id"])["cached_pages"] == 3


def test_rerun_keeps_waiting_pages_and_switching_to_a_key_reextracts(settings, corpus, tmp_path):
    s = ScholarOCR(settings)
    r = s.process(corpus["scanned_poor.pdf"])
    task_file = host.task_path(s.store, r["document_id"], 1)
    before = task_file.stat().st_mtime_ns
    r2 = s.process(corpus["scanned_poor.pdf"])
    assert r2["awaiting_transcription"] == [1] and task_file.stat().st_mtime_ns == before  # not re-prepared
    t = FakeTransport()
    keyed = ScholarOCR(settings, transport=t)
    r3 = keyed.process(corpus["scanned_poor.pdf"])
    assert r3["mode"] == "gemini" and r3["status"] == "completed" and t.calls


# ---------------------------------------------------------------------------------------------- validation
def test_invalid_submission_lists_every_problem_and_stores_nothing(svc, corpus):
    doc = svc.process(corpus["scanned_poor.pdf"])["document_id"]
    bad = {"elements": [
        {"type": "paragraph", "bbox": [1, 2, 3, 4], "text": "x"},                 # unknown field
        {"type": "paragrph", "text": "y"},                                        # bad type
        {"type": "heading", "text": "z", "box_2d": [500, 100, 400, 900]},         # ymin > ymax
        {"type": "heading", "text": "z", "box_2d": [10, 20, 30, 1400]},           # out of range
        {"type": "equation", "text": "1/2", "latex": "\\frac{1}{2"},              # broken LaTeX, low doubt
        {"type": "caption", "text": "Fig", "caption_of": 9},                      # bad index
        {"type": "table"},                                                        # no rows
        {"type": "paragraph", "text": ""},                                        # empty text
    ], "rotation_detected": 45}
    with pytest.raises(SubmissionRejected) as ei:
        svc.submit_page_content(doc, 1, bad)
    errs = ei.value.details["errors"]
    paths = [e["path"] for e in errs]
    assert "elements[0].bbox" in paths and next(e for e in errs if e["path"] == "elements[0].bbox")["fix"] == "use 'box_2d'"
    assert any(p.startswith("elements[1].type") for p in paths)
    # pydantic errors stop the semantic pass; fix the type and the rest is reported in one round
    bad["elements"][1]["type"] = "paragraph"
    with pytest.raises(SubmissionRejected) as ei:
        svc.submit_page_content(doc, 1, bad)
    errs = {e["path"]: e for e in ei.value.details["errors"]}
    assert "ymin < ymax" in errs["elements[2].box_2d"]["problem"]
    assert "0-1000" in errs["elements[3].box_2d"]["problem"]
    assert "unbalanced braces" in errs["elements[4].latex"]["problem"] and "high" in errs["elements[4].latex"]["fix"]
    assert "elements[5].caption_of" in errs and "elements[6].table" in errs and "elements[7].text" in errs
    assert "rotation_detected" in errs
    page = load_page(svc.store, doc, 1)
    assert page.status == PageStatus.AWAITING_TRANSCRIPTION and page.elements == []
    # Ambiguous LaTeX is accepted when the model says it is unsure (uncertainty escalated, not rejected).
    ok = svc.submit_page_content(doc, 1, {"elements": [
        {"type": "equation", "text": "1/2", "latex": "\\frac{1}{2", "uncertainty": "high", "box_2d": [10, 10, 50, 500]}]})
    assert ok["accepted"] and any("unbalanced" in w for w in ok["warnings"])


def test_lenient_normalisation_and_wrong_pages(svc, corpus):
    doc = svc.process(corpus["scanned_poor.pdf"])["document_id"]
    ok = svc.submit_page_content(doc, 1, {"elements": [
        {"type": "table", "box_2d": ["100", "50", "300", "950"], "table": {"rows": [["a", "b"], ["1", "2"]]}}]})
    assert ok["accepted"]
    el = load_page(svc.store, doc, 1).elements[0]
    assert el.table.as_grid() == [["a", "b"], ["1", "2"]] and el.bbox is not None
    # a host page can be resubmitted (replaces it); other pages and out-of-range numbers are refused
    again = svc.submit_page_content(doc, 1, {"elements": [{"type": "paragraph", "text": "new", "box_2d": [1, 1, 9, 9]}]})
    assert again["accepted"] and load_page(svc.store, doc, 1).elements[0].content == "new"
    from scholarocr.exceptions import InputError

    with pytest.raises(InputError, match="between 1 and 1"):
        svc.submit_page_content(doc, 2, {"elements": []})
    digital = svc.process(svc.settings.allowed_dirs[0] / "clean_digital.pdf")["document_id"]
    with pytest.raises(InputError, match="does not need transcription"):
        svc.submit_page_content(digital, 1, {"elements": []})


def test_reported_rotation_reorients_and_asks_again(svc, corpus):
    doc = svc.process(corpus["scanned_poor.pdf"])["document_id"]
    _, _, info = svc.host_page_image(doc)
    w, h = info["image"]["width_px"], info["image"]["height_px"]
    out = svc.submit_page_content(doc, 1, {"elements": [{"type": "paragraph", "text": "x"}], "rotation_detected": 90})
    assert out["accepted"] is False and out["rotated"] is True
    page = load_page(svc.store, doc, 1)
    assert page.status == PageStatus.AWAITING_TRANSCRIPTION and page.elements == []
    _, _, info2 = svc.host_page_image(doc, 1)
    assert (info2["image"]["width_px"], info2["image"]["height_px"]) != (w, h)  # rotated image now served
    assert host.load_task(svc.store, doc, 1).rotations == 1


# ---------------------------------------------------------------------------------------------- review & model-only tools
def test_host_review_pass(svc, corpus):
    doc = svc.process(corpus["math_paper.pdf"])["document_id"]
    svc.submit_page_content(doc, 1, valid_page())
    report = svc.verify(doc, review=True)
    assert report.review_candidates and "submit_review" in report.notes[0]
    target = next(c for c in report.review_candidates if c["type"] == "uncertain")
    with pytest.raises(SubmissionRejected):
        svc.submit_review(doc, target["element_id"], {"verdict": "corrected"})  # correction without content
    with pytest.raises(SubmissionRejected):
        svc.submit_review(doc, target["element_id"], {"verdict": "maybe"})
    res = svc.submit_review(doc, target["element_id"], {"verdict": "corrected", "text": "three legible words",
                                                        "uncertainty": "medium"}, transcriber="claude-code")
    assert res["verdict"] == "corrected" and res["uncertainty"] == "medium"
    _, el = svc.find_element(doc, target["element_id"])
    assert el.content == "three legible words" and el.revisions[0].previous == "[illegible: 3 words]"
    assert el.revisions[-1].by == "review:host:claude-code"
    assert target["element_id"] not in [c["element_id"] for c in svc.host_review_candidates(doc)]
    fig = next(e for e in load_page(svc.store, doc, 1).elements if e.type.value == "figure")
    svc.submit_review(doc, fig.element_id, {"verdict": "confirmed", "figure": {"description": "An upward parabola",
                                                                              "labels": ["x"], "kind": "graph"}})
    _, fig2 = svc.find_element(doc, fig.element_id)
    assert fig2.figure.description == "An upward parabola" and fig2.revisions[-1].field == "figure"


def test_classify_needs_a_model_in_host_mode(svc, corpus):
    doc = svc.process(corpus["clean_digital.pdf"])["document_id"]
    with pytest.raises(ModelUnavailableError, match="classify the document yourself"):
        svc.classify(doc)


# ---------------------------------------------------------------------------------------------- over MCP
async def test_host_loop_over_mcp(settings, corpus):
    server = build_server(ScholarOCR(settings))
    async with Client(server) as c:
        r = await c.call_tool("ocr_document", {"file_path": str(corpus["math_paper.pdf"])})
        sc = r.structured_content
        assert sc["mode"] == "host" and sc["awaiting_transcription"] == [1]
        doc = sc["document_id"]

        img = await c.call_tool("get_page_image", {"document_id": doc})
        assert [b.type for b in img.content] == ["image", "text"]
        assert img.content[0].mime_type in ("image/png", "image/jpeg")
        assert "Page info:" in img.content[1].text and "box_2d" in img.content[1].text

        bad = await c.call_tool("submit_page_content", {"document_id": doc, "page_number": 1,
                                                        "elements": [{"type": "paragraph", "content": "x"}]})
        assert bad.is_error
        text = bad.content[0].text
        err = json.loads(text[text.index("{"):])  # the SDK prefixes "Error executing tool ...: "
        assert err["error"] == "submission_rejected"
        assert {"path": "elements[0].content", "problem": "unknown field", "fix": "use 'text'"} in err["details"]["errors"]

        page = valid_page()
        ok = await c.call_tool("submit_page_content", {"document_id": doc, "page_number": 1, **page})
        assert not ok.is_error and ok.structured_content["accepted"] is True

        done = await c.call_tool("get_page_image", {"document_id": doc})
        assert len(done.content) == 1 and "nothing_to_transcribe" in done.content[0].text

        content = await c.call_tool("get_document_content", {"document_id": doc, "element_types": ["equation"]})
        assert "\\frac{1}{3}" in content.structured_content["content"]
        ver = await c.call_tool("verify_document", {"document_id": doc, "review": True})
        assert ver.structured_content["review_candidates"]
        el_id = ver.structured_content["review_candidates"][0]["element_id"]
        rev = await c.call_tool("submit_review", {"document_id": doc, "element_id": el_id, "verdict": "confirmed",
                                                  "uncertainty": "low"})
        assert not rev.is_error
        exp = await c.call_tool("export_document", {"document_id": doc, "format": "markdown"})
        assert exp.structured_content["primary_file"].endswith(".md")
        cls = await c.call_tool("classify_document", {"document_id": doc})
        assert cls.is_error and "model_unavailable" in cls.content[0].text


def test_user_env_file_is_lowest_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # never the repo root: its .env may hold a real key
    user_file = tmp_path / "user.env"
    user_file.write_text("GEMINI_API_KEY=from-user-file\nGEMINI_MODEL=user-model\n", encoding="utf-8")
    (tmp_path / ".env").write_text("GEMINI_MODEL=workspace-model\n", encoding="utf-8")
    monkeypatch.setenv("SCHOLAROCR_ENV_FILE", str(user_file))
    monkeypatch.setenv("SCHOLAROCR_PROVIDER", "auto")
    s = Settings.from_env()
    assert s.gemini_api_key == "from-user-file" and s.gemini_model == "workspace-model"  # workspace .env wins
    assert s.effective_provider == "gemini"
    monkeypatch.setenv("SCHOLAROCR_PROVIDER", "host")  # explicit host still wins over any key
    assert Settings.from_env().effective_provider == "host"
    monkeypatch.delenv("SCHOLAROCR_PROVIDER")  # unset = host by default, even with a key in the user file
    assert Settings.from_env().effective_provider == "host"


# ---------------------------------------------------------------------------------------------- 0.3.0 regressions
def _clean_page_png(path):
    from tests.unit.test_regressions import clean_digital_page

    clean_digital_page().save(path, dpi=(150, 150))
    return path


def test_clean_digital_page_image_is_not_routed_blank(svc, tmp_path):
    r = svc.process(_clean_page_png(tmp_path / "arxiv_like.png"))
    assert r["awaiting_transcription"] == [1] and "blank_pages" not in r
    assert not any("skipped as blank" in w for w in r["warnings"])


def test_blank_skip_is_warned_and_force_visual_reprocess_reads_it(svc, tmp_path):
    from PIL import Image as PILImage

    blank = tmp_path / "blank.png"
    PILImage.new("RGB", (1200, 1600), "white").save(blank)
    r = svc.process(blank)
    assert r["blank_pages"] == [1] and any("skipped as blank" in w and "force_visual=true" in w for w in r["warnings"])
    r2 = svc.process(blank, ProcessingOptions(force_visual=True, reprocess=True))
    assert r2["awaiting_transcription"] == [1] and "blank_pages" not in r2


def test_stale_blank_flag_is_rebuilt_on_reprocess_and_checked_at_full_resolution(svc, tmp_path):
    src = _clean_page_png(tmp_path / "stale.png")
    doc = svc.process(src)["document_id"]
    mpath = svc.store.doc_dir(doc) / "manifest.json"
    m = json.loads(mpath.read_text(encoding="utf-8"))
    m["pages"][0]["profile"]["is_blank"] = True  # what the 0.2 detector stored for such pages
    mpath.write_text(json.dumps(m), encoding="utf-8")
    svc.delete(doc)  # forget the extracted page; keep the (stale) manifest
    r = svc.process(src)
    assert r["awaiting_transcription"] == [1]  # full-resolution check overrules the stale flag
    page = load_page(svc.store, doc, 1)
    assert any("full-resolution check found ink" in w for w in page.warnings)
    r = svc.process(src, ProcessingOptions(reprocess=True))
    assert not svc.load_manifest(doc).pages[0].profile.is_blank and r["awaiting_transcription"] == [1]


def test_page_doubt_does_not_raise_element_labels(svc, corpus):
    doc = svc.process(corpus["math_paper.pdf"])["document_id"]
    page = valid_page()
    page.pop("legibility", None)
    out = svc.submit_page_content(doc, 1, {**page, "page_doubt": "high"})
    assert out["uncertainty"].get("low", 0) >= 6  # clean elements stay clean
    p = load_page(svc.store, doc, 1)
    assert "model note: overall page doubt reported as high" in p.warnings
    out = svc.submit_page_content(doc, 1, {**page, "legibility": "high"})  # deprecated name: same meaning
    assert any("old name of page_doubt" in w for w in out["warnings"]) and out["uncertainty"].get("low", 0) >= 6


def test_confirmed_review_is_not_re_raised_by_the_same_rule(svc, corpus):
    doc = svc.process(corpus["math_paper.pdf"])["document_id"]
    page = valid_page()
    page["elements"].append({"type": "chemical_equation", "text": "CH4 + Cl2 CH3Cl",
                             "latex": r"\ce{CH4 + Cl2 CH3Cl}", "box_2d": [800, 100, 830, 600]})
    svc.submit_page_content(doc, 1, page)
    el = next(e for e in load_page(svc.store, doc, 1).elements if e.type.value == "chemical_equation")
    assert el.uncertainty.value == "medium" and "chemistry: reaction without an arrow" in el.uncertainty_reasons
    res = svc.submit_review(doc, el.element_id, {"verdict": "confirmed", "uncertainty": "low"})
    assert res["uncertainty"] == "low"
    _, again = svc.find_element(doc, el.element_id)
    assert again.uncertainty.value == "low"


def test_verify_collapses_unselected_pages_and_compact_page_content(svc, corpus):
    doc = svc.process(corpus["mixed.pdf"], ProcessingOptions(pages="1"))["document_id"]
    report = svc.verify(doc)
    not_processed = [f for f in report.findings if f.check == "completeness"]
    assert len(not_processed) == 1 and not_processed[0].severity.value == "info"
    assert "not selected" in not_processed[0].message and not_processed[0].data["pages"] == [2, 3]
    full = svc.get_page(doc, 1)
    compact = svc.get_page(doc, 1, compact=True)
    assert len(json.dumps(compact)) < len(json.dumps(full)) / 2
    el = compact["elements"][0]
    assert set(el) <= {"element_id", *svc.COMPACT_FIELDS, "bbox"} and "source" not in el
    only = svc.get_page(doc, 1, fields=["type"])
    assert all(set(e) == {"element_id", "type"} for e in only["elements"])
    with pytest.raises(InputError):
        svc.get_page(doc, 1, fields=["nope"])


async def test_malformed_elements_over_mcp_get_the_validator_answer(settings, corpus):
    server = build_server(ScholarOCR(settings))
    async with Client(server) as c:
        doc = (await c.call_tool("ocr_document", {"file_path": str(corpus["math_paper.pdf"])})).structured_content[
            "document_id"]
        bad = await c.call_tool("submit_page_content", {"document_id": doc, "page_number": 1,
                                                        "elements": [0, 1, 2]})
        assert bad.is_error
        text = bad.content[0].text
        err = json.loads(text[text.index("{"):])
        assert err["error"] == "submission_rejected" and "validation error" not in text
        assert [e["path"] for e in err["details"]["errors"]] == ["elements[0]", "elements[1]", "elements[2]"]
        page = valid_page()
        ok = await c.call_tool("submit_page_content", {"document_id": doc, "page_number": 1,
                                                       "elements": json.dumps(page["elements"]),
                                                       "page_doubt": "low"})
        assert not ok.is_error and ok.structured_content["accepted"]
        compact = await c.call_tool("get_page_content", {"document_id": doc, "page_number": 1, "compact": True})
        assert "source" not in compact.structured_content["elements"][0]
