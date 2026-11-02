"""In-process MCP round trip through the remote tools, with the model mocked (FakeTransport)."""

import json

from mcp import Client

from tests.remote.conftest import USER_B_KEY


async def test_upload_to_export_round_trip(remote, corpus):
    ns = remote()
    uid = ns.login()
    f = ns.fetcher.add("https://files.example/dl/abc", corpus["handwritten.png"].read_bytes())
    f["file_name"] = "Maths Exam (final).png"
    seen: list[str] = []
    async with Client(ns.mcp) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        assert "ocr_upload" in tools

        r = await c.call_tool("ocr_upload", {"files": [f], "context_hint": "Class 12 maths exam"})
        assert not r.is_error, r.content[0].text
        seen.append(r.content[0].text)
        doc_id = r.structured_content["document_id"]
        assert r.structured_content["status"] == "completed"
        assert r.structured_content["files"] == [{"name": "Maths Exam _final_.png", "type": "image/png",
                                                  "bytes": len(ns.fetcher.files["https://files.example/dl/abc"])}]
        assert ns.fetcher.calls == ["https://files.example/dl/abc"]

        listed = await c.call_tool("list_documents", {})
        assert listed.structured_content["total"] == 1

        status = await c.call_tool("get_document_status", {"job_id": r.structured_content["job_id"]})
        assert status.structured_content["status"] == "completed"

        content = await c.call_tool("get_document_content", {"document_id": doc_id, "element_types": ["equation"]})
        assert "$$" in content.structured_content["content"]

        page = await c.call_tool("get_page_content", {"document_id": doc_id, "page_number": 1})
        seen.append(page.content[0].text)
        el = next(e for e in page.structured_content["elements"] if e["type"] == "equation")

        region = await c.call_tool("get_source_region", {"document_id": doc_id, "element_id": el["element_id"]})
        assert [b.type for b in region.content] == ["image", "text"]
        seen.append(region.content[1].text)

        view = await c.call_tool("show_document_pages", {"document_id": doc_id})
        assert not view.is_error, view.content[0].text
        data = view.structured_content
        assert data["pages"][0]["page_number"] == 1 and data["pages"][0]["elements"]
        assert data["counts"]["unresolved"] >= 1  # FakeTransport returns an [illegible] region
        assert any(e["uncertainty"] == "unresolved" for p in data["pages"] for e in p["elements"])
        assert set(data) >= {"document_id", "range", "next_pages", "prev_pages", "counts", "page_count"}

        ver = await c.call_tool("verify_document", {"document_id": doc_id, "scope": ["math", "uncertainty"]})
        assert ver.structured_content["counts"]["warning"] >= 1

        exp = await c.call_tool("export_document", {"document_id": doc_id, "format": "markdown"})
        assert not exp.is_error, exp.content[0].text
        seen.append(exp.content[0].text)
        url = exp.structured_content["download_url"]
        assert url.startswith("http://127.0.0.1:8000/download/")
        token = url.rsplit("/", 1)[1]
        hit = ns.comps.signer.verify(token)
        assert hit is not None and hit[0] == uid and hit[1].endswith(".md")

        cls = await c.call_tool("classify_document", {"document_id": doc_id})
        assert cls.structured_content["document_type"] == "exam_paper"

        acct = await c.call_tool("account_status", {})
        assert acct.structured_content["provider"] == "gemini"
        assert acct.structured_content["billing"] == "your own key"
        seen.append(acct.content[0].text)

    blob = "\n".join(seen)
    root = str(ns.cfg.root_dir.resolve())
    assert root not in blob and root.replace("\\", "/") not in blob and root.replace("\\", "\\\\") not in blob
    assert "AIzaUSER" not in blob  # the key is never echoed
    # staged upload was removed after ingestion; the service kept its own preserved copy
    assert list(ns.pool.sandbox(uid).inbox.iterdir()) == []


async def test_users_are_isolated(remote, corpus):
    ns = remote()
    ns.login()
    f = ns.fetcher.add("https://files.example/a", corpus["handwritten.png"].read_bytes())
    async with Client(ns.mcp) as c:
        doc_id = (await c.call_tool("ocr_upload", {"files": [f]})).structured_content["document_id"]
        ns.login(USER_B_KEY)
        assert (await c.call_tool("list_documents", {})).structured_content["total"] == 0
        other = await c.call_tool("get_page_content", {"document_id": doc_id, "page_number": 1})
        assert other.is_error and "not_found" in other.content[0].text
        gone = await c.call_tool("delete_document", {"document_id": doc_id, "confirm": True})
        assert gone.is_error
        ns.login()
        assert (await c.call_tool("list_documents", {})).structured_content["total"] == 1


async def test_unauthenticated_calls_get_oauth_challenge(remote):
    ns = remote()
    async with Client(ns.mcp) as c:
        r = await c.call_tool("list_documents", {})
    assert r.is_error and json.loads(r.content[0].text)["error"] == "unauthenticated"
    assert "resource_metadata=" in r.meta["mcp/www_authenticate"][0]


async def test_base64_fallback_round_trip(remote, corpus):
    import base64

    ns = remote(max_base64_mb=1)
    ns.login()
    data = corpus["handwritten.png"].read_bytes()
    assert len(data) < 1024 * 1024
    async with Client(ns.mcp) as c:
        r = await c.call_tool("ocr_upload_base64", {"files": [{"file_name": "../../notes.png",
                                                               "data_base64": base64.b64encode(data).decode()}]})
    assert not r.is_error, r.content[0].text
    assert r.structured_content["files"][0]["name"] == "notes.png"


async def test_unexpected_errors_do_not_leak_details(remote):
    ns = remote()
    ns.login()

    class Boom:
        def fetch(self, url, *, max_bytes, timeout_s):
            raise RuntimeError(r"C:\srv\secret\internal\path")

    # the fetcher is captured at build time, so build a second server with a failing one
    from scholarocr.remote.server import build_remote_server

    mcp = build_remote_server(ns.cfg, store=ns.store, pool=ns.pool, fetcher=Boom(), identity=ns.identity).mcp
    async with Client(mcp) as c:
        r = await c.call_tool("ocr_upload", {"files": [{"download_url": "https://x.example/a", "file_id": "f"}]})
    assert r.is_error and json.loads(r.content[0].text)["error"] == "internal_error"
    assert "secret" not in r.content[0].text
