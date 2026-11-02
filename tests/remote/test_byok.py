"""Bring-your-own-key enforcement: the operator's key is never used unless operator-key mode is on."""

import json
import sqlite3

import pytest
from mcp import Client

from scholarocr.remote.sandbox import UserSandbox
from scholarocr.remote.services import ByoKeyRequired, user_settings
from scholarocr.remote.store import AuthStore, UserKey
from tests.remote.conftest import OPERATOR_ENV, USER_KEY


def test_no_user_key_and_no_operator_mode_raises(remote_cfg):
    sb = UserSandbox(remote_cfg, "u_" + "1" * 24)
    with pytest.raises(ByoKeyRequired):
        user_settings(remote_cfg, sb, None, OPERATOR_ENV)
    with pytest.raises(ByoKeyRequired):  # operator-mode account record, but mode switched off
        user_settings(remote_cfg, sb, UserKey("u_" + "1" * 24, "gemini", None, True, None), OPERATOR_ENV)


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
    assert other.get_key(uid) is None


async def test_tools_refuse_without_byo_key(remote, corpus):
    ns = remote()
    ns.identity.user = "u_" + "4" * 24  # authenticated, but no key linked
    f = ns.fetcher.add("https://files.example/p.pdf", corpus["handwritten.png"].read_bytes())
    async with Client(ns.mcp) as c:
        r = await c.call_tool("ocr_upload", {"files": [f]})
        cls = await c.call_tool("classify_document", {"document_id": "doc_0000000000000000"})
    assert r.is_error and "byo_key_required" in r.content[0].text
    assert r.meta["mcp/www_authenticate"][0].startswith("Bearer resource_metadata=")
    assert cls.is_error and "byo_key_required" in cls.content[0].text
    assert ns.transports.built == [] and ns.fetcher.calls == []  # nothing fetched, no model client built


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
