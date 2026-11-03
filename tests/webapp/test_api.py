"""HTTP API: security checks, uploads and limits, preferences, conversations, the Antigravity read loop."""

from __future__ import annotations

import re
from pathlib import Path

from scholarocr.webapp.agy import AgyCancelled, AgyTimeout
from scholarocr.webapp.server import CSP, static_dir
from tests.webapp.conftest import ORIGIN, broken_payload, events, make_client, page_payload, upload

STATIC = static_dir()


# ---------------------------------------------------------------------- security
def test_index_embeds_token_and_sets_csp(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'content="test-token"' in r.text
    assert r.headers["content-security-policy"] == CSP
    assert "script-src 'self'" in CSP and "unsafe-eval" not in CSP and "'unsafe-inline'" not in CSP.split("style-src")[0]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"


def test_api_requires_token(client):
    assert client.get("/api/state", headers={"X-ScholarOCR-Token": "wrong"}).status_code == 403
    no_token = client.get("/api/state", headers={"X-ScholarOCR-Token": ""})
    assert no_token.status_code == 403
    assert client.get("/api/state?t=test-token", headers={"X-ScholarOCR-Token": ""}).status_code == 200


def test_query_token_only_for_get(client):
    r = client.post("/api/conversations?t=test-token", headers={"X-ScholarOCR-Token": ""})
    assert r.status_code == 403


def test_foreign_origin_and_cross_site_refused(client):
    assert client.post("/api/conversations", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/conversations", headers={"Origin": "http://127.0.0.1:9999"}).status_code == 403
    assert client.get("/api/state", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/conversations", headers={"Origin": ORIGIN}).status_code == 201
    assert client.post("/api/conversations", headers={"Origin": "http://localhost:8765"}).status_code == 201


def test_host_header_must_be_loopback(client):
    # DNS rebinding: a page on attacker.example resolving to 127.0.0.1 sends Host: attacker.example
    assert client.get("/", headers={"Host": "attacker.example:8765"}).status_code == 421
    assert client.get("/api/state", headers={"Host": "attacker.example"}).status_code == 421
    assert client.get("/", headers={"Host": "localhost:8765"}).status_code == 200


def test_json_endpoints_need_json(client):
    r = client.put("/api/prefs", content=b"saved_instructions=x", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400 and "json" in r.json()["message"]


# ---------------------------------------------------------------------- output escaping
def test_frontend_never_uses_html_injection_sinks():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in js, sink
    # KaTeX renders untrusted LaTeX with trust off (no \href, \htmlClass, ...).
    assert re.search(r"katex\.render\([^)]*trust: false", js)
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and "onclick=" not in html  # no inline script under the CSP


def test_model_text_is_returned_as_data_not_html(client, corpus):
    hostile = "<img src=x onerror=alert(1)> Q1. Solve $x$ [2] <script>alert(2)</script>"
    client.fake_agy.replies = [page_payload(hostile)]
    up = upload(client, corpus["math_paper.pdf"]).json()
    cid = client.post("/api/conversations").json()["id"]
    run = client.post(f"/api/conversations/{cid}/messages", json={"text": "", "uploads": [up["id"]]}).json()
    done = events(client, run["run_id"])[-1]
    doc_id = done["data"]["documents"][0]["document_id"]
    r = client.get(f"/api/documents/{doc_id}")
    assert r.headers["content-type"].startswith("application/json")
    texts = [e["text"] for p in r.json()["pages"] for e in p["elements"]]
    assert hostile in texts  # stored verbatim; the page puts it in a text node


# ---------------------------------------------------------------------- uploads & limits
def test_upload_type_sniffing_and_safe_names(client, corpus, png_bytes):
    r = client.post("/api/uploads", params={"name": "../../evil/page.pdf"}, content=png_bytes,
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 201
    meta = r.json()
    assert meta["type"] == "image/png" and meta["kind"] == "image"
    assert "/" not in meta["name"] and ".." not in meta["name"] and meta["name"].endswith(".png")
    stored = list((client.app.cfg.uploads_dir / meta["id"]).iterdir())
    assert all(p.resolve().is_relative_to(client.app.cfg.uploads_dir.resolve()) for p in stored)
    bad = client.post("/api/uploads", params={"name": "notes.pdf"}, content=b"hello, not a pdf at all",
                      headers={"Content-Type": "application/octet-stream"})
    assert bad.status_code == 400
    assert client.post("/api/uploads", params={"name": "e.pdf"}, content=b"").status_code == 400
    assert upload(client, corpus["lab_record.docx"]).json()["kind"] == "docx"


def test_upload_size_limit_from_settings(client, png_bytes):
    client.put("/api/prefs", json={"max_file_mb": 1})
    big = png_bytes + b"\0" * (1024 * 1024 + 10)
    r = client.post("/api/uploads", params={"name": "big.png"}, content=big, headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 413 and "1 MB" in r.json()["message"]


def test_files_per_message_limit(client, png_bytes):
    client.put("/api/prefs", json={"max_files": 2})
    ids = [client.post("/api/uploads", params={"name": f"p{i}.png"}, content=png_bytes,
                       headers={"Content-Type": "application/octet-stream"}).json()["id"] for i in range(3)]
    cid = client.post("/api/conversations").json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", json={"uploads": ids})
    assert r.status_code == 413 and "at most 2 files" in r.json()["message"]


def test_upload_ids_are_validated(client):
    cid = client.post("/api/conversations").json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", json={"uploads": ["../../etc/passwd"]})
    assert r.status_code == 400
    r = client.post(f"/api/conversations/{cid}/messages", json={"uploads": ["u" + "0" * 20]})
    assert r.status_code == 404


def test_question_without_document_is_refused(client):
    cid = client.post("/api/conversations").json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", json={"text": "what is Q3?"})
    assert r.status_code == 400 and "attach a document first" in r.json()["message"]


# ---------------------------------------------------------------------- preferences
def test_saved_instructions_persist_across_restarts(cfg, keys):
    c1, _, _ = make_client(cfg, keys)
    r = c1.put("/api/prefs", json={"saved_instructions": "Class 11 chemistry; keep question numbers; Hindi + English",
                                   "agy_model": "gemini-3.8-flash-low", "theme": "dark"})
    assert r.status_code == 200
    c2, _, _ = make_client(cfg, keys)
    prefs = c2.get("/api/state").json()["prefs"]
    assert prefs["saved_instructions"].startswith("Class 11 chemistry") and prefs["theme"] == "dark"
    assert prefs["agy_model"] == "gemini-3.8-flash-low"


def test_prefs_validation(client):
    assert client.put("/api/prefs", json={"saved_instructions": "x" * 5000}).status_code == 400
    assert client.put("/api/prefs", json={"engine": "chatgpt"}).status_code == 400
    assert client.put("/api/prefs", json={"max_file_mb": 0}).status_code == 400
    assert client.put("/api/prefs", json={"agy_model": "a b; rm -rf"}).status_code == 400
    assert client.put("/api/prefs", json={"unknown": 1}).status_code == 400


def test_setup_needed_without_any_engine(cfg, keys):
    c, _, _ = make_client(cfg, keys, has_agy=False)
    st = c.get("/api/state").json()
    assert st["setup_needed"] is True
    assert "not on PATH" in next(e for e in st["engines"] if e["id"] == "antigravity")["reason"]
    up = c.post("/api/uploads", params={"name": "a.pdf"}, content=b"%PDF-1.4\n%%EOF\n",
                headers={"Content-Type": "application/octet-stream"}).json()
    cid = c.post("/api/conversations").json()["id"]
    r = c.post(f"/api/conversations/{cid}/messages", json={"uploads": [up["id"]]})
    assert r.status_code == 409 and r.json()["error"] == "no_engine"


# ---------------------------------------------------------------------- the Antigravity read loop
def read(client, path: Path, text: str = "", **extra):
    up = upload(client, path).json()
    cid = client.post("/api/conversations").json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", json={"text": text, "uploads": [up["id"]], **extra})
    assert r.status_code == 202, r.text
    return cid, r.json(), events(client, r.json()["run_id"])


def test_read_with_antigravity_end_to_end(client, corpus):
    client.put("/api/prefs", json={"saved_instructions": "Class 11 maths; keep question numbers"})
    cid, posted, evs = read(client, corpus["math_paper.pdf"], "Hindi + English", model="gemini-3.8-flash-low")
    types = [e["type"] for e in evs]
    assert types[0] == "stage" and types[-1] == "done"
    assert {"document_started", "job", "page", "usage", "verification", "document"} <= set(types)
    done = evs[-1]
    assert done["state"] == "done"
    call = client.fake_agy.calls[0]
    assert call["model"] == "gemini-3.8-flash-low"
    assert call["schema"]["properties"]["elements"]["type"] == "array"  # PageExtractionResponse schema
    img_dir = call["read_dirs"][0]
    assert str(img_dir).startswith(str(client.app.cfg.runs_dir))
    assert "read_file" in call["prompt"] and "denied" in call["prompt"]
    # Saved and per-message instructions both reach the model as context through the host brief.
    assert "Class 11 maths; keep question numbers" in call["prompt"] and "Hindi + English" in call["prompt"]
    assert not img_dir.exists()  # the per-page run folder is removed afterwards
    page_ev = [e for e in evs if e["type"] == "page" and e["status"] == "done"][0]
    assert page_ev["elements"] > 3
    data = client.get(f"/api/conversations/{cid}").json()
    asst = data["messages"][-1]
    assert asst["state"] == "done" and asst["data"]["usage"]["total_tokens"] == 22300
    stats = next(iter(asst["data"]["pages"].values()))
    assert stats["attempts"] == 1 and stats["usage"]["input_tokens"] == 20000
    doc_id = asst["data"]["documents"][0]["document_id"]
    view = client.get(f"/api/documents/{doc_id}").json()
    assert view["pages"][0]["model"] == "host:antigravity:gemini-3.8-flash-low"
    q = next(e for e in view["pages"][0]["elements"] if e.get("question"))
    assert q["question"] == {"number": "1", "marks": "2"}
    # source crop and page image
    el = view["pages"][0]["elements"][0]
    png = client.get(f"/api/documents/{doc_id}/elements/{el['id']}/source.png")
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get(f"/api/documents/{doc_id}/pages/1/source.png").status_code == 200
    # exports
    md = client.get(f"/api/documents/{doc_id}/export/markdown")
    assert md.status_code == 200 and "Solve" in md.text
    assert 'filename="math_paper.md"' in md.headers["content-disposition"]
    assert client.get(f"/api/documents/{doc_id}/export/docx").status_code == 200
    assert client.get(f"/api/documents/{doc_id}/export/exe").status_code == 400
    # history
    convs = client.get("/api/conversations").json()["conversations"]
    assert convs[0]["id"] == cid and convs[0]["title"] == "math_paper" and convs[0]["documents"] == 1


def test_documents_of_other_apps_are_not_served(client):
    assert client.get("/api/documents/doc_0123456789abcdef").status_code == 404
    assert client.get("/api/documents/doc_0123456789abcdef/export/markdown").status_code == 404


def test_rejected_submission_is_retried_with_the_errors(client, corpus):
    client.fake_agy.replies = [broken_payload(), page_payload()]
    _, _, evs = read(client, corpus["math_paper.pdf"])
    calls = client.fake_agy.calls
    assert len(calls) == 2
    retry = calls[1]
    assert retry["conversation_id"] == "conv-1"  # continues the agy conversation that already has the image
    assert "REJECTED" in retry["prompt"] and "elements[3].latex" in retry["prompt"] and "LaTeX syntax" in retry["prompt"]
    statuses = [e["status"] for e in evs if e["type"] == "page"]
    assert statuses == ["reading", "retrying", "done"]
    assert evs[-1]["state"] == "done"


def test_rejections_stop_after_two_retries(client, corpus):
    client.fake_agy.replies = [broken_payload(), "not json at all", broken_payload()]
    cid, _, evs = read(client, corpus["math_paper.pdf"])
    assert len(client.fake_agy.calls) == 3  # first try + 2 retries
    last_page = [e for e in evs if e["type"] == "page"][-1]
    assert last_page["status"] == "rejected" and last_page["errors"]
    done = evs[-1]
    assert done["state"] == "partial" and done["data"]["documents"][0]["unread_pages"] == [1]
    msgs = client.get(f"/api/conversations/{cid}").json()["messages"]
    assert msgs[-1]["state"] == "partial"


def test_page_timeout_is_reported_and_the_run_continues(client, corpus):
    client.fake_agy.replies = [AgyTimeout("agy did not finish within 5 s")]
    _, _, evs = read(client, corpus["math_paper.pdf"])
    page = [e for e in evs if e["type"] == "page"][-1]
    assert page["status"] == "timeout"
    assert evs[-1]["state"] == "partial"


def test_cancel_stops_the_run(client, corpus):
    client.fake_agy.replies = [AgyCancelled("cancelled")]
    _, _, evs = read(client, corpus["math_paper.pdf"])
    assert evs[-1]["state"] == "cancelled"
    doc = evs[-1]["data"]["documents"][0]  # the stopped document is still listed, with its unread page
    assert doc["state"] == "cancelled" and doc["unread_pages"] == [1]


def test_cancel_endpoint_sets_the_flag(cfg, keys, corpus):
    import threading

    gate = threading.Event()

    class SlowAgy:
        calls = []

        def run(self, prompt, **kw):
            self.calls.append(kw)
            gate.set()
            kw["cancel"].wait(5)
            raise AgyCancelled("cancelled")

    slow = SlowAgy()
    from scholarocr.webapp.server import create_app
    from starlette.testclient import TestClient
    from tests.webapp.conftest import FakeRegistry

    asgi = create_app(cfg, keys=keys, registry=FakeRegistry(cfg, keys), agy_factory=lambda: slow, token="t",
                      threaded_runs=True)
    c = TestClient(asgi, base_url=ORIGIN)
    c.headers.update({"X-ScholarOCR-Token": "t", "Origin": ORIGIN})
    up = upload(c, corpus["math_paper.pdf"]).json()
    cid = c.post("/api/conversations").json()["id"]
    run_id = c.post(f"/api/conversations/{cid}/messages", json={"uploads": [up["id"]]}).json()["run_id"]
    assert gate.wait(30)
    second = c.post(f"/api/conversations/{cid}/messages", json={"text": "q"})
    assert second.status_code == 400 and "still working" in second.json()["message"]
    assert c.post(f"/api/runs/{run_id}/cancel").json() == {"cancelling": True}
    evs = events(c, run_id)
    assert evs[-1]["state"] == "cancelled"


def test_chat_through_antigravity_is_grounded(client, corpus):
    cid, _, _ = read(client, corpus["math_paper.pdf"])
    r = client.post(f"/api/conversations/{cid}/messages", json={"text": "What's Q1 about?"})
    assert r.status_code == 202
    evs = events(client, r.json()["run_id"])
    assert [e for e in evs if e["type"] == "delta"]
    chat = client.fake_agy.calls[-1]
    assert chat["schema"] is None and chat["read_dirs"] == []  # no tools for chat
    assert "=== math_paper.pdf | page 1 ===" in chat["prompt"] and "Question: What's Q1 about?" in chat["prompt"]
    assert "[p. N]" in chat["prompt"]
    msgs = client.get(f"/api/conversations/{cid}").json()["messages"]
    assert msgs[-1]["text"] == client.fake_agy.chat_text and msgs[-1]["data"]["usage"]["total_tokens"] == 940


def test_photos_become_one_document(client, corpus):
    ids = [upload(client, corpus[n]).json()["id"] for n in ("chemistry.png", "skewed.png")]
    cid = client.post("/api/conversations").json()["id"]
    run = client.post(f"/api/conversations/{cid}/messages", json={"uploads": ids}).json()
    done = events(client, run["run_id"])[-1]
    docs = done["data"]["documents"]
    assert len(docs) == 1 and docs[0]["page_count"] == 2 and docs[0]["files"] == ["chemistry.png", "skewed.png"]


def test_conversation_rename_and_delete(client):
    cid = client.post("/api/conversations").json()["id"]
    assert client.patch(f"/api/conversations/{cid}", json={"title": "  Chem   unit 3 "}).json()["title"] == "Chem unit 3"
    assert client.delete(f"/api/conversations/{cid}").status_code == 200
    assert client.get(f"/api/conversations/{cid}").status_code == 404
