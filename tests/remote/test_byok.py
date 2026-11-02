"""Keys are optional: without one an account runs in host mode. The operator's key is never used unless
operator-key mode is on, and a user's own key can be linked and unlinked without changing the account."""

import json
import sqlite3

import pytest
from mcp import Client

from scholarocr.remote.sandbox import UserSandbox
from scholarocr.remote.services import ByoKeyRequired, user_settings
from scholarocr.remote.store import AuthStore, UserKey
from tests.remote.conftest import OPERATOR_ENV, USER_KEY


def test_no_user_key_and_no_operator_mode_is_host_mode(remote_cfg):
    sb = UserSandbox(remote_cfg, "u_" + "1" * 24)
    for key in (None, UserKey(sb.user_id, "host", None, False, None),
                UserKey(sb.user_id, "gemini", None, True, None)):  # operator-mode record, mode switched off
        s = user_settings(remote_cfg, sb, key, OPERATOR_ENV)
        assert s.effective_provider == "host" and s.api_key is None
        assert s.gemini_api_key is None and s.openrouter_api_key is None
        assert "OPERATOR" not in json.dumps(s.model_dump(mode="json"))


def test_user_key_replaces_operator_key(remote_cfg):
    sb = UserSandbox(remote_cfg, "u_" + "2" * 24)
    s = user_settings(remote_cfg, sb, UserKey(sb.user_id, "gemini", USER_KEY, False, None), OPERATOR_ENV)
    assert s.api_key == USER_KEY and s.provider == "gemini"
    assert s.openrouter_api_key is None  # operator's OpenRouter key was stripped too
    assert s.allowed_dirs == [sb.inbox]  # operator SCHOLAROCR_ALLOWED_DIRS ignored
    assert s.data_dir == sb.data_dir
    o = user_settings(remote_cfg, sb, UserKey(sb.user_id, "openrouter", "sk-or-user-own-key-123456789", False, None),
                      OPERATOR_ENV)
    assert o.provider == "openrouter" and o.api_key == "sk-or-user-own-key-123456789"
    assert o.gemini_api_key is None
    assert "OPERATOR" not in json.dumps(s.model_dump(mode="json")) + json.dumps(o.model_dump(mode="json"))


def test_operator_mode_is_explicit_opt_in(remote_cfg):
    cfg = remote_cfg.model_copy(update={"operator_key_mode": True})
    sb = UserSandbox(cfg, "u_" + "3" * 24)
    s = user_settings(cfg, sb, None, OPERATOR_ENV)
    assert s.api_key == OPERATOR_ENV["GEMINI_API_KEY"]
    # a user who linked a key still pays with their own key
    own = user_settings(cfg, sb, UserKey(sb.user_id, "gemini", USER_KEY, False, None), OPERATOR_ENV)
    assert own.api_key == USER_KEY
    with pytest.raises(ByoKeyRequired, match="operator has no"):
        user_settings(cfg, sb, None, {})


def test_keys_encrypted_at_rest(tmp_path):
    store = AuthStore(tmp_path / "auth.db", "s" * 40)
    uid = store.user_id_for_key("gemini", USER_KEY)
    store.put_key(uid, "gemini", USER_KEY)
    raw = sqlite3.connect(tmp_path / "auth.db").execute("SELECT ciphertext, key_hint FROM user_keys").fetchone()
    assert USER_KEY.encode() not in raw[0] and raw[1] == "..." + USER_KEY[-4:]
    assert store.get_key(uid).api_key == USER_KEY
    # same key -> same user id; different secret -> unreadable
    assert store.user_id_for_key("gemini", USER_KEY) == uid
    other = AuthStore(tmp_path / "auth.db", "t" * 40)
    assert other.get_key(uid).api_key is None and other.get_key(uid).provider == "host"  # unreadable -> host


async def test_without_key_the_account_runs_in_host_mode(remote, corpus):
    ns = remote()
    ns.identity.user = ns.store.create_account()  # signed in, no key: host mode
    f = ns.fetcher.add("https://files.example/p.png", corpus["handwritten.png"].read_bytes())
    async with Client(ns.mcp) as c:
        r = await c.call_tool("ocr_upload", {"files": [f]})
        assert not r.is_error, r.content[0].text
        sc = r.structured_content
        assert sc["mode"] == "host" and sc["awaiting_transcription"] == [1]
        doc = sc["document_id"]
        img = await c.call_tool("get_page_image", {"document_id": doc})
        assert not img.is_error and [b.type for b in img.content] == ["image", "text"]
        assert img.meta["scholarocr/pageImage"]["mime_type"].startswith("image/")  # for the ChatGPT viewer only
        assert "data" not in json.dumps(img.structured_content)  # the model-facing JSON carries no base64
        ok = await c.call_tool("submit_page_content", {"document_id": doc, "page_number": 1, "elements": [
            {"type": "handwriting", "text": "Notes on enzymes", "handwritten": True, "box_2d": [80, 60, 140, 900]}]})
        assert not ok.is_error and ok.structured_content["accepted"]
        content = await c.call_tool("get_document_content", {"document_id": doc})
        assert "Notes on enzymes" in content.structured_content["content"]
        cls = await c.call_tool("classify_document", {"document_id": doc})
        assert cls.is_error and "model_unavailable" in cls.content[0].text
        acct = await c.call_tool("account_status", {})
        assert acct.structured_content["mode"] == "host" and acct.structured_content["key"] == "none (host mode)"
    assert ns.transports.built == []  # no model client was ever built: the operator key cannot be used
    for t in ns.transports.fakes.values():
        assert t.calls == []


async def test_link_and_unlink_key_keeps_the_account(remote, corpus):
    ns = remote()
    uid = ns.identity.user = ns.store.create_account()
    assert uid.startswith("u_") and ns.store.get_key(uid).provider == "host"
    async with Client(ns.mcp) as c:
        link = await c.call_tool("manage_model_key", {"action": "link"})
        url = link.structured_content["url"]
        assert url.startswith(ns.cfg.public_url + "/byok/key?t=") and USER_KEY not in link.content[0].text
        token = url.split("t=", 1)[1]
        assert ns.store.peek_link_token(token) == uid
        # (the browser page validates and calls link_key; tested over HTTP in test_http_oauth)
        assert ns.store.consume_link_token(token) == uid and ns.store.consume_link_token(token) is None
        ns.store.link_key(uid, "gemini", USER_KEY)
        f = ns.fetcher.add("https://files.example/n.png", corpus["handwritten.png"].read_bytes())
        r = await c.call_tool("ocr_upload", {"files": [f]})
        assert not r.is_error and r.structured_content["mode"] == "gemini"
        assert ns.transports.built == [(uid, "gemini", USER_KEY)]
        acct = await c.call_tool("account_status", {})
        assert acct.structured_content["mode"] == "gemini" and acct.structured_content["key"] == "..." + USER_KEY[-4:]
        un = await c.call_tool("manage_model_key", {"action": "unlink"})
        assert un.structured_content["unlinked"] is True and un.structured_content["mode"] == "host"
        assert ns.store.get_key(uid).api_key is None and ns.store.account_exists(uid)
        listed = await c.call_tool("list_documents", {})
        assert listed.structured_content["total"] == 1  # documents survive unlinking
        acct = await c.call_tool("account_status", {})
        assert acct.structured_content["mode"] == "host"


async def test_operator_key_never_used_without_operator_mode(remote, corpus):
    ns = remote()
    ns.identity.user = ns.store.create_account(operator=True)  # even an operator-flagged account
    f = ns.fetcher.add("https://files.example/q.png", corpus["handwritten.png"].read_bytes())
    async with Client(ns.mcp) as c:
        r = await c.call_tool("ocr_upload", {"files": [f]})
        acct = await c.call_tool("account_status", {})
    assert r.structured_content["mode"] == "host" and ns.transports.built == []
    assert acct.structured_content["billing"].startswith("no model API calls")
    assert "OPERATOR" not in json.dumps(r.structured_content) + json.dumps(acct.structured_content)


async def test_model_client_gets_the_users_key(remote, corpus):
    ns = remote()
    uid = ns.login()
    f = ns.fetcher.add("https://files.example/notes", corpus["handwritten.png"].read_bytes())
    async with Client(ns.mcp) as c:
        r = await c.call_tool("ocr_upload", {"files": [f]})
    assert not r.is_error, r.content[0].text
    assert ns.transports.built == [(uid, "gemini", USER_KEY)]
    assert ns.transports.fakes[uid].calls, "extraction should have gone through the (fake) model"


async def test_operator_mode_serves_users_without_key(remote, corpus):
    ns = remote(operator_key_mode=True)
    ns.identity.user = "op_" + "5" * 24
    ns.store.put_key(ns.identity.user, "gemini", None, operator=True)
    f = ns.fetcher.add("https://files.example/p.pdf", corpus["handwritten.png"].read_bytes())
    async with Client(ns.mcp) as c:
        r = await c.call_tool("ocr_upload", {"files": [f]})
        acct = await c.call_tool("account_status", {})
    assert not r.is_error, r.content[0].text
    assert ns.transports.built[0][2] == OPERATOR_ENV["GEMINI_API_KEY"]
    assert acct.structured_content["billing"] == "operator"
    assert OPERATOR_ENV["GEMINI_API_KEY"] not in acct.content[0].text
