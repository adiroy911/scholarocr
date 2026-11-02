import io
import threading

import pymupdf
import pytest
from PIL import Image

from scholarocr.exceptions import AccessDeniedError, InputError
from scholarocr.pipeline.checkpoints import load_page
from scholarocr.schemas import ElementType, JobStatus, PageStatus, ProcessingOptions, Uncertainty
from scholarocr.service import ScholarOCR
from tests.fixtures.corpus import images_to_pdf, text_page_image
from tests.fixtures.fake_gemini import APIErrorLike, FakeTransport, default_page


def svc(settings, transport=None) -> ScholarOCR:
    return ScholarOCR(settings, transport=transport)


def extract_calls(t: FakeTransport) -> list[dict]:
    return [c for c in t.calls if c["kind"] == "extract"]


def test_digital_pdf_is_deterministic_and_cached(settings, corpus):
    t = FakeTransport()
    s = svc(settings, t)
    r = s.process(corpus["clean_digital.pdf"])
    assert r["status"] == "completed" and r["synchronous"]
    summary = r["summary"]
    assert summary["routes"] == {"embedded_text": 2} and summary["pages_by_status"] == {"done": 2}
    assert t.calls == []  # no Gemini call for a reliable text layer
    doc = s.get_document(r["document_id"])
    assert doc.title == "Introduction to Algebra"
    questions = [e for e in doc.iter_elements() if e.role.value == "question"]
    assert [q.question.marks for q in questions[:3]] == ["2", "3", "5"]
    assert all(e.bbox_source == "pdf_text_layer" for e in doc.iter_elements())
    r2 = s.process(corpus["clean_digital.pdf"])
    assert s.job_status(r2["job_id"])["cached_pages"] == 2
    r3 = s.process(corpus["clean_digital.pdf"], ProcessingOptions(reprocess=True))
    assert s.job_status(r3["job_id"])["cached_pages"] == 0


def test_scanned_page_without_key_fails_clearly(settings, corpus):
    s = svc(settings)  # no transport, no API key
    r = s.process(corpus["scanned_poor.pdf"])
    assert r["status"] == "failed"
    page = s.get_page(r["document_id"], 1)
    assert page["status"] == "failed" and "GEMINI_API_KEY" in page["error"]
    assert s.document_summary(r["document_id"])["failed_pages"][0]["page"] == 1


def test_scanned_math_visual_pipeline(settings, corpus):
    t = FakeTransport()
    settings.gemini_math_model = "math-model"
    s = svc(settings, t)
    r = s.process(corpus["math_paper.pdf"], ProcessingOptions(context_hint="Class 12 mathematics exam",
                                                                document_type="exam_paper"))
    assert r["status"] == "completed"
    page = load_page(s.store, r["document_id"], 1)
    assert page.route.value == "visual_math" and page.model == "math-model"
    # Resolution is judged from measured character height, not DPI metadata: 16 px glyphs are readable.
    assert page.quality.glyph_height_px and page.quality.glyph_height_px >= 12
    assert not any(tr.step == "upscale" for tr in page.transforms)
    assert (s.store.doc_dir(r["document_id"]) / page.processed_image).exists()
    ex = extract_calls(t)
    assert len(ex) == 1 and "may be wrong" in ex[0]["text"]
    reviews = [c for c in t.calls if c["kind"] == "review"]
    assert reviews  # flagged elements (bad LaTeX / poor quality) were reviewed with the review model
    assert all(c["model"] == settings.gemini_review_model for c in reviews)
    eqs = [e for e in page.elements if e.type == ElementType.EQUATION]
    assert all(e.source.region_image for e in eqs)
    unc = [e for e in page.elements if e.type == ElementType.UNCERTAIN][0]
    assert unc.uncertainty == Uncertainty.UNRESOLVED and "[illegible" in unc.content
    png, meta = s.get_source_region(r["document_id"], element_id=eqs[0].element_id)
    assert Image.open(io.BytesIO(png)).size[0] > 10 and meta["bbox_source"] == "model_estimate"
    usage = s.usage(r["document_id"])
    assert usage["totals"]["calls"] >= 2 and usage["estimated_cost"] is None


def test_mixed_pdf_routes_per_page(settings, corpus):
    t = FakeTransport()
    s = svc(settings, t)
    r = s.process(corpus["mixed.pdf"])
    assert r["summary"]["routes"] == {"embedded_text": 2, "visual_general": 1}
    assert len(extract_calls(t)) == 1


def test_resume_after_page_failure_skips_finished_pages(settings, tmp_path):
    pdf = images_to_pdf([text_page_image([f"Page {i} content line"] * 8) for i in range(1, 4)], tmp_path / "three.pdf")
    state = {"fail": True}
    calls: list[int] = []

    class PageAwareTransport(FakeTransport):
        def generate(self, **kw):
            page_no = len([c for c in self.calls if c["kind"] == "extract"]) + 1
            if self.kind(kw["system"]) == "extract":
                calls.append(page_no)
                if state["fail"] and page_no == 2:
                    self.calls.append({"kind": "extract", "model": kw["model"], "text": "", "n_images": 1})
                    raise APIErrorLike(400, "INVALID_ARGUMENT")
            return super().generate(**kw)

    settings.concurrency = 1
    s = svc(settings, PageAwareTransport())
    r = s.process(pdf, ProcessingOptions(review=False))
    job = s.job_status(r["job_id"])
    assert job["status"] == "partial" and job["failed_pages"] == 1 and job["completed_pages"] == 2
    assert load_page(s.store, r["document_id"], 2).status == PageStatus.FAILED
    state["fail"] = False
    before = len(calls)
    resumed = s.resume_job(r["job_id"], wait=True)
    assert resumed["status"] == "completed" and resumed["cached_pages"] == 2
    assert len(calls) - before == 1  # only the failed page was re-extracted


def test_background_job_cancel_and_resume(settings, tmp_path):
    pdf = images_to_pdf([text_page_image([f"Sheet {i} of the stack"] * 8) for i in range(1, 7)], tmp_path / "six.pdf")
    gate = threading.Event()

    def slow(kind, parts, idx):
        gate.wait(5)
        return default_page()

    settings.concurrency = 1
    s = svc(settings, FakeTransport(slow))
    r = s.process(pdf, ProcessingOptions(review=False))
    assert not r["synchronous"] and r["status"] in ("queued", "running")
    s.cancel_job(r["job_id"])
    gate.set()
    job = s.wait_for_job(r["job_id"], timeout=30)
    assert job["status"] == "cancelled" and job["completed_pages"] < 6
    resumed = s.resume_job(r["job_id"], wait=True)
    assert resumed["status"] == "completed" and resumed["completed_pages"] == 6


def test_interrupted_job_is_recovered_and_resumable(settings, corpus):
    s = svc(settings, FakeTransport())
    r = s.process(corpus["clean_digital.pdf"])
    s.repo.update_job(r["job_id"], status=JobStatus.RUNNING, pid=999_999_9)  # dead pid
    assert s.job_status(r["job_id"])["status"] == "interrupted"
    assert s.resume_job(r["job_id"], wait=True)["status"] == "completed"


def test_rotation_reported_by_model_triggers_one_reextract(settings, corpus):
    def responder(kind, parts, idx):
        page = default_page()
        if kind == "extract" and idx == 0:
            page["rotation_detected"] = 180
        return page

    t = FakeTransport(responder)
    s = svc(settings, t)
    r = s.process(corpus["chemistry.png"], ProcessingOptions(review=False))
    page = load_page(s.store, r["document_id"], 1)
    assert len(extract_calls(t)) == 2
    assert page.orientation == 180 and any("re-extracted" in w for w in page.warnings)


def test_figures_on_text_pages_get_targeted_description(settings, corpus):
    def responder(kind, parts, idx):
        return {"description": "A rectangular circuit loop with a battery", "labels": ["V", "R"], "kind": "circuit"}

    t = FakeTransport(responder)
    s = svc(settings, t)
    r = s.process(corpus["physics_diagram.pdf"])
    page = load_page(s.store, r["document_id"], 1)
    assert page.route.value == "embedded_text"
    diagram = next(e for e in page.elements if e.type == ElementType.DIAGRAM)
    assert diagram.figure.description.startswith("A rectangular") and set(diagram.figure.labels) == {"V", "R"}
    assert diagram.source.region_image and (s.store.doc_dir(r["document_id"]) / diagram.source.region_image).exists()
    caption = next(e for e in page.elements if e.type == ElementType.CAPTION)
    assert caption.parent_id is not None
    assert len(t.calls) == 2  # one description per visual element; no full-page extraction


def test_handwriting_hint_routes_to_handwriting_profile(settings, corpus):
    t = FakeTransport()
    s = svc(settings, t)
    s.process(corpus["handwritten.png"], ProcessingOptions(document_type="handwritten_notes", review=False))
    assert "handwritten" in extract_calls(t)[0]["text"].lower()


def test_verification_findings(settings, corpus):
    s = svc(settings, FakeTransport())
    r = s.process(corpus["duplicates_blank.pdf"])
    rep = s.verify(r["document_id"])
    checks = {(f.check, f.severity.value) for f in rep.findings}
    assert ("duplicates", "warning") in checks and ("completeness", "info") in checks
    assert rep.passed
    r2 = s.process(corpus["math_paper.pdf"], ProcessingOptions(review=False))
    rep2 = s.verify(r2["document_id"])
    msgs = [f.message for f in rep2.findings]
    assert any("invalid LaTeX" in m for m in msgs) and any("unresolved" in m for m in msgs)
    assert (s.store.doc_dir(r2["document_id"]) / "verification.json").exists()
    with pytest.raises(ValueError):
        s.verify(r2["document_id"], scope=["nope"])


def test_verify_with_review_reviews_only_unreviewed_flagged(settings, corpus):
    t = FakeTransport()
    s = svc(settings, t)
    r = s.process(corpus["math_paper.pdf"], ProcessingOptions(review=False))
    n0 = len(t.calls)
    rep = s.verify(r["document_id"], review=True)
    assert rep.reviewed_elements and len(t.calls) > n0
    n1 = len(t.calls)
    assert s.verify(r["document_id"], review=True).reviewed_elements == [] and len(t.calls) == n1


def test_question_numbering_gap_detected(settings, tmp_path):
    pdf = tmp_path / "gap.pdf"
    d = pymupdf.open()
    p = d.new_page()
    for i, q in enumerate((1, 2, 4)):
        p.insert_text((72, 100 + 30 * i), f"{q}. Answer the following question carefully please. [2]", fontsize=11)
    d.save(pdf)
    s = svc(settings)
    r = s.process(pdf)
    rep = s.verify(r["document_id"], scope=["structure"])
    assert any("jumps from 2 to 4" in f.message for f in rep.findings)


def test_classify_uses_text_when_available(settings, corpus):
    t = FakeTransport()
    s = svc(settings, t)
    r = s.process(corpus["clean_digital.pdf"])
    res = s.classify(r["document_id"], context_hint="Class 11 maths paper")
    assert res["document_type"] == "exam_paper" and res["evidence"] == "text"
    assert t.calls[-1]["n_images"] == 0 and "may be wrong" in t.calls[-1]["text"]
    assert s.get_document(r["document_id"]).document_type.value == "exam_paper"


def test_delete_modes_preserve_sources(settings, corpus):
    s = svc(settings)
    r = s.process(corpus["clean_digital.pdf"])
    doc_dir = s.store.doc_dir(r["document_id"])
    out = s.delete(r["document_id"], mode="generated")
    assert "pages" in out["deleted"] and (doc_dir / "source").exists() and not (doc_dir / "pages").exists()
    with pytest.raises(InputError):
        s.delete(r["document_id"], mode="all")
    s.delete(r["document_id"], mode="all", confirm=True)
    assert not doc_dir.exists() and corpus["clean_digital.pdf"].exists()  # caller's original untouched


def test_access_denied_outside_allowed(settings, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "x.pdf"
    outside.write_bytes(b"%PDF-1.4")
    with pytest.raises(AccessDeniedError):
        svc(settings).process(outside)


def test_get_content_json_and_text_pagination(settings, corpus):
    s = svc(settings)
    r = s.process(corpus["clean_digital.pdf"])
    first = s.get_content(r["document_id"], fmt="json", max_chars=100)
    assert len(first["pages"]) == 1 and first["next_cursor"] == 2
    second = s.get_content(r["document_id"], fmt="json", cursor=first["next_cursor"])
    assert second["pages"][0]["page_number"] == 2 and second["next_cursor"] is None
    txt = s.get_content(r["document_id"], fmt="text", element_types=["title"])
    assert txt["content"].strip() == "Introduction to Algebra"


def test_docx_input(settings, corpus):
    s = svc(settings)
    r = s.process(corpus["lab_record.docx"])
    doc = s.get_document(r["document_id"])
    types = [e.type.value for e in doc.iter_elements()]
    assert types[:2] == ["heading", "paragraph"] and "table" in types


def test_screenshot_is_cropped_and_boxes_land_inside_note(settings, tmp_path):
    from tests.fixtures.corpus import screenshot_note_png

    shot = screenshot_note_png(tmp_path / "meeting_screenshot.png")
    t = FakeTransport()
    s = svc(settings, t)
    r = s.process(shot, ProcessingOptions(document_type="handwritten_notes", review=False))
    page = load_page(s.store, r["document_id"], 1)
    crop = next(tr for tr in page.transforms if tr.step == "crop")
    x0, y0, x1, y1 = crop.params["box_px"]
    assert any("cropped to the detected document region" in w for w in page.warnings)
    k = page.width_pt / 2000
    for el in page.elements:
        assert el.bbox and el.bbox.x0 >= x0 * k - 1 and el.bbox.x1 <= x1 * k + 1  # mapped into the note region
    processed = Image.open(s.store.doc_dir(r["document_id"]) / page.processed_image)
    assert processed.size == (x1 - x0, y1 - y0)
    r2 = s.process(shot, ProcessingOptions(document_type="handwritten_notes", review=False, auto_crop=False))
    page2 = load_page(s.store, r2["document_id"], 1)
    assert not any(tr.step == "crop" for tr in page2.transforms)


def test_failed_page_keeps_diagnostics(settings, tmp_path):
    from tests.fixtures.corpus import screenshot_note_png

    s = svc(settings)  # no Gemini key
    r = s.process(screenshot_note_png(tmp_path / "shot.png"))
    page = load_page(s.store, r["document_id"], 1)
    assert page.status == PageStatus.FAILED and "GEMINI_API_KEY" in page.error
    assert page.route.value == "visual_general" and page.quality.light_on_dark is True  # quality measured on the dark note
    assert page.processed_image and any(tr.step == "crop" for tr in page.transforms)
