"""Full HTTP path, in-process (ASGI, no sockets): OAuth 2.1 + PKCE with BYO key, then MCP over streamable HTTP."""

import base64
import contextlib
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from scholarocr.remote.config import RemoteConfig
from scholarocr.remote.server import create_app
from tests.remote.conftest import OPERATOR_ENV, USER_KEY, FakeFetcher, Transports

BASE = "http://127.0.0.1:8000"
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def _authorize(http, verifier_challenge, api_key=USER_KEY):
    verifier, challenge = verifier_challenge
    reg = await http.post("/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
                                             "grant_types": ["authorization_code", "refresh_token"],
                                             "response_types": ["code"], "client_name": "ChatGPT"})
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    auth = await http.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "st4te", "scope": "scholarocr", "resource": f"{BASE}/mcp"})
    assert auth.status_code == 302, auth.text
    connect_url = auth.headers["location"]
    assert connect_url.startswith(f"{BASE}/byok/connect?req=")
    page = await http.get(connect_url)
    assert page.status_code == 200 and 'type="password"' in page.text
    assert page.headers["x-frame-options"] == "DENY"
    req = parse_qs(urlparse(connect_url).query)["req"][0]

    bad = await http.post("/byok/connect", data={"req": req, "provider": "openrouter", "api_key": USER_KEY,
                                                 "consent": "1"})
    assert bad.status_code == 400 and "sk-or-" in bad.text
    no_consent = await http.post("/byok/connect", data={"req": req, "provider": "gemini", "api_key": api_key})
    assert no_consent.status_code == 400
    ok = await http.post("/byok/connect", data={"req": req, "provider": "gemini", "api_key": api_key, "consent": "1"})
    assert ok.status_code == 302, ok.text
    back = urlparse(ok.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT
    q = parse_qs(back.query)
    assert q["state"] == ["st4te"]
    # the request id is single use
    assert (await http.post("/byok/connect", data={"req": req, "provider": "gemini", "api_key": api_key,
                                                   "consent": "1"})).status_code == 400

    wrong = await http.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                            "redirect_uri": REDIRECT, "client_id": client_id,
                                            "code_verifier": "x" * 50, "resource": f"{BASE}/mcp"})
    assert wrong.status_code == 400  # PKCE mismatch
    tok = await http.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0],
                                          "redirect_uri": REDIRECT, "client_id": client_id,
                                          "code_verifier": verifier, "resource": f"{BASE}/mcp"})
    assert tok.status_code == 200, tok.text
    return client_id, tok.json()


async def test_oauth_byok_flow_and_mcp_over_http(tmp_path, corpus):
    cfg = RemoteConfig(root_dir=tmp_path / "remote", secret="s" * 48, auth="oauth", public_url=BASE,
                       calls_per_minute=0, ocr_calls_per_hour=0, pages_per_day=0)
    fetcher, transports = FakeFetcher(), Transports()
    app = create_app(cfg, transport_factory=transports, fetcher=fetcher, environ=dict(OPERATOR_ENV))
    store = app.state.components.store
    transport = httpx2.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=transport, base_url=BASE) as http:
            # discovery: 401 + WWW-Authenticate, protected resource metadata, AS metadata
            r = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                headers={"Accept": "application/json, text/event-stream"})
            assert r.status_code == 401
            assert "resource_metadata=" in r.headers["www-authenticate"]
            prm = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
            assert prm["resource"].rstrip("/") == f"{BASE}/mcp" and prm["authorization_servers"]
            root_prm = (await http.get("/.well-known/oauth-protected-resource")).json()
            assert root_prm["resource"] == f"{BASE}/mcp"
            asm = (await http.get("/.well-known/oauth-authorization-server")).json()
            assert "S256" in asm["code_challenge_methods_supported"] and asm["registration_endpoint"]
            assert (await http.get("/healthz")).json()["status"] == "ok"

            client_id, tokens = await _authorize(http, _pkce())
            assert tokens["token_type"].lower() == "bearer" and tokens["refresh_token"]
            uid = store.get_token(tokens["access_token"], "access")["user_id"]
            assert uid != store.user_id_for_key("gemini", USER_KEY)  # the account id does not come from the key
            assert store.get_key(uid).api_key == USER_KEY

            url = fetcher.add("https://files.example/x", corpus["handwritten.png"].read_bytes())
            authed = httpx2.AsyncClient(transport=transport, base_url=BASE,
                                        headers={"Authorization": f"Bearer {tokens['access_token']}"})
            async with authed, Client(streamable_http_client(f"{BASE}/mcp", http_client=authed)) as c:
                names = {t.name for t in (await c.list_tools()).tools}
                assert "ocr_upload" in names and "ocr_document" not in names
                up = await c.call_tool("ocr_upload", {"files": [url]})
                assert not up.is_error, up.content[0].text
                doc_id = up.structured_content["document_id"]
                exp = await c.call_tool("export_document", {"document_id": doc_id, "format": "markdown"})
                link = exp.structured_content["download_url"]
                widget = await c.read_resource("ui://scholarocr/page-viewer-v1.html")
                assert widget.contents[0].mime_type == "text/html;profile=mcp-app"
            assert transports.built == [(uid, "gemini", USER_KEY)]

            dl = await http.get(link.replace(BASE, ""))
            assert dl.status_code == 200 and b"$$" in dl.content
            token = link.rsplit("/", 1)[1]
            body, mac = token.split(".")
            forged_body = re.sub(r"^.", lambda m: "A" if m.group(0) != "A" else "B", body)
            for forged in (f"{forged_body}.{mac}", f"{body}.{mac[:-1]}{'A' if mac[-1] != 'A' else 'B'}", "junk"):
                assert (await http.get(f"/download/{forged}")).status_code == 404

            # refresh rotation: the old refresh token stops working
            new = await http.post("/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                  "refresh_token": tokens["refresh_token"]})
            assert new.status_code == 200, new.text
            again = await http.post("/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                    "refresh_token": tokens["refresh_token"]})
            assert again.status_code == 400
            old_access = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                         headers={"Authorization": f"Bearer {tokens['access_token']}",
                                                  "Accept": "application/json, text/event-stream"})
            assert old_access.status_code == 401

            # deleting the user's data invalidates the remaining token
            fresh = new.json()["access_token"]
            statuses: list[int] = []

            async def record(resp):
                statuses.append(resp.status_code)

            h2 = httpx2.AsyncClient(transport=transport, base_url=BASE, headers={"Authorization": f"Bearer {fresh}"},
                                    event_hooks={"response": [record]})
            # The tool call itself returns 200; the SDK client's follow-up request in the same session then gets
            # 401 because the token was revoked by that very call, which the client surfaces as an MCPError.
            with contextlib.suppress(Exception):
                async with h2, Client(streamable_http_client(f"{BASE}/mcp", http_client=h2)) as c:
                    await c.call_tool("delete_my_data", {"confirm": True})
            assert statuses[:2] == [200, 200]
            gone = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                   headers={"Authorization": f"Bearer {fresh}",
                                            "Accept": "application/json, text/event-stream"})
            assert gone.status_code == 401
            assert store.get_key(uid) is None


async def test_noauth_mode_rejects_unknown_hosts(tmp_path):
    cfg = RemoteConfig(root_dir=tmp_path / "r", secret="s" * 48, auth="none", public_url="https://ocr.example.com")
    app = create_app(cfg, environ={})
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="https://evil.example") as h:
            r = await h.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                             headers={"Accept": "application/json, text/event-stream"})
            assert r.status_code == 421  # DNS-rebinding protection: Host must be the public host
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="https://ocr.example.com") as h:
            assert (await h.get("/byok/connect")).status_code == 404  # no OAuth pages without OAuth


async def test_consent_without_key_then_link_a_key_later(tmp_path, corpus):
    cfg = RemoteConfig(root_dir=tmp_path / "remote", secret="s" * 48, auth="oauth", public_url=BASE,
                       calls_per_minute=0, ocr_calls_per_hour=0, pages_per_day=0)
    fetcher, transports = FakeFetcher(), Transports()
    app = create_app(cfg, transport_factory=transports, fetcher=fetcher, environ=dict(OPERATOR_ENV))
    store = app.state.components.store
    transport = httpx2.ASGITransport(app=app)
    verifier, challenge = _pkce()
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=transport, base_url=BASE) as http:
            reg = await http.post("/register", json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
                                                     "grant_types": ["authorization_code", "refresh_token"],
                                                     "response_types": ["code"], "client_name": "ChatGPT"})
            client_id = reg.json()["client_id"]
            auth = await http.get("/authorize", params={
                "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "code_challenge": challenge,
                "code_challenge_method": "S256", "state": "s", "scope": "scholarocr", "resource": f"{BASE}/mcp"})
            connect_url = auth.headers["location"]
            page = await http.get(connect_url)
            assert "Continue without a key" in page.text and 'value="host" checked' in page.text
            assert "Use this server" not in page.text  # operator option only in operator-key mode
            req = parse_qs(urlparse(connect_url).query)["req"][0]
            ok = await http.post("/byok/connect", data={"req": req, "mode": "host", "consent": "1"})
            assert ok.status_code == 302, ok.text
            code = parse_qs(urlparse(ok.headers["location"]).query)["code"][0]
            tok = (await http.post("/token", data={"grant_type": "authorization_code", "code": code,
                                                   "redirect_uri": REDIRECT, "client_id": client_id,
                                                   "code_verifier": verifier, "resource": f"{BASE}/mcp"})).json()
            uid = store.get_token(tok["access_token"], "access")["user_id"]
            assert store.get_key(uid).provider == "host" and store.get_key(uid).api_key is None

            url = fetcher.add("https://files.example/h", corpus["handwritten.png"].read_bytes())
            authed = httpx2.AsyncClient(transport=transport, base_url=BASE,
                                        headers={"Authorization": f"Bearer {tok['access_token']}"})
            async with authed, Client(streamable_http_client(f"{BASE}/mcp", http_client=authed)) as c:
                up = await c.call_tool("ocr_upload", {"files": [url]})
                assert not up.is_error and up.structured_content["mode"] == "host"
                img = await c.call_tool("get_page_image", {"document_id": up.structured_content["document_id"]})
                assert img.content[0].type == "image"
                link = (await c.call_tool("manage_model_key", {"action": "link"})).structured_content["url"]
            assert transports.built == []  # host mode: no model client, operator key untouched

            path = link.replace(BASE, "")
            form = await http.get(path)
            assert form.status_code == 200 and 'type="password"' in form.text
            assert form.headers["x-frame-options"] == "DENY"
            token = parse_qs(urlparse(link).query)["t"][0]
            bad = await http.post("/byok/key", data={"t": token, "provider": "gemini", "api_key": "short",
                                                     "consent": "1"})
            assert bad.status_code == 400 and store.get_key(uid).api_key is None
            saved = await http.post("/byok/key", data={"t": token, "provider": "gemini", "api_key": USER_KEY,
                                                       "consent": "1"})
            assert saved.status_code == 200 and "Key saved" in saved.text
            assert store.get_key(uid).api_key == USER_KEY  # same account id, key linked
            reuse = await http.post("/byok/key", data={"t": token, "provider": "gemini", "api_key": USER_KEY,
                                                       "consent": "1"})
            assert reuse.status_code == 400  # one-time link
            assert (await http.get("/byok/key?t=forged")).status_code == 400
