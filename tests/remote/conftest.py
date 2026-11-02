"""Fixtures for the hosted (ChatGPT app) server. No network, no real model calls: downloads come from
an in-memory fetcher and every model call goes to tests.fixtures.fake_gemini.FakeTransport."""

from __future__ import annotations

import pytest

from scholarocr.exceptions import InputError, LimitExceededError
from scholarocr.remote.config import RemoteConfig
from scholarocr.remote.server import build_remote_server
from scholarocr.remote.services import ServicePool
from scholarocr.remote.store import AuthStore
from tests.fixtures.fake_gemini import FakeTransport

OPERATOR_ENV = {"GEMINI_API_KEY": "AIzaOPERATOR-KEY-must-never-be-used-0001",
                "OPENROUTER_API_KEY": "sk-or-OPERATOR-KEY-must-never-be-used",
                "SCHOLAROCR_ALLOWED_DIRS": "C:\\", "GEMINI_RPM_LIMIT": "0", "SCHOLAROCR_RENDER_DPI": "100"}
USER_KEY = "AIzaUSER-A-own-key-000000000000000001"
USER_B_KEY = "AIzaUSER-B-own-key-000000000000000002"


class FakeFetcher:
    """download_url -> bytes. Honours max_bytes like the real fetcher."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []

    def add(self, url: str, data: bytes) -> dict:
        self.files[url] = data
        return {"download_url": url, "file_id": "file_" + str(len(self.files)), "mime_type": "", "file_name": ""}

    def fetch(self, url: str, *, max_bytes: int, timeout_s: float):
        self.calls.append(url)
        if url not in self.files:
            raise InputError("file download failed with HTTP 404")
        data = self.files[url]
        if len(data) > max_bytes:
            raise LimitExceededError("file is larger than the limit (SCHOLAROCR_REMOTE_MAX_FILE_MB)")
        return data, None


class Identity:
    def __init__(self, user: str | None = None):
        self.user = user

    def __call__(self):
        return self.user


class Transports:
    """transport_factory that records which key each service was built with."""

    def __init__(self):
        self.built: list[tuple[str, str, str | None]] = []
        self.fakes: dict[str, FakeTransport] = {}

    def __call__(self, user_id, provider, api_key):
        self.built.append((user_id, provider, api_key))
        return self.fakes.setdefault(user_id, FakeTransport())


@pytest.fixture
def remote_cfg(tmp_path) -> RemoteConfig:
    return RemoteConfig(root_dir=tmp_path / "remote", secret="test-secret-" + "x" * 40, auth="oauth",
                        public_url="http://127.0.0.1:8000", max_file_mb=20, max_base64_mb=1, calls_per_minute=0,
                        ocr_calls_per_hour=0, pages_per_day=0)


@pytest.fixture
def remote(remote_cfg):
    """Factory: remote(**cfg_overrides) -> namespace with cfg, store, pool, mcp, identity, fetcher, transports."""

    def make(**overrides):
        cfg = remote_cfg.model_copy(update=overrides)
        cfg.root_dir.mkdir(parents=True, exist_ok=True)
        store = AuthStore(cfg.auth_db, cfg.secret)
        transports = Transports()
        pool = ServicePool(cfg, store, transport_factory=transports, environ=dict(OPERATOR_ENV))
        identity, fetcher = Identity(), FakeFetcher()
        comps = build_remote_server(cfg, store=store, pool=pool, fetcher=fetcher, identity=identity)

        class NS:
            pass

        ns = NS()
        ns.cfg, ns.store, ns.pool, ns.mcp, ns.identity = cfg, store, pool, comps.mcp, identity
        ns.fetcher, ns.transports, ns.comps = fetcher, transports, comps

        def login(api_key: str = USER_KEY, provider: str = "gemini") -> str:
            uid = store.user_id_for_key(provider, api_key)
            store.put_key(uid, provider, api_key)
            identity.user = uid
            return uid

        ns.login = login
        return ns

    return make
