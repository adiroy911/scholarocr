"""Post-deploy checks for a running scholarocr-remote server.

Usage:
    python scripts/check_remote_deploy.py https://scholarocr.fly.dev
    python scripts/check_remote_deploy.py http://127.0.0.1:8765        # local run; https checks are skipped

Checks (read-only; nothing is registered, no token is created):
  1. GET  /healthz                                      -> 200, {"status": "ok"}
  2. GET  /.well-known/oauth-protected-resource/mcp     -> resource = <base>/mcp, authorization server = <base>
  3. GET  /.well-known/oauth-protected-resource         -> same document at the root path
  4. GET  /.well-known/oauth-authorization-server       -> issuer and endpoints on <base>
  5. POST /mcp without a token                          -> 401 with a Bearer WWW-Authenticate pointing at <base>
  6. the base URL is https (skipped for loopback)
  7. plain http:// redirects to https:// (skipped for loopback)

Exit status is 0 when nothing FAILs. Requires httpx (installed with the [remote] extra).
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlparse

import httpx

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def line(self, status: str, name: str, detail: str = "") -> None:
        if status == "FAIL":
            self.failed += 1
        print(f"{status:<4}  {name}" + (f"  -- {detail}" if detail else ""))

    def check(self, ok: bool, name: str, detail: str = "") -> None:
        self.line("PASS" if ok else "FAIL", name, detail)


def _norm(url: object) -> str:
    return str(url or "").rstrip("/")


def _json(r: httpx.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def run(base: str, timeout: float) -> int:
    base = _norm(base)
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        print(f"not an http(s) origin: {base}", file=sys.stderr)
        return 2
    loopback = parsed.hostname in LOOPBACK
    rep = Report()
    print(f"Checking {base}\n")

    with httpx.Client(timeout=timeout, follow_redirects=False) as c:
        def get(path: str, **kw) -> httpx.Response | None:
            try:
                return c.get(base + path, **kw)
            except httpx.HTTPError as e:
                rep.line("FAIL", f"GET {path}", f"{e.__class__.__name__}: {e}")
                return None

        # 1. health
        r = get("/healthz")
        if r is not None:
            body = _json(r)
            rep.check(r.status_code == 200 and body.get("status") == "ok", "GET /healthz -> 200 ok",
                      f"status {r.status_code}, version {body.get('version', '?')}")

        # 2 + 3. protected-resource metadata (RFC 9728), SDK path and root path
        for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
            r = get(path)
            if r is None:
                continue
            body = _json(r)
            servers = [_norm(s) for s in body.get("authorization_servers") or []]
            ok = r.status_code == 200 and _norm(body.get("resource")) == base + "/mcp" and base in servers
            rep.check(ok, f"GET {path} -> resource on {base}",
                      f"status {r.status_code}, resource={body.get('resource')}, authorization_servers={servers}")

        # 4. authorization-server metadata (RFC 8414)
        r = get("/.well-known/oauth-authorization-server")
        if r is not None:
            body = _json(r)
            endpoints = {k: str(body.get(k) or "") for k in ("authorization_endpoint", "token_endpoint",
                                                              "registration_endpoint")}
            bad = [k for k, v in endpoints.items() if not v.startswith(base + "/")]
            ok = r.status_code == 200 and _norm(body.get("issuer")) == base and not bad
            pkce = "S256" in (body.get("code_challenge_methods_supported") or [])
            rep.check(ok, f"GET /.well-known/oauth-authorization-server -> issuer {base}",
                      f"status {r.status_code}, issuer={body.get('issuer')}"
                      + (f", endpoints not on base: {bad}" if bad else ""))
            rep.check(pkce, "authorization server advertises PKCE S256",
                      f"code_challenge_methods_supported={body.get('code_challenge_methods_supported')}")

        # 5. /mcp without a token
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "check_remote_deploy", "version": "1"}}}
        try:
            r = c.post(base + "/mcp", json=init,
                       headers={"Accept": "application/json, text/event-stream"})
        except httpx.HTTPError as e:
            rep.line("FAIL", "POST /mcp (no token)", f"{e.__class__.__name__}: {e}")
            r = None
        if r is not None:
            www = r.headers.get("www-authenticate", "")
            rep.check(r.status_code == 401, "POST /mcp without token -> 401", f"status {r.status_code}")
            rep.check(www.lower().startswith("bearer") and f"{base}/.well-known/oauth-protected-resource" in www,
                      "401 carries WWW-Authenticate: Bearer resource_metadata on base", www or "(header missing)")

        # 6 + 7. https
        if loopback:
            rep.line("SKIP", "base URL is https", "loopback address")
            rep.line("SKIP", "http:// redirects to https://", "loopback address")
        else:
            rep.check(parsed.scheme == "https", "base URL is https", base)
            if parsed.scheme == "https":
                try:
                    r = c.get("http://" + parsed.netloc + "/healthz")
                    loc = r.headers.get("location", "")
                    rep.check(r.status_code in (301, 302, 307, 308) and loc.startswith("https://"),
                              "http:// redirects to https://", f"status {r.status_code}, location={loc or '-'}")
                except httpx.HTTPError as e:
                    rep.line("FAIL", "http:// redirects to https://", f"{e.__class__.__name__}: {e}")

    print(f"\n{'ALL CHECKS PASSED' if rep.failed == 0 else f'{rep.failed} CHECK(S) FAILED'}")
    return 0 if rep.failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Post-deploy checks for scholarocr-remote.")
    p.add_argument("base_url", help="public origin, e.g. https://scholarocr.fly.dev")
    p.add_argument("--timeout", type=float, default=20.0, help="seconds per request (default 20)")
    a = p.parse_args(argv)
    return run(a.base_url, a.timeout)


if __name__ == "__main__":
    sys.exit(main())
