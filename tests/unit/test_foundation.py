import os
from pathlib import Path

import pytest

from scholarocr.config import Settings
from scholarocr.exceptions import (AccessDeniedError, NotFoundError, CorruptDocumentError, EncryptedDocumentError, InputError,
                                   LimitExceededError)
from scholarocr.ingestion.loader import build_manifest
from scholarocr.schemas import BBox, Element, ElementType, SourceRef, TableCell, TableData, Uncertainty
from scholarocr.security import PathPolicy
from scholarocr.storage import ArtifactStore, Repository
from scholarocr.utils import parse_page_range


def test_settings_from_env_parses_and_redacts(tmp_path):
    s = Settings.from_env({"GEMINI_API_KEY": "secret-key", "GEMINI_MODEL": "m1", "SCHOLAROCR_DATA_DIR": str(tmp_path),
                           "SCHOLAROCR_ALLOWED_DIRS": os.pathsep.join([str(tmp_path), "/x"]), "SCHOLAROCR_CONCURRENCY": "3"})
    assert s.gemini_model == "m1" and s.math_model == "m1" and s.concurrency == 3
    assert s.allowed_dirs == [tmp_path, Path("/x")]
    red = s.redacted()
    assert red["gemini_api_key"] == "set" and "secret-key" not in str(red)
    assert "secret-key" not in repr(s)


def test_settings_google_api_key_fallback():
    assert Settings.from_env({"GOOGLE_API_KEY": "k"}).gemini_api_key == "k"


@pytest.mark.parametrize("spec,total,expected", [
    (None, 3, [1, 2, 3]), ("2", 5, [2]), ("1-3,5", 6, [1, 2, 3, 5]), ("4-", 6, [4, 5, 6]), ("2-99", 4, [2, 3, 4]),
])
def test_page_range(spec, total, expected):
    assert parse_page_range(spec, total) == expected


@pytest.mark.parametrize("spec", ["0", "3-1", "abc", "9"])
def test_page_range_invalid(spec):
    with pytest.raises(InputError):
        parse_page_range(spec, 5)


def test_path_policy_blocks_outside_and_traversal(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "a.pdf").write_bytes(b"%PDF-1.4")
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"%PDF-1.4")
    pol = PathPolicy([allowed], 1)
    assert pol.check_file(allowed / "a.pdf") == (allowed / "a.pdf").resolve()
    with pytest.raises(AccessDeniedError):
        pol.check_file(allowed / ".." / "secret.pdf")
    with pytest.raises(AccessDeniedError):
        pol.check_file(secret)
    with pytest.raises(AccessDeniedError):
        pol.check_file(tmp_path / "missing-outside.pdf")  # no existence oracle outside allowed dirs
    link = allowed / "link.pdf"
    link.symlink_to(secret)
    with pytest.raises(AccessDeniedError):
        pol.check_file(link)
    with pytest.raises(InputError):
        pol.check_file(allowed / "missing.pdf")


def test_path_policy_size_limit(tmp_path):
    f = tmp_path / "big.pdf"
    f.write_bytes(b"0" * 2_000_000)
    with pytest.raises(LimitExceededError):
        PathPolicy([tmp_path], 1).check_file(f)


def test_artifact_store_rejects_bad_ids_and_escape(tmp_path):
    st = ArtifactStore(tmp_path)
    for bad in ("../x", "a/b", "..", ""):
        with pytest.raises(NotFoundError):
            st.doc_dir(bad)
    with pytest.raises(NotFoundError):
        st.resolve("doc_1", "../../etc/passwd")


def _policy(settings):
    return PathPolicy(settings.allowed_dirs, settings.max_file_mb)


def test_manifest_digital_pdf_is_stable(settings, corpus):
    st = ArtifactStore(settings.documents_dir)
    m1 = build_manifest([corpus["clean_digital.pdf"]], policy=_policy(settings), store=st, max_pages=50)
    m2 = build_manifest([corpus["clean_digital.pdf"]], policy=_policy(settings), store=st, max_pages=50)
    assert m1.document_id == m2.document_id and len(m1.pages) == 2
    assert [p.page_number for p in m1.pages] == [1, 2]
    assert all(p.profile.has_text_layer and not p.profile.is_scanned for p in m1.pages)
    stored = st.resolve(m1.document_id, m1.sources[0].stored_path)
    assert stored.read_bytes() == corpus["clean_digital.pdf"].read_bytes()  # preserved original


def test_manifest_detects_scanned_blank_duplicate(settings, corpus):
    st = ArtifactStore(settings.documents_dir)
    m = build_manifest([corpus["duplicates_blank.pdf"]], policy=_policy(settings), store=st, max_pages=50)
    assert m.pages[1].profile.is_blank and "blank_page" in m.pages[1].notes
    assert m.pages[3].duplicate_of == 1 and m.pages[2].duplicate_of is None
    scan = build_manifest([corpus["scanned_poor.pdf"]], policy=_policy(settings), store=st, max_pages=50)
    assert scan.pages[0].profile.is_scanned and not scan.pages[0].profile.has_text_layer


def test_manifest_mixed_collection_preserves_order(settings, corpus):
    st = ArtifactStore(settings.documents_dir)
    files = [corpus["chemistry.png"], corpus["clean_digital.pdf"], corpus["skewed.png"]]
    m = build_manifest(files, policy=_policy(settings), store=st, max_pages=50)
    assert [p.source_file for p in m.pages] == ["chemistry.png", "clean_digital.pdf", "clean_digital.pdf", "skewed.png"]
    assert m.pages[0].image_dpi == 150
    reversed_m = build_manifest(list(reversed(files)), policy=_policy(settings), store=st, max_pages=50)
    assert reversed_m.document_id != m.document_id


@pytest.mark.parametrize("name,exc", [("corrupted.pdf", CorruptDocumentError), ("fake.pdf", CorruptDocumentError),
                                      ("encrypted.pdf", EncryptedDocumentError)])
def test_bad_inputs_rejected(settings, corpus, name, exc):
    with pytest.raises(exc):
        build_manifest([corpus[name]], policy=_policy(settings), store=ArtifactStore(settings.documents_dir), max_pages=5)


def test_page_limit(settings, corpus):
    with pytest.raises(LimitExceededError):
        build_manifest([corpus["duplicates_blank.pdf"]], policy=_policy(settings),
                       store=ArtifactStore(settings.documents_dir), max_pages=2)


def test_repository_roundtrip(tmp_path):
    r = Repository(tmp_path / "db.sqlite")
    r.upsert_document("doc_1", title=None, filenames=["a.pdf"], document_type=None, page_count=2, status="ingested")
    r.set_page("doc_1", 1, "done", route="embedded_text", cache_key="k")
    r.create_job("job_1", "doc_1", 2, {"pages": None})
    r.update_job("job_1", status="running", completed_pages=1, errors=["x"])
    assert r.get_job("job_1").completed_pages == 1 and r.get_job("job_1").errors == ["x"]
    assert r.get_pages("doc_1")[1]["cache_key"] == "k"
    assert r.acquire_lock("doc:doc_1", "a") and not r.acquire_lock("doc:doc_1", "b")
    r.release_lock("doc:doc_1", "a")
    assert r.acquire_lock("doc:doc_1", "b")
    docs, total = r.list_documents(query="a.pdf")
    assert total == 1 and docs[0]["filenames"] == ["a.pdf"]
    r.record_call(purpose="extract", model="m", status="ok", prompt_tokens=10, total_tokens=12, document_id="doc_1")
    assert r.usage("doc_1")[0]["total_tokens"] == 12


def test_element_schema_traceability_and_table_grid():
    src = SourceRef(document_id="doc_1", page_number=3, source_file="a.pdf", source_page_index=2,
                    region_image="pages/0003/regions/e1.png")
    t = TableData(rows=[[TableCell(text="A", col_span=2, is_header=True)], [TableCell(text="1"), TableCell(text="2")]],
                  header_rows=1)
    e = Element(element_id="doc_1-p0003-e001", type=ElementType.TABLE, page_id="doc_1-p0003", page_number=3,
                reading_order=1, table=t, bbox=BBox(x0=1, y0=2, x1=11, y1=22), uncertainty=Uncertainty.MEDIUM, source=src)
    assert t.as_grid() == [["A", ""], ["1", "2"]] and t.n_cols == 2
    assert e.bbox.as_xywh() == {"x": 1, "y": 2, "width": 10, "height": 20}
    again = Element.model_validate_json(e.model_dump_json())
    assert again == e and again.source.region_image.endswith("e1.png")


def test_load_dotenv_fills_empty_but_keeps_set_values(tmp_path):
    from scholarocr.config import load_dotenv
    p = tmp_path / ".env"
    p.write_text("GEMINI_API_KEY=from-dotenv\nSCHOLAROCR_PROVIDER=openrouter\nNEW_VAR=x\n", encoding="utf-8")
    env = {"GEMINI_API_KEY": "", "SCHOLAROCR_PROVIDER": "gemini"}
    load_dotenv(p, env)
    assert env["GEMINI_API_KEY"] == "from-dotenv"  # blank passed by an MCP client counts as unset
    assert env["SCHOLAROCR_PROVIDER"] == "gemini"  # a real value still wins over .env
    assert env["NEW_VAR"] == "x"
