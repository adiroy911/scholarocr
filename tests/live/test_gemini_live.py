"""Live Gemini API tests. NOT run by default.

Run with:  SCHOLAROCR_RUN_LIVE_TESTS=1 GEMINI_API_KEY=... pytest tests/live -m live
They make real, billable API calls with the configured GEMINI_MODEL / GEMINI_REVIEW_MODEL.
"""

import os

import pytest

from scholarocr.config import Settings
from scholarocr.schemas import ElementType, ProcessingOptions
from scholarocr.service import ScholarOCR

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not (os.environ.get("SCHOLAROCR_RUN_LIVE_TESTS") == "1"
                            and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))),
                       reason="live Gemini tests need SCHOLAROCR_RUN_LIVE_TESTS=1 and GEMINI_API_KEY"),
]


@pytest.fixture
def live(tmp_path, corpus):
    s = Settings.from_env()
    if s.provider == "host" and not os.environ.get("SCHOLAROCR_PROVIDER"):
        s = s.model_copy(update={"provider": "gemini"})  # the environment default is host; these tests are Gemini's
    s.data_dir = tmp_path / "data"
    s.allowed_dirs = [next(iter(corpus.values())).parent]
    return ScholarOCR(s)


def test_live_math_page_extraction(live, corpus):
    r = live.process(corpus["math_paper.pdf"], ProcessingOptions(context_hint="mathematics exam paper"), wait=True)
    assert r["status"] == "completed", live.document_summary(r["document_id"])
    doc = live.get_document(r["document_id"])
    eqs = [e for e in doc.iter_elements() if e.type == ElementType.EQUATION or (e.latex and "x" in e.latex)]
    text = " ".join(e.text_for_search() for e in doc.iter_elements())
    assert "Solve" in text and eqs, "expected equations from a math paper"
    assert all(e.bbox is not None for e in doc.iter_elements() if e.type != ElementType.UNCERTAIN)


def test_live_chemistry_and_classification(live, corpus):
    r = live.process(corpus["chemistry.png"], ProcessingOptions(document_type="exam_paper"), wait=True)
    assert r["status"] == "completed"
    doc = live.get_document(r["document_id"])
    assert any(e.type == ElementType.CHEMICAL_EQUATION for e in doc.iter_elements())
    cls = live.classify(r["document_id"])
    assert cls["document_type"] in ("exam_paper", "previous_year_paper", "worksheet", "question_bank", "assignment")


def test_live_usage_recorded(live, corpus):
    r = live.process(corpus["scanned_poor.pdf"], wait=True)
    usage = live.usage(r["document_id"])
    assert usage["totals"]["calls"] >= 1 and usage["totals"]["total_tokens"] > 0
