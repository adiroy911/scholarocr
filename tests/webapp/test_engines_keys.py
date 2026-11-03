"""Engine selection, key storage, and the key-provider path with a fake transport."""

from __future__ import annotations

import pytest

from scholarocr.webapp.agy import AgyError
from scholarocr.webapp.config import Prefs
from scholarocr.webapp.engines import EngineRegistry, NoEngineError, engine_settings
from scholarocr.webapp.keys import KeyStore
from tests.fixtures.fake_gemini import FakeTransport
from tests.webapp.conftest import MODELS, events, make_client, upload


def registry(cfg, keys, *, agy_path="C:/agy.exe", models=MODELS, fail=False, calls=None):
    def models_fn(path):
        if calls is not None:
            calls.append(path)
        if fail:
            raise AgyError("not signed in")
        return list(models)

    return EngineRegistry(cfg, keys, which=lambda name: agy_path, models_fn=models_fn)


def test_auto_prefers_antigravity(cfg, keys):
    keys.set("gemini", "AIzaSyTESTKEY000000000000000")
    s = registry(cfg, keys).resolve(None, Prefs())
    assert s.id == "antigravity" and s.default_model == "gemini-3.8-flash-medium"


def test_auto_falls_back_to_keys_in_order(cfg, keys):
    keys.set("openrouter", "sk-or-v1-0000000000000000000000")
    assert registry(cfg, keys, agy_path=None).resolve("auto", Prefs()).id == "openrouter"
    keys.set("gemini", "AIzaSyTESTKEY000000000000000")
    assert registry(cfg, keys, agy_path=None).resolve("auto", Prefs()).id == "gemini"


def test_agy_installed_but_not_signed_in_is_unavailable(cfg, keys):
    reg = registry(cfg, keys, fail=True)
    status = {s.id: s for s in reg.statuses(Prefs())}
    assert not status["antigravity"].available and "sign in" in status["antigravity"].reason.lower()
    with pytest.raises(NoEngineError, match="No engine is set up"):
        reg.resolve(None, Prefs())


def test_explicit_engine_must_be_available(cfg, keys):
    reg = registry(cfg, keys)
    with pytest.raises(NoEngineError, match="Gemini API key is not available"):
        reg.resolve("gemini", Prefs())
    with pytest.raises(NoEngineError, match="unknown engine"):
        reg.resolve("chatgpt", Prefs())
    assert reg.resolve(None, Prefs(engine="antigravity")).id == "antigravity"


def test_preference_and_stale_model(cfg, keys):
    keys.set("gemini", "AIzaSyTESTKEY000000000000000")
    reg = registry(cfg, keys)
    assert reg.resolve(None, Prefs(engine="gemini")).id == "gemini"
    # A remembered agy model that agy no longer lists falls back to the first listed one.
    s = reg.resolve("antigravity", Prefs(agy_model="gemini-9-gone"))
    assert s.default_model == MODELS[0]["id"]


def test_models_are_cached(cfg, keys):
    calls = []
    reg = registry(cfg, keys, calls=calls)
    reg.statuses(Prefs())
    reg.statuses(Prefs())
    assert len(calls) == 1
    reg.statuses(Prefs(), refresh=True)
    assert len(calls) == 2


def test_engine_settings_never_take_keys_from_the_environment(cfg):
    env = {"GEMINI_API_KEY": "from-env", "OPENROUTER_API_KEY": "from-env", "SCHOLAROCR_PROVIDER": "auto",
           "SCHOLAROCR_ALLOWED_DIRS": "C:/"}
    host = engine_settings(cfg, Prefs(), "antigravity", api_key=None, model=None, environ=env)
    assert host.effective_provider == "host" and host.gemini_api_key is None and host.openrouter_api_key is None
    assert host.allowed_dirs == [cfg.uploads_dir] and host.data_dir == cfg.engine_data_dir
    gem = engine_settings(cfg, Prefs(max_file_mb=7), "gemini", api_key="stored-key", model="gemini-x", environ=env)
    assert gem.effective_provider == "gemini" and gem.gemini_api_key == "stored-key" and gem.gemini_model == "gemini-x"
    assert gem.openrouter_api_key is None and gem.max_file_mb == 7


# ---------------------------------------------------------------------- keys
def test_file_backend_roundtrip(tmp_path):
    f = tmp_path / "scholarocr.env"
    f.write_text("SCHOLAROCR_ALLOWED_DIRS=D:/Study\nGEMINI_API_KEY=old\n", encoding="utf-8")
    ks = KeyStore(f, keyring_loader=lambda: None)
    assert ks.backend == "file" and ks.get("gemini") == "old"
    assert ks.set("gemini", "AIzaSyNEWKEY0000000000000000") == "file"
    text = f.read_text(encoding="utf-8")
    assert "SCHOLAROCR_ALLOWED_DIRS=D:/Study" in text and text.count("GEMINI_API_KEY") == 1
    ks.delete("gemini")
    assert ks.get("gemini") is None and "GEMINI_API_KEY" not in f.read_text(encoding="utf-8")
    assert "SCHOLAROCR_ALLOWED_DIRS=D:/Study" in f.read_text(encoding="utf-8")


def test_keychain_backend(tmp_path):
    class FakeKeyring:
        def __init__(self):
            self.data = {}

        def get_password(self, s, n):
            return self.data.get((s, n))

        def set_password(self, s, n, v):
            self.data[(s, n)] = v

        def delete_password(self, s, n):
            self.data.pop((s, n), None)

    kr = FakeKeyring()
    ks = KeyStore(tmp_path / "none.env", keyring_loader=lambda: kr)
    assert ks.set("openrouter", "sk-or-v1-0000000000000000000000") == "keychain"
    assert ks.get("openrouter") == "sk-or-v1-0000000000000000000000"
    assert not (tmp_path / "none.env").exists()
    assert ks.status()["openrouter"] == {"set": True, "backend": "keychain"}
    ks.delete("openrouter")
    assert ks.get("openrouter") is None


def test_key_validation(keys):
    from scholarocr.exceptions import InputError

    with pytest.raises(InputError):
        keys.set("gemini", "short")
    with pytest.raises(InputError):
        keys.set("gemini", "has spaces in it which is not a key")
    with pytest.raises(InputError):
        keys.set("anthropic", "AIzaSyNEWKEY0000000000000000")


def test_key_api_never_returns_the_key(cfg, keys):
    client, _, _ = make_client(cfg, keys)
    secret = "AIzaSySECRET000000000000000"
    r = client.put("/api/keys/gemini", json={"key": secret})
    assert r.status_code == 200 and secret not in r.text
    st = client.get("/api/state")
    assert secret not in st.text and st.json()["keys"]["gemini"]["set"] is True
    assert client.delete("/api/keys/gemini").json() == {"provider": "gemini", "set": False}


# ---------------------------------------------------------------------- key provider path (fake transport)
def test_gemini_key_engine_reads_and_answers_with_fake_transport(cfg, keys, corpus):
    keys.set("gemini", "AIzaSyTESTKEY000000000000000")
    transports = []

    def factory(engine, key):
        assert engine == "gemini" and key == "AIzaSyTESTKEY000000000000000"

        def responder(kind, parts, idx):
            if "Question:" in " ".join(getattr(p, "text", "") for p in parts):
                return {"answer": "Q1 is worth 2 marks [p. 1].", "cited_pages": [1]}
            if kind == "extract":
                from tests.webapp.conftest import page_payload
                return page_payload()
            return FakeTransport.default_responder(kind, parts, idx)

        t = FakeTransport(responder)
        transports.append(t)
        return t

    client, fake_agy, _ = make_client(cfg, keys, has_agy=False, transport_factory=factory)
    st = client.get("/api/state").json()
    assert not st["setup_needed"] and [e["id"] for e in st["engines"] if e["available"]] == ["gemini"]
    up = upload(client, corpus["math_paper.pdf"]).json()
    cid = client.post("/api/conversations").json()["id"]
    r = client.post(f"/api/conversations/{cid}/messages", json={"text": "Class 11", "uploads": [up["id"]]})
    assert r.status_code == 202, r.text
    evs = events(client, r.json()["run_id"])
    done = evs[-1]
    assert done["type"] == "done" and done["state"] == "done", done
    doc = done["data"]["documents"][0]
    view = client.get(f"/api/documents/{doc['document_id']}").json()
    assert view["pages_done"] == 1 and any(e["type"] == "table" for e in view["pages"][0]["elements"])
    assert "csv" in view["export_formats"]
    assert transports and any(c["kind"] == "extract" for c in transports[0].calls)
    assert fake_agy.calls == []  # no agy in key mode
    # The per-message text reached the model as a hint.
    assert "Class 11" in transports[0].calls[0]["text"]

    r = client.post(f"/api/conversations/{cid}/messages", json={"text": "How many marks is Q1?"})
    assert r.status_code == 202
    evs = events(client, r.json()["run_id"])
    assert evs[-1]["state"] == "done"
    msgs = client.get(f"/api/conversations/{cid}").json()["messages"]
    assert msgs[-1]["kind"] == "chat" and msgs[-1]["text"] == "Q1 is worth 2 marks [p. 1]."
    chat_call = [c for c in transports[-1].calls if "Question:" in c["text"]][-1]
    assert "=== math_paper.pdf | page 1 ===" in chat_call["text"]  # grounded in the extracted content
