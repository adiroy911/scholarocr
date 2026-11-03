"""Fixtures for the web app tests. Nothing here starts the real ``agy`` or reaches a model API."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from scholarocr.webapp.agy import AgyResult
from scholarocr.webapp.config import AppConfig
from scholarocr.webapp.engines import EngineRegistry
from scholarocr.webapp.keys import KeyStore
from scholarocr.webapp.server import create_app
from tests.fixtures.fake_gemini import default_page

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
MODELS = [{"id": "gemini-3.8-flash-medium", "label": "Gemini 3.8 Flash (Medium)"},
          {"id": "gemini-3.8-flash-low", "label": "Gemini 3.8 Flash (Low)"}]


def page_payload(text: str = "Q1. Solve $x^2 + 5x + 6 = 0$. [2]") -> dict[str, Any]:
    """A valid host-mode submission (the fake page minus the deliberately broken equation)."""
    p = default_page(text)
    p["elements"] = [e for e in p["elements"] if not (e["type"] == "equation" and e["latex"].endswith("{3"))]
    fig = next(i for i, e in enumerate(p["elements"]) if e["type"] == "figure")
    for e in p["elements"]:
        if e["type"] == "caption":
            e["caption_of"] = fig
    return p


def broken_payload() -> dict[str, Any]:
    """The fake page as is: its second equation has unbalanced LaTeX marked low uncertainty -> rejected."""
    return default_page()


class FakeAgy:
    """Stands in for AgyRunner in RunManager tests: returns scripted replies, records every call."""

    def __init__(self, replies: list[Any] | None = None, chat_text: str = "Q1 asks to solve a quadratic [p. 1]."):
        self.replies = list(replies or [])
        self.chat_text = chat_text
        self.calls: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def run(self, prompt: str, **kw: Any) -> AgyResult:
        with self.lock:
            self.calls.append({"prompt": prompt, **kw})
            idx = len(self.calls)
        if kw.get("schema") is None:  # chat
            if kw.get("on_delta"):
                kw["on_delta"](self.chat_text)
            return AgyResult(self.chat_text, None, {"input_tokens": 900, "output_tokens": 40, "total_tokens": 940},
                             1.2, 1.3, f"conv-chat-{idx}")
        reply = self.replies.pop(0) if self.replies else page_payload()
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        structured = None if isinstance(reply, str) else reply
        return AgyResult(text, structured, {"input_tokens": 20000, "output_tokens": 1500, "thinking_tokens": 800,
                                            "total_tokens": 22300}, 30.0, 31.0, f"conv-{idx}")


class FakeRegistry(EngineRegistry):
    def __init__(self, cfg, keys, *, agy: bool = True):
        super().__init__(cfg, keys, which=lambda name: "C:/fake/agy.exe" if agy else None,
                         models_fn=lambda path: list(MODELS))


@pytest.fixture
def cfg(tmp_path) -> AppConfig:
    return AppConfig(data_dir=tmp_path / "app", port=PORT, page_timeout_s=5, chat_timeout_s=5)


@pytest.fixture
def keys(tmp_path) -> KeyStore:
    return KeyStore(tmp_path / "user.env", keyring_loader=lambda: None)


def make_client(cfg, keys, *, agy: FakeAgy | None = None, has_agy: bool = True, transport_factory=None):
    fake = agy if agy is not None else FakeAgy()
    asgi = create_app(cfg, keys=keys, registry=FakeRegistry(cfg, keys, agy=has_agy),
                      agy_factory=(lambda: fake) if has_agy else (lambda: None), token="test-token",
                      transport_factory=transport_factory, threaded_runs=False)
    client = TestClient(asgi, base_url=ORIGIN)
    client.headers.update({"X-ScholarOCR-Token": "test-token", "Origin": ORIGIN})
    return client, fake, asgi.state.app


@pytest.fixture
def client(cfg, keys):
    c, fake, app = make_client(cfg, keys)
    c.fake_agy, c.app = fake, app
    return c


def upload(client: TestClient, path: Path, name: str | None = None):
    return client.post("/api/uploads", params={"name": name or path.name}, content=path.read_bytes(),
                       headers={"Content-Type": "application/octet-stream"})


def events(client: TestClient, run_id: str) -> list[dict[str, Any]]:
    out = []
    with client.stream("GET", f"/api/runs/{run_id}/events") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


@pytest.fixture
def png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="PNG")
    return buf.getvalue()
