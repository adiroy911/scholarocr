"""End-to-end: mixed scanned + digital collection -> extract once -> verify -> every export format,
with zero additional model calls after extraction (export never re-runs OCR)."""

import json
import logging
import shutil

import pymupdf

from scholarocr.schemas import ProcessingOptions
from scholarocr.service import ScholarOCR
from tests.fixtures.fake_gemini import FakeTransport


def test_extract_once_export_everything(settings, corpus):
    t = FakeTransport()
    s = ScholarOCR(settings, transport=t)
    files = [corpus["clean_digital.pdf"], corpus["math_paper.pdf"], corpus["chemistry.png"]]
    r = s.process(files, ProcessingOptions(context_hint="Class 11 mathematics and chemistry"))
    assert r["status"] == "completed" and r["page_count"] == 4
    doc_id = r["document_id"]
    calls_after_extraction = len(t.calls)
    assert calls_after_extraction > 0

    report = s.verify(doc_id)
    assert report.passed, [f.message for f in report.findings if f.severity.value == "error"]

    outputs = {}
    for fmt in ("json", "markdown", "searchable-pdf", "docx", "latex", "csv", "xlsx"):
        res = s.export(doc_id, fmt)
        outputs[fmt] = res
        assert res["files"], fmt
    assert len(t.calls) == calls_after_extraction  # no OCR/model call during export

    # JSON export round-trips the IR with full traceability.
    data = json.loads(open(outputs["json"]["primary_file"], encoding="utf-8").read())
    assert len(data["pages"]) == 4
    for page in data["pages"]:
        for el in page["elements"]:
            assert el["source"]["document_id"] == doc_id and el["source"]["page_number"] == page["page_number"]

    # Searchable PDF: same page count, original page appearance, scanned pages now searchable.
    v = outputs["searchable-pdf"]["verification"]
    assert v["page_count_ok"] and v["page_sizes_match"] and v["appearance_preserved"] and v["text_extractable"]
    with pymupdf.open(outputs["searchable-pdf"]["primary_file"]) as pdf:
        assert pdf.page_count == 4
        assert "Introduction to Algebra" in pdf[0].get_text()  # original text layer kept
        assert "Mathematics" in pdf[2].get_text()  # OCR text layer on the scanned page

    if shutil.which("xelatex"):
        assert outputs["latex"]["verification"]["compiled"] is True
    assert outputs["xlsx"]["verification"]

    # Agent-facing content retrieval reuses the same stored IR.
    content = s.get_content(doc_id, pages="3")
    assert "<!-- page 3 -->" in content["content"] and "$$" in content["content"]
    assert len(t.calls) == calls_after_extraction

    # Re-processing the same files is fully cached.
    again = s.process(files, ProcessingOptions(context_hint="Class 11 mathematics and chemistry"))
    assert s.job_status(again["job_id"])["cached_pages"] == 4
    assert len(t.calls) == calls_after_extraction


def test_retention_purges_generated_only(settings, corpus):
    s = ScholarOCR(settings)
    r = s.process(corpus["clean_digital.pdf"])
    doc_dir = s.store.doc_dir(r["document_id"])
    assert s.apply_retention(days=1)["purged"] == []
    dry = s.apply_retention(days=0, dry_run=True)
    assert dry["purged"] == [r["document_id"]] and (doc_dir / "pages").exists()
    s.apply_retention(days=0)
    assert not (doc_dir / "pages").exists() and (doc_dir / "source").exists() and (doc_dir / "manifest.json").exists()
    # Still re-processable from the preserved copy.
    assert s.process(corpus["clean_digital.pdf"])["status"] == "completed"


def test_document_text_never_logged(settings, corpus, caplog):
    secret = "ZEBRA-CONFIDENTIAL-7731"

    def responder(kind, parts, idx):
        if idx == 0:
            return {"elements": [{"type": "not-a-type", "text": secret}]}  # invalid -> repair path
        return {"elements": [{"type": "paragraph", "text": secret, "box_2d": [10, 10, 100, 900]}]}

    caplog.set_level(logging.DEBUG, logger="scholarocr")
    s = ScholarOCR(settings, transport=FakeTransport(responder))
    s.process(corpus["chemistry.png"], ProcessingOptions(review=False))
    assert secret not in caplog.text
    assert "attempting repair" in caplog.text
