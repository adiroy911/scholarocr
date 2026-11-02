"""OAuth 2.1 authorization server whose "login" step is linking the user's own model API key.

ChatGPT apps authenticate users only through OAuth 2.1 (authorization code + PKCE S256, with
dynamic client registration or CIMD); they cannot send custom API keys or headers, and secrets must
not be collected through tool inputs or conversation. So the BYO key is collected on this server's
own consent page during the OAuth authorization step:

    ChatGPT -> /authorize (SDK handler, validates client/redirect/PKCE) -> provider.authorize()
            -> 302 /byok/connect?req=...   (this module: HTML form, user pastes Gemini/OpenRouter key)
            -> key encrypted into AuthStore, authorization code issued
            -> 302 back to ChatGPT's redirect_uri?code=...&state=...
    ChatGPT -> /token (SDK handler verifies PKCE) -> exchange_authorization_code() -> opaque tokens

The access token's ``subject`` is the ScholarOCR user id (derived from the key), which selects the
sandbox and the key for every tool call.
"""

from __future__ import annotations

import html
import re
import secrets
import time

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
                                      OAuthAuthorizationServerProvider, RefreshToken, TokenError,
                                      construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import REMOTE_PROVIDERS, RemoteConfig
from .store import AuthStore

SCOPE = "scholarocr"
ACCESS_TTL_S = 3600
REFRESH_TTL_S = 30 * 86400
CODE_TTL_S = 300
PENDING_TTL_S = 900

_KEY_SHAPE = re.compile(r"^[\x21-\x7e]{20,256}$")  # printable ASCII, no whitespace


def validate_api_key(provider: str, key: str) -> str | None:
    """Offline sanity check only (no network call). Returns an error message or None."""
    if provider not in REMOTE_PROVIDERS:
        return "Choose Gemini or OpenRouter."
    if not _KEY_SHAPE.match(key or ""):
        return "That does not look like an API key (20-256 printable characters, no spaces)."
    if provider == "openrouter" and not key.startswith("sk-or-"):
        return "OpenRouter keys start with sk-or-."
    return None


class ByokOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, cfg: RemoteConfig, store: AuthStore):
        self.cfg = cfg
        self.store = store

    # -------------------------------------------------------------- clients (DCR)
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.put_client(client_info.client_id, client_info.model_dump(mode="json"))

    # -------------------------------------------------------------- authorization
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        req_id = secrets.token_urlsafe(32)
        self.store.put_pending(req_id, {
            "client_id": client.client_id, "client_name": client.client_name,
            "params": params.model_dump(mode="json"),
        }, PENDING_TTL_S)
        return f"{self.cfg.public_url}/byok/connect?req={req_id}"

    def complete_authorization(self, req_id: str, user_id: str) -> str:
        """Issue a code for a pending request and return the client redirect URL."""
        pending = self.store.pop_pending(req_id)
        if pending is None:
            raise AuthorizeError("invalid_request", "authorization request expired; start again from ChatGPT")
        p = AuthorizationParams.model_validate(pending["params"])
        code = secrets.token_urlsafe(32)
        ac = AuthorizationCode(code=code, scopes=p.scopes or [SCOPE], expires_at=time.time() + CODE_TTL_S,
                               client_id=pending["client_id"], code_challenge=p.code_challenge,
                               redirect_uri=p.redirect_uri,
                               redirect_uri_provided_explicitly=p.redirect_uri_provided_explicitly,
                               resource=p.resource, subject=user_id)
        self.store.put_code(code, ac.model_dump(mode="json"), ac.expires_at)
        return construct_redirect_uri(str(p.redirect_uri), code=code, state=p.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        data = self.store.get_code(authorization_code)
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate(data)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        if self.store.get_code(authorization_code.code) is None:
            raise TokenError("invalid_grant", "authorization code already used or expired")
        self.store.delete_code(authorization_code.code)
        if not authorization_code.subject:
            raise TokenError("invalid_grant", "authorization code has no subject")
        return self._issue(client.client_id, authorization_code.subject, authorization_code.scopes,
                           authorization_code.resource, grant_id=secrets.token_hex(16))

    def _issue(self, client_id: str, user_id: str, scopes: list[str], resource: str | None,
               grant_id: str) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        self.store.put_token(access, kind="access", grant_id=grant_id, user_id=user_id, client_id=client_id,
                             scopes=scopes, resource=resource, expires_at=now + ACCESS_TTL_S)
        self.store.put_token(refresh, kind="refresh", grant_id=grant_id, user_id=user_id, client_id=client_id,
                             scopes=scopes, resource=resource, expires_at=now + REFRESH_TTL_S)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL_S,
                          refresh_token=refresh, scope=" ".join(scopes))

    # -------------------------------------------------------------- refresh
    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self.store.get_token(refresh_token, "refresh")
        if not row or row["client_id"] != client.client_id or self.store.get_key(row["user_id"]) is None:
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=row["scopes"],
                            expires_at=int(row["expires_at"]), resource=row["resource"], subject=row["user_id"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        row = self.store.get_token(refresh_token.token, "refresh")
        if not row:
            raise TokenError("invalid_grant", "refresh token expired or revoked")
        self.store.delete_grant(row["grant_id"])  # rotate both tokens
        granted = [s for s in (scopes or row["scopes"]) if s in row["scopes"]] or row["scopes"]
        return self._issue(client.client_id, row["user_id"], granted, row["resource"], grant_id=secrets.token_hex(16))

    # -------------------------------------------------------------- access tokens
    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.store.get_token(token, "access")
        if not row:
            return None
        if row["resource"] and row["resource"].rstrip("/") != self.cfg.mcp_url.rstrip("/"):
            return None  # token minted for another resource (RFC 8707)
        if self.store.get_key(row["user_id"]) is None:
            return None  # key removed (delete_my_data / retention): force re-linking
        return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"],
                           expires_at=int(row["expires_at"]), resource=self.cfg.mcp_url, subject=row["user_id"],
                           claims={"iss": self.cfg.public_url})

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        for kind in ("access", "refresh"):
            row = self.store.get_token(token.token, kind)
            if row:
                self.store.delete_grant(row["grant_id"])


# ------------------------------------------------------------------ consent page
PAGE_CSP = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https:; frame-ancestors 'none'; "
            "base-uri 'none'")


def _page(body: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Connect ScholarOCR</title>
<style>
:root{{--bg:#fafaf7;--fg:#1d1d1b;--muted:#5f5f5a;--line:#d9d8d0;--accent:#1f4fd1;--err:#a3271c}}
@media (prefers-color-scheme: dark){{:root{{--bg:#151514;--fg:#ecebe6;--muted:#a3a29b;--line:#3a3a36;--accent:#8fb0ff;--err:#ff8a7a}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:34rem;margin:0 auto;padding:2.5rem 1rem}}
h1{{font-size:1.4rem;margin:0 0 .5rem}} p,li{{color:var(--muted)}} label{{display:block;margin:1rem 0 .3rem;font-weight:600}}
input[type=password],select{{width:100%;box-sizing:border-box;padding:.6rem;border:1px solid var(--line);border-radius:6px;
background:transparent;color:var(--fg);font:inherit}}
.row{{display:flex;gap:.5rem;align-items:flex-start;margin-top:1rem}} .row label{{margin:0;font-weight:400;color:var(--fg)}}
button{{margin-top:1.25rem;padding:.65rem 1.1rem;border:0;border-radius:6px;background:var(--accent);color:#fff;font:inherit;
font-weight:600;cursor:pointer}} .err{{color:var(--err);font-weight:600}}
</style></head><body><main>{body}</main></body></html>"""
    return HTMLResponse(doc, status_code=status, headers={"Content-Security-Policy": PAGE_CSP,
                                                         "X-Frame-Options": "DENY", "Cache-Control": "no-store",
                                                         "Referrer-Policy": "no-referrer"})


def connect_form(cfg: RemoteConfig, req_id: str, client_name: str | None, error: str | None = None,
                 provider: str = "gemini") -> HTMLResponse:
    who = html.escape(client_name or "ChatGPT")
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    opts = "".join(f'<option value="{p}"{" selected" if p == provider else ""}>{label}</option>'
                   for p, label in (("gemini", "Google Gemini API key"), ("openrouter", "OpenRouter API key")))
    operator = ""
    if cfg.operator_key_mode:
        operator = ('<div class="row"><input type="checkbox" id="use_operator" name="use_operator" value="1">'
                    '<label for="use_operator">I do not have a key; use the operator-provided quota.</label></div>')
    body = f"""<h1>Connect ScholarOCR</h1>
<p>{who} is asking to use ScholarOCR on your behalf. ScholarOCR runs OCR with <strong>your own</strong> model API key,
so model usage is billed to your Gemini or OpenRouter account.</p>
<ul><li>The key is stored encrypted on this server and is used only for your OCR requests.</li>
<li>Uploaded documents and results are deleted after {cfg.retention_hours:g} hours.</li>
<li>Run the <em>delete_my_data</em> tool, or disconnect the app, to remove the key earlier.</li></ul>
{err}
<form method="post" action="{html.escape(cfg.public_url)}/byok/connect" autocomplete="off">
<input type="hidden" name="req" value="{html.escape(req_id)}">
<label for="provider">Provider</label><select id="provider" name="provider">{opts}</select>
<label for="api_key">API key</label><input type="password" id="api_key" name="api_key" spellcheck="false">
{operator}
<div class="row"><input type="checkbox" id="consent" name="consent" value="1">
<label for="consent">I allow ScholarOCR to store this key and use it to process documents I send.</label></div>
<button type="submit">Connect</button></form>"""
    return _page(body, 400 if error else 200)


def make_connect_routes(cfg: RemoteConfig, store: AuthStore, provider: ByokOAuthProvider):
    async def connect(request: Request) -> Response:
        if request.method == "GET":
            req_id = request.query_params.get("req", "")
            pending = store.get_pending(req_id)
            if pending is None:
                return _page("<h1>Link expired</h1><p>Start the connection again from ChatGPT.</p>", 400)
            return connect_form(cfg, req_id, pending.get("client_name"))
        form = await request.form()
        req_id = str(form.get("req", ""))
        pending = store.get_pending(req_id)
        if pending is None:
            return _page("<h1>Link expired</h1><p>Start the connection again from ChatGPT.</p>", 400)
        prov = str(form.get("provider", "gemini")).strip().lower()
        api_key = str(form.get("api_key", "")).strip()
        if form.get("consent") != "1":
            return connect_form(cfg, req_id, pending.get("client_name"), "Please confirm the consent box.", prov)
        if cfg.operator_key_mode and form.get("use_operator") == "1" and not api_key:
            user_id = "op_" + secrets.token_hex(12)
            store.put_key(user_id, prov if prov in REMOTE_PROVIDERS else "gemini", None, operator=True)
        else:
            problem = validate_api_key(prov, api_key)
            if problem:
                return connect_form(cfg, req_id, pending.get("client_name"), problem, prov)
            user_id = store.user_id_for_key(prov, api_key)
            store.put_key(user_id, prov, api_key)
        try:
            target = provider.complete_authorization(req_id, user_id)
        except AuthorizeError as e:
            return _page(f"<h1>Could not connect</h1><p>{html.escape(e.error_description or e.error)}</p>", 400)
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    return connect

