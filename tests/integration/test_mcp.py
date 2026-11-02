"""MCP server tests through the official SDK client: in-process and over a real stdio subprocess."""

import json
import os
import sys

import pytest
from mcp import Client, StdioServerParameters

from scholarocr.mcp.server import build_server
from scholarocr.service import ScholarOCR
from tests.fixtures.fake_gemini import FakeTransport

EXPECTED_TOOLS = {"ocr_document", "get_document_status", "get_document_content", "get_page_content",
                  "get_source_region", "verify_document", "export_document", "list_documents", "classify_document",
                  "delete_document", "manage_job",
                  # host mode (the client's model is the vision model)
                  "get_page_image", "submit_page_content", "submit_review"}


@pytest.fixture
def server(settings):
    return build_server(ScholarOCR(settings, transport=FakeTransport()))


async def test_tools_listed_with_schemas(server):
    async with Client(server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == EXPECTED_TOOLS
    assert "enum" in json.dumps(tools["export_document"].input_schema)
    assert tools["get_page_content"].annotations.read_only_hint is True
    assert tools["delete_document"].annotations.destructive_hint is True
    assert all(t.description for t in tools.values())


async def test_end_to_end_workflow_over_mcp(server, corpus):
    async with Client(server) as c:
        r = await c.call_tool("ocr_document", {"file_path": str(corpus["math_paper.pdf"]),
                                               "context_hint": "Class 12 maths exam"})
        assert not r.is_error
        doc_id = r.structured_content["document_id"]
        assert r.structured_content["status"] == "completed"

        listed = await c.call_tool("list_documents", {"query": "math_paper"})
        assert listed.structured_content["total"] == 1

        status = await c.call_tool("get_document_status", {"job_id": r.structured_content["job_id"]})
        assert status.structured_content["status"] == "completed"

        content = await c.call_tool("get_document_content", {"document_id": doc_id, "element_types": ["equation"]})
        md = content.structured_content["content"]
        assert "$$" in md and "<!-- el:" in md and "uncertainty=" in md

        page = await c.call_tool("get_page_content", {"document_id": doc_id, "page_number": 1})
        el = next(e for e in page.structured_content["elements"] if e["type"] == "equation")

        region = await c.call_tool("get_source_region", {"document_id": doc_id, "element_id": el["element_id"]})
        assert [b.type for b in region.content] == ["image", "text"]
        meta = json.loads(region.content[1].text)
        assert meta["element_id"] == el["element_id"] and meta["bbox_pt"]

        ver = await c.call_tool("verify_document", {"document_id": doc_id, "scope": ["math", "uncertainty"]})
        assert ver.structured_content["counts"]["warning"] >= 1

        exp = await c.call_tool("export_document", {"document_id": doc_id, "format": "markdown"})
        assert exp.structured_content["primary_file"].endswith(".md")

        cls = await c.call_tool("classify_document", {"document_id": doc_id})
        assert cls.structured_content["document_type"] == "exam_paper"

        deleted = await c.call_tool("delete_document", {"document_id": doc_id, "mode": "all"})
        assert deleted.is_error and "confirm" in deleted.content[0].text


async def test_errors_are_tool_errors(server, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4")
    async with Client(server) as c:
        denied = await c.call_tool("ocr_document", {"file_path": str(outside)})
        assert denied.is_error and "access_denied" in denied.content[0].text
        missing = await c.call_tool("get_page_content", {"document_id": "doc_0000000000000000", "page_number": 1})
        assert missing.is_error and "not_found" in missing.content[0].text
        bad = await c.call_tool("export_document", {"document_id": "x", "format": "pptx"})
        assert bad.is_error


async def test_background_job_via_mcp(server, settings, corpus):
    async with Client(server) as c:
        r = await c.call_tool("ocr_document", {"file_path": str(corpus["clean_digital.pdf"]), "wait": False})
        job_id = r.structured_content["job_id"]
        assert r.structured_content["synchronous"] is False
        import asyncio

        for _ in range(100):
            st = (await c.call_tool("get_document_status", {"job_id": job_id})).structured_content
            if st["status"] in ("completed", "failed", "partial"):
                break
            await asyncio.sleep(0.1)
        assert st["status"] == "completed" and st["progress"] == "2/2"


async def test_stdio_subprocess_server(settings, corpus):
    """Launch the real console entry point over stdio, as Antigravity CLI / Claude Code would."""
    corpus_dir = str(corpus["clean_digital.pdf"].parent)
    # No real model keys: drop them from the inherited environment, and start in the corpus dir so a
    # developer's .env in the repo root is not loaded (empty variables count as unset for .env).
    env = {k: v for k, v in os.environ.items()
           if k not in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "SCHOLAROCR_PROVIDER")}
    env.update({"SCHOLAROCR_DATA_DIR": str(settings.data_dir), "SCHOLAROCR_ALLOWED_DIRS": corpus_dir,
                "SCHOLAROCR_PROVIDER": "host"})
    params = StdioServerParameters(command=sys.executable, args=["-m", "scholarocr.mcp.server"], env=env,
                                   cwd=corpus_dir)
    async with Client(params) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        assert tools == EXPECTED_TOOLS
        r = await c.call_tool("ocr_document", {"file_path": str(corpus["clean_digital.pdf"])})
        assert r.structured_content["status"] == "completed"
        doc_id = r.structured_content["document_id"]
        content = await c.call_tool("get_document_content", {"document_id": doc_id, "format": "text"})
        assert "Introduction to Algebra" in content.structured_content["content"]
        # Host mode over the real transport: a scanned page waits for the client's model.
        scan = await c.call_tool("ocr_document", {"file_path": str(corpus["scanned_poor.pdf"])})
        assert scan.structured_content["mode"] == "host"
        assert scan.structured_content["awaiting_transcription"] == [1]
        img = await c.call_tool("get_page_image", {"document_id": scan.structured_content["document_id"]})
        assert img.content[0].type == "image" and len(img.content[0].data) > 1000
        ok = await c.call_tool("submit_page_content", {
            "document_id": scan.structured_content["document_id"], "page_number": 1,
            "elements": [{"type": "paragraph", "text": "Scanned line", "box_2d": [100, 100, 140, 900]}]})
        assert ok.structured_content["accepted"] is True
