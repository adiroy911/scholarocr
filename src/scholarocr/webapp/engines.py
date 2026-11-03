"""Which engine reads the pages, and one ScholarOCR service per engine.

Engines:

* ``antigravity``: Antigravity CLI signed in with the user's own Google account. The service runs in host
  mode (no key, no server-side model) and the app hands each page awaiting transcription to ``agy -p``.
* ``gemini`` / ``openrouter``: the server-side pipeline with a key the user stored in Settings.

``auto`` picks Antigravity when ``agy`` is on PATH and lists models, else Gemini, else OpenRouter. With no
engine at all the UI shows the setup screen.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import DEFAULT_MODEL, DEFAULT_OPENROUTER_MODEL, Settings
from ..exceptions import ScholarOCRError
from ..intelligence.gemini_client import Transport
from ..service import ScholarOCR
from .agy import AgyError, find_agy, list_models
from .config import AppConfig, Prefs
from .keys import KeyStore

ENGINE_LABELS = {
    "antigravity": "Antigravity (your Gemini account)",
    "gemini": "Gemini API key",
    "openrouter": "OpenRouter key",
}
ENGINE_ORDER = ("antigravity", "gemini", "openrouter")
DROPPED_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "SCHOLAROCR_ALLOWED_DIRS",
               "SCHOLAROCR_DATA_DIR", "SCHOLAROCR_PROVIDER", "SCHOLAROCR_MAX_FILE_MB", "SCHOLAROCR_MAX_PAGES",
               "GEMINI_MODEL", "OPENROUTER_MODEL")
MODELS_TTL_S = 600.0


class NoEngineError(ScholarOCRError):
    code = "no_engine"


@dataclass
class EngineStatus:
    id: str
    label: str
    available: bool
    reason: str = ""
    models: list[dict[str, str]] = field(default_factory=list)
    default_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "available": self.available, "reason": self.reason,
                "models": self.models, "default_model": self.default_model}


class EngineRegistry:
    def __init__(self, cfg: AppConfig, keys: KeyStore, *, which: Callable[[str], str | None] | None = None,
                 models_fn: Callable[[str], list[dict[str, str]]] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.keys = keys
        self._which = which
        self._models_fn = models_fn or (lambda path: list_models(path))
        self._clock = clock
        self._lock = threading.Lock()
        self._agy_cache: tuple[float, str | None, list[dict[str, str]], str] | None = None

    def agy_path(self) -> str | None:
        if self._which is not None:
            return find_agy(self.cfg.agy_path, which=self._which)
        return find_agy(self.cfg.agy_path)

    def _agy_state(self, refresh: bool = False) -> tuple[str | None, list[dict[str, str]], str]:
        with self._lock:
            hit = self._agy_cache
            if hit and not refresh and self._clock() - hit[0] < MODELS_TTL_S:
                return hit[1], hit[2], hit[3]
        path = self.agy_path()
        models: list[dict[str, str]] = []
        reason = ""
        if path is None:
            reason = "Antigravity CLI (agy) is not on PATH."
        else:
            try:
                models = self._models_fn(path)
            except AgyError as e:
                reason = f"agy is installed but did not list models ({e.message}). Run agy once and sign in."
        with self._lock:
            self._agy_cache = (self._clock(), path, models, reason)
        return path, models, reason

    def statuses(self, prefs: Prefs, *, refresh: bool = False) -> list[EngineStatus]:
        path, models, reason = self._agy_state(refresh)
        agy_ok = path is not None and bool(models)
        default_agy = prefs.agy_model if any(m["id"] == prefs.agy_model for m in models) else (
            models[0]["id"] if models else prefs.agy_model)
        out = [EngineStatus("antigravity", ENGINE_LABELS["antigravity"], agy_ok, reason, models, default_agy)]
        for p, default in (("gemini", prefs.gemini_model or DEFAULT_MODEL),
                           ("openrouter", prefs.openrouter_model or DEFAULT_OPENROUTER_MODEL)):
            has = bool(self.keys.get(p))
            out.append(EngineStatus(p, ENGINE_LABELS[p], has, "" if has else "Add a key in Settings.",
                                    [{"id": default, "label": default}], default))
        return out

    def resolve(self, requested: str | None, prefs: Prefs) -> EngineStatus:
        """The engine to use for a run. ``requested`` None or "auto" follows the preference, then the order."""
        statuses = {s.id: s for s in self.statuses(prefs)}
        want = requested if requested and requested != "auto" else prefs.engine
        if want and want != "auto":
            if want not in statuses:
                raise NoEngineError(f"unknown engine {want!r}")
            s = statuses[want]
            if not s.available:
                raise NoEngineError(f"{s.label} is not available: {s.reason}")
            return s
        for eid in ENGINE_ORDER:
            if statuses[eid].available:
                return statuses[eid]
        raise NoEngineError("No engine is set up. Install Antigravity CLI and sign in, or add a key in Settings.")


def engine_settings(cfg: AppConfig, prefs: Prefs, engine: str, *, api_key: str | None, model: str | None,
                    environ: dict[str, str] | None = None) -> Settings:
    """Service settings for one engine. Keys from the process environment are never used; only the stored one."""
    env = {k: v for k, v in (os.environ if environ is None else environ).items() if k not in DROPPED_ENV}
    if engine in ("gemini", "openrouter"):
        env["SCHOLAROCR_PROVIDER"] = engine
        env["OPENROUTER_API_KEY" if engine == "openrouter" else "GEMINI_API_KEY"] = api_key or ""
        if model:
            env["OPENROUTER_MODEL" if engine == "openrouter" else "GEMINI_MODEL"] = model
    else:
        env["SCHOLAROCR_PROVIDER"] = "host"
    s = Settings.from_env(environ=env)
    return s.model_copy(update={
        "data_dir": cfg.engine_data_dir,
        "allowed_dirs": [cfg.uploads_dir],
        "max_file_mb": prefs.max_file_mb,
        "max_pages": prefs.max_pages,
        "retention_days": None,
        "env_file": None,  # the app manages its own uploads folder
    })


class ServicePool:
    """One ScholarOCR service per (engine, model, key). They share one data directory, so documents read by
    any engine can be opened, exported and discussed with any other."""

    def __init__(self, cfg: AppConfig, keys: KeyStore, *,
                 transport_factory: Callable[[str, str], Transport | None] | None = None):
        self.cfg = cfg
        self.keys = keys
        self.transport_factory = transport_factory
        self._services: dict[tuple, ScholarOCR] = {}
        self._lock = threading.Lock()

    def get(self, engine: str, prefs: Prefs, model: str | None = None) -> ScholarOCR:
        key = self.keys.get(engine) if engine in ("gemini", "openrouter") else None
        if engine in ("gemini", "openrouter") and not key:
            raise NoEngineError(f"No {engine} key is stored. Add one in Settings.")
        sig = (engine, model if engine != "antigravity" else None, hash(key), prefs.max_file_mb, prefs.max_pages)
        with self._lock:
            svc = self._services.get(sig)
            if svc is None:
                settings = engine_settings(self.cfg, prefs, engine, api_key=key, model=model)
                transport = self.transport_factory(engine, key) if (self.transport_factory and key) else None
                svc = ScholarOCR(settings, transport=transport)
                self._services[sig] = svc
            return svc

    def reader(self, prefs: Prefs) -> ScholarOCR:
        """A host-mode service for reading, exporting and verifying (never calls a model)."""
        return self.get("antigravity", prefs)

    def drop_engine(self, engine: str) -> None:
        with self._lock:
            for sig in [s for s in self._services if s[0] == engine]:
                self._services.pop(sig, None)
