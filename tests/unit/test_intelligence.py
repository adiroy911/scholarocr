import json

import pytest
from PIL import Image

from scholarocr.config import Settings
from scholarocr.exceptions import GeminiError, GeminiUnavailableError, ResponseValidationError
from scholarocr.intelligence.extractor import VisualPageContext, box_to_bbox, convert_response, extract_page
from scholarocr.intelligence.gemini_client import GeminiClient, RateLimiter, RawResponse, TextPart, classify_exception
from scholarocr.intelligence.mathematics import chemistry_issues, latex_issues
from scholarocr.intelligence.responses import PageExtractionResponse, ReviewResponse
from scholarocr.intelligence.reviewer import review_element, select_for_review
from scholarocr.intelligence.schema_utils import to_gemini_schema
from scholarocr.intelligence.tables import table_issues
from scholarocr.intelligence.uncertainty import after_review
from scholarocr.preprocessing import geometry
from scholarocr.schemas import ElementType, QualityAssessment, Route, TableCell, TableData, Uncertainty
from tests.fixtures.fake_gemini import APIErrorLike, FakeTransport, default_page


def make_client(transport, **kw):
    sink = []
    s = Settings(gemini_api_key=None, gemini_max_retries=kw.pop("retries", 2), gemini_rpm_limit=0, **kw)
    return GeminiClient(s, transport=transport, usage_sink=lambda **f: sink.append(f), sleep=lambda _: None), sink


def test_schema_is_flat_for_gemini():
    schema = to_gemini_schema(PageExtractionResponse)
    s = json.dumps(schema)
    assert "$ref" not in s and "anyOf" not in s and '["string", "null"]' in s

    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k != "properties":
                    yield k
                yield from keys(v)
        elif isinstance(node, list):
            for v in node:
                yield from keys(v)

    assert not {"title", "default", "$defs"} & set(keys(schema))


def test_no_key_raises_unavailable():
    c = GeminiClient(Settings(gemini_api_key=None))
    assert not c.available
    with pytest.raises(GeminiUnavailableError):
        c.generate_structured(purpose="x", model="m", system="s", parts=[TextPart("t")], schema=ReviewResponse,
                              prompt_version="v")


def test_retry_on_429_then_success_and_usage_recorded():
    calls = {"n": 0}

    def responder(kind, parts, idx):
        calls["n"] += 1
        return APIErrorLike(429, "RESOURCE_EXHAUSTED") if idx == 0 else {"verdict": "confirmed"}

    client, sink = make_client(FakeTransport(responder))
    resp, info = client.generate_structured(purpose="review:x", model="m", system="You are a second-pass verifier",
                                            parts=[TextPart("t")], schema=ReviewResponse, prompt_version="v")
    assert resp.verdict == "confirmed" and info.attempts == 2
    assert sink[-1]["status"] == "ok" and sink[-1]["attempts"] == 2 and sink[-1]["total_tokens"] == 1200


@pytest.mark.parametrize("code,category", [(401, "auth_error"), (404, "model_not_found"), (400, "invalid_request")])
def test_non_retryable_errors(code, category):
    t = FakeTransport(lambda *a: APIErrorLike(code))
    client, sink = make_client(t)
    with pytest.raises(GeminiError) as ei:
        client.generate_structured(purpose="p", model="m", system="s", parts=[TextPart("t")], schema=ReviewResponse,
                                   prompt_version="v")
    assert ei.value.category == category and len(t.calls) == 1 and sink[-1]["error_category"] == category


def test_retries_exhausted_on_server_errors():
    t = FakeTransport(lambda *a: APIErrorLike(503))
    client, _ = make_client(t, retries=2)
    with pytest.raises(GeminiError) as ei:
        client.generate_structured(purpose="p", model="m", system="s", parts=[TextPart("t")], schema=ReviewResponse,
                                   prompt_version="v")
    assert ei.value.retryable and len(t.calls) == 3


def test_invalid_output_repaired_once():
    def responder(kind, parts, idx):
        return "{not json" if idx == 0 else {"verdict": "corrected", "text": "fixed"}

    t = FakeTransport(responder)
    client, sink = make_client(t)
    resp, info = client.generate_structured(purpose="p", model="m", system="s", parts=[TextPart("t")],
                                            schema=ReviewResponse, prompt_version="v")
    assert resp.text == "fixed" and info.repaired and len(t.calls) == 2
    assert "did not validate" in t.calls[1]["text"]
    assert any(r["status"] == "invalid" for r in sink)


def test_invalid_output_twice_is_rejected_never_accepted():
    t = FakeTransport(lambda *a: {"verdict": "maybe"})
    client, sink = make_client(t)
    with pytest.raises(ResponseValidationError):
        client.generate_structured(purpose="p", model="m", system="s", parts=[TextPart("t")], schema=ReviewResponse,
                                   prompt_version="v")
    assert len(t.calls) == 2 and sink[-1]["status"] == "invalid"


def test_blocked_response_is_error():
    t = FakeTransport(lambda *a: RawResponse(text=None, finish_reason="SAFETY"))
    client, _ = make_client(t)
    with pytest.raises(GeminiError) as ei:
        client.generate_structured(purpose="p", model="m", system="s", parts=[TextPart("t")], schema=ReviewResponse,
                                   prompt_version="v")
    assert ei.value.category == "blocked"


def test_classify_exception_timeouts():
    assert classify_exception(TimeoutError()) == ("timeout", True)
    assert classify_exception(ValueError()) == ("unknown", False)


def test_rate_limiter_waits_when_window_full():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    rl = RateLimiter(2, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        rl.acquire()
    assert slept and sum(slept) >= 59.9


def _ctx(matrix=geometry.IDENTITY, processed=(1000, 2000), quality=None):
    return VisualPageContext(document_id="doc_x", page_id="doc_x-p0001", page_number=1, source_file="a.png",
                             source_page_index=0, page_width_pt=720, page_height_pt=1440, original_dpi=100,
                             original_size=(1000, 2000), processed_size=processed, matrix=matrix,
                             page_image_rel="pages/0001/original.png", quality=quality)


def test_box_mapping_identity_and_through_rotation():
    ctx = _ctx()
    bb = box_to_bbox([100, 200, 300, 400], ctx)  # y 10%-30%, x 20%-40%
    assert (round(bb.x0), round(bb.y0), round(bb.x1), round(bb.y1)) == (144, 144, 288, 432)
    # Original image rotated 90 deg CCW to make processed image; mapping must return to original coordinates.
    m, size = geometry.rotation_matrix(1000, 2000, 90)
    rctx = _ctx(matrix=m, processed=size)
    # A box on the original (x 100-300 px, y 200-600 px) -> processed coords -> normalised
    px = geometry.apply_box(m, 100, 200, 300, 600)
    box = [int(px[1] / size[1] * 1000), int(px[0] / size[0] * 1000), int(px[3] / size[1] * 1000), int(px[2] / size[0] * 1000)]
    back = box_to_bbox(box, rctx)
    assert abs(back.x0 - 72) < 2 and abs(back.y0 - 144) < 2 and abs(back.x1 - 216) < 2 and abs(back.y1 - 432) < 2
    assert box_to_bbox([500, 500, 400, 600], ctx) is None and box_to_bbox(None, ctx) is None


def test_convert_response_applies_uncertainty_rules_and_links():
    resp = PageExtractionResponse.model_validate(default_page())
    els = convert_response(resp, _ctx(quality=QualityAssessment(grade="good")), model="m")
    by_type = {}
    for e in els:
        by_type.setdefault(e.type, []).append(e)
    good_eq, bad_eq = by_type[ElementType.EQUATION]
    assert good_eq.uncertainty == Uncertainty.LOW and good_eq.bbox_source == "model_estimate"
    assert bad_eq.uncertainty == Uncertainty.HIGH and any("unbalanced" in r for r in bad_eq.uncertainty_reasons)
    assert by_type[ElementType.UNCERTAIN][0].uncertainty == Uncertainty.UNRESOLVED
    cap = by_type[ElementType.CAPTION][0]
    assert cap.parent_id == by_type[ElementType.FIGURE][0].element_id
    q = els[1]
    assert q.role.value == "question" and q.question.marks == "2"
    assert all(e.source.document_id == "doc_x" and e.source.page_image for e in els)
    assert [e.reading_order for e in els] == list(range(1, len(els) + 1))


def test_poor_quality_escalates_math():
    resp = PageExtractionResponse.model_validate(default_page())
    els = convert_response(resp, _ctx(quality=QualityAssessment(grade="poor")), model="m")
    assert els[2].uncertainty == Uncertainty.MEDIUM and "poor page quality" in els[2].uncertainty_reasons


@pytest.mark.parametrize("latex,ok", [
    (r"\frac{a}{b}", True), (r"\left( x \right)", True), (r"\begin{pmatrix}1\\2\end{pmatrix}", True),
    (r"\frac{a}{b", False), (r"\left( x", False), (r"\begin{cases} x \end{matrix}", False), ("x^", False), ("", False),
])
def test_latex_issues(latex, ok):
    assert (latex_issues(latex) == []) is ok


def test_chemistry_issues():
    assert chemistry_issues(r"\ce{N2 + 3H2 <=> 2NH3}", "") == []
    assert chemistry_issues(None, "H2 + O2 H2O")  # missing arrow


def test_table_issues():
    good = TableData(rows=[[TableCell(text="a"), TableCell(text="b")], [TableCell(text="1"), TableCell(text="2")]])
    assert table_issues(good) == []
    span = TableData(rows=[[TableCell(text="h", col_span=2)], [TableCell(text="1"), TableCell(text="2")]])
    assert table_issues(span) == []
    rowspan = TableData(rows=[[TableCell(text="a", row_span=2), TableCell(text="b")], [TableCell(text="c")]])
    assert table_issues(rowspan) == []
    ragged = TableData(rows=[[TableCell(text="a"), TableCell(text="b")], [TableCell(text="1")]])
    assert any("ragged" in i for i in table_issues(ragged))


def test_after_review_rules():
    assert after_review(Uncertainty.HIGH, "confirmed", Uncertainty.LOW) == Uncertainty.MEDIUM
    assert after_review(Uncertainty.MEDIUM, "confirmed", Uncertainty.LOW) == Uncertainty.LOW
    assert after_review(Uncertainty.LOW, "corrected", Uncertainty.LOW) == Uncertainty.MEDIUM
    assert after_review(Uncertainty.MEDIUM, "unreadable", Uncertainty.LOW) == Uncertainty.UNRESOLVED
    assert after_review(Uncertainty.UNRESOLVED, "confirmed", Uncertainty.LOW) == Uncertainty.UNRESOLVED


def test_review_corrects_and_keeps_revision():
    resp = PageExtractionResponse.model_validate(default_page())
    els = convert_response(resp, _ctx(quality=QualityAssessment(grade="good")), model="m")
    selected = select_for_review(els, detail="standard")
    assert els[3] in selected and els[2] not in selected  # flagged bad equation, not the clean one
    t = FakeTransport(lambda *a: {"verdict": "corrected", "latex": r"\int_0^1 x^2 \, dx = \frac{1}{3}", "uncertainty": "low"})
    client, _ = make_client(t, gemini_review_model="review-model")
    verdict = review_element(client, client.settings, els[3], Image.new("RGB", (50, 20), "white"), document_id="doc_x")
    assert verdict == "corrected" and els[3].latex.endswith("{3}")
    assert els[3].uncertainty == Uncertainty.MEDIUM and els[3].revisions[0].previous.endswith("{3")
    assert t.calls[0]["model"] == "review-model"
    high = select_for_review(els, detail="high")
    assert els[2] in high


def test_extract_page_uses_math_model_and_hints():
    t = FakeTransport()
    client, _ = make_client(t, gemini_model="primary", gemini_math_model="mathy")
    resp, info = extract_page(client, client.settings, Route.VISUAL_MATH, Image.new("RGB", (100, 100), "white"),
                              document_id="d", page_number=1, document_type="exam_paper", language_hint="en",
                              context_hint="Class 11 maths", text_layer="x2+5x")
    assert info.model == "mathy" and t.calls[0]["n_images"] == 1
    assert "Class 11 maths" in t.calls[0]["text"] and "may be wrong" in t.calls[0]["text"]
    assert t.calls[0]["media_resolution"] == "MEDIA_RESOLUTION_HIGH"
    extract_page(client, client.settings, Route.VISUAL_GENERAL, Image.new("RGB", (10, 10)), document_id="d",
                 page_number=1, document_type=None, language_hint=None, context_hint=None, text_layer=None)
    assert t.calls[1]["model"] == "primary"
