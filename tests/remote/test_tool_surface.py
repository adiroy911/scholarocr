"""The remote tool surface: no path-taking tools, ChatGPT file params, annotations, UI resource."""

import json

from mcp import Client

from scholarocr.mcp.server import build_server
from scholarocr.remote.server import FORBIDDEN_PARAMS, PAGE_IMAGE_URI, WIDGET_MIME, WIDGET_URI
from scholarocr.service import ScholarOCR
from tests.fixtures.fake_gemini import FakeTransport

EXPECTED_REMOTE = {"ocr_upload", "ocr_upload_base64", "get_document_status", "get_document_content",
                   "get_page_content", "get_source_region", "show_document_pages", "verify_document",
                   "export_document", "list_documents", "classify_document", "delete_document", "manage_job",
                   "account_status", "delete_my_data",
                   # host mode and the optional key
                   "get_page_image", "submit_page_content", "submit_review", "manage_model_key"}


def _property_names(schema) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        for k, v in schema.items():
            if k == "properties" and isinstance(v, dict):
                names |= set(v)
            names |= _property_names(v)
    elif isinstance(schema, list):
        for v in schema:
            names |= _property_names(v)
    return names


async def test_no_path_tools_exposed(remote):
    ns = remote()
    async with Client(ns.mcp) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == EXPECTED_REMOTE
    assert "ocr_document" not in tools
    for t in tools.values():
        bad = _property_names(t.input_schema) & FORBIDDEN_PARAMS
        assert not bad, f"{t.name} accepts path-like parameters {bad}"
        assert "path" not in json.dumps(t.input_schema).lower().replace("download_url", "")


async def test_local_server_is_unchanged(settings):
    async with Client(build_server(ScholarOCR(settings, transport=FakeTransport()))) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert "ocr_document" in names and "ocr_upload" not in names


async def test_file_params_follow_apps_sdk_shape(remote):
    ns = remote()
    async with Client(ns.mcp) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    up = tools["ocr_upload"]
    assert up.meta["openai/fileParams"] == ["files"]
    files = up.input_schema["properties"]["files"]
    assert files["type"] == "array"
    ref = files["items"]["$ref"].rsplit("/", 1)[-1]
    item = up.input_schema["$defs"][ref]
    assert set(item["properties"]) == {"download_url", "file_id", "mime_type", "file_name"}
    assert set(item["required"]) == {"download_url", "file_id"}
    assert all(item["properties"][k]["type"] == "string" for k in item["properties"])
    assert "files" in up.input_schema["required"]
    # the base64 fallback is NOT declared as a file param
    assert "openai/fileParams" not in (tools["ocr_upload_base64"].meta or {})


async def test_annotations_explicit_and_security_schemes(remote):
    ns = remote()
    async with Client(ns.mcp) as c:
        tools = (await c.list_tools()).tools
    for t in tools:
        a = t.annotations
        assert a is not None, t.name
        assert isinstance(a.read_only_hint, bool) and isinstance(a.destructive_hint, bool)
        assert isinstance(a.open_world_hint, bool), t.name
        assert t.meta["securitySchemes"] == [{"type": "oauth2", "scopes": ["scholarocr"]}]
        assert t.description
    by = {t.name: t for t in tools}
    assert by["delete_my_data"].annotations.destructive_hint is True
    assert by["delete_document"].annotations.destructive_hint is True
    assert by["show_document_pages"].annotations.read_only_hint is True


async def test_base64_tool_can_be_disabled(remote):
    ns = remote(allow_base64=False)
    async with Client(ns.mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert "ocr_upload_base64" not in names and "ocr_upload" in names


async def test_widget_resource_and_template_metadata(remote):
    ns = remote(widget_domain="https://ocr.example.com")
    async with Client(ns.mcp) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        res = {str(r.uri): r for r in (await c.list_resources()).resources}
        read = await c.read_resource(WIDGET_URI)
    show = tools["show_document_pages"]
    assert show.meta["ui"]["resourceUri"] == WIDGET_URI
    assert show.meta["openai/outputTemplate"] == WIDGET_URI
    # only the two render tools point at templates
    assert tools["get_page_image"].meta["openai/outputTemplate"] == PAGE_IMAGE_URI
    assert all("ui" not in (t.meta or {}) for n, t in tools.items() if n not in ("show_document_pages",
                                                                                   "get_page_image"))
    assert PAGE_IMAGE_URI in res and res[PAGE_IMAGE_URI].mime_type == WIDGET_MIME
    assert res[WIDGET_URI].mime_type == WIDGET_MIME == "text/html;profile=mcp-app"
    content = read.contents[0]
    assert content.mime_type == WIDGET_MIME
    assert content.meta["ui"]["domain"] == "https://ocr.example.com"
    assert content.meta["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}
    html = content.text
    assert "ui/initialize" in html and "ui/notifications/tool-result" in html
    assert "<script src" not in html and "http://" not in html.replace("http://www.w3.org", "")
    assert "innerHTML" not in html  # all data is rendered with textContent


async def test_page_image_widget_makes_the_image_model_visible(remote):
    ns = remote()
    async with Client(ns.mcp) as c:
        read = await c.read_resource(PAGE_IMAGE_URI)
    html = read.contents[0].text
    # ChatGPT: upload the image and list it in widget-state imageIds; MCP Apps: ui/update-model-context.
    for needle in ("uploadFile", "setWidgetState", "imageIds", "ui/update-model-context", "scholarocr/pageImage",
                   "toolResponseMetadata", "sendFollowUpMessage"):
        assert needle in html, needle
    assert "<script src" not in html and "innerHTML" not in html


async def test_noauth_mode_declares_noauth(remote):
    ns = remote(auth="none")
    async with Client(ns.mcp) as c:
        t = (await c.list_tools()).tools[0]
    assert t.meta["securitySchemes"] == [{"type": "noauth"}]
