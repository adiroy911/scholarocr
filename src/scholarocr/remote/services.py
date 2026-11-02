"""One ``ScholarOCR`` service instance per user, built on that user's sandbox and (optional) key.

The per-user ``Settings`` are derived from the operator's environment with every model API key REMOVED.
Then, in this order:

1. the account has its own linked key -> that provider and key;
2. ``SCHOLAROCR_REMOTE_OPERATOR_KEY_MODE=1`` -> the operator's key (the operator pays);
3. otherwise -> host mode (``SCHOLAROCR_PROVIDER=host``): no server-side model at all; pages that need
   vision wait for ChatGPT's own model (get_page_image -> submit_page_content).

The operator's ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` / ``OPENROUTER_API_KEY`` are only ever used in case 2.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Mapping

from ..config import Settings, load_dotenv
from ..intelligence.gemini_client import Transport
from ..service import ScholarOCR
from .config import REMOTE_PROVIDERS, RemoteConfig
from .sandbox import UserSandbox
from .store import AuthStore, UserKey

KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY")
# Operator settings that make no sense per user, or would widen file access, are dropped too.
DROPPED_ENV_VARS = KEY_ENV_VARS + ("SCHOLAROCR_ALLOWED_DIRS", "SCHOLAROCR_DATA_DIR", "SCHOLAROCR_RETENTION_DAYS",
                                   "SCHOLAROCR_MAX_FILE_MB", "SCHOLAROCR_MAX_PAGES")

TransportFactory = Callable[[str, str, str | None], Transport | None]  # (user_id, provider, api_key) -> transport


class ByoKeyRequired(Exception):
    """Operator-key mode is on but the operator configured no key for the provider (a server misconfiguration)."""


def operator_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    if environ is None:
        load_dotenv(Path.cwd() / ".env", env)
    return env


def user_settings(cfg: RemoteConfig, sandbox: UserSandbox, user_key: UserKey | None,
                  environ: Mapping[str, str]) -> Settings:
    """Settings for one user's service: own key, else operator key (operator mode only), else host mode."""
    env = {k: v for k, v in environ.items() if k not in DROPPED_ENV_VARS}
    if user_key is not None and user_key.has_own_key:
        provider, api_key = user_key.provider, user_key.api_key
    elif cfg.operator_key_mode:
        provider = (user_key.provider if user_key and user_key.provider in REMOTE_PROVIDERS else None) or (
            environ.get("SCHOLAROCR_PROVIDER") or "gemini")
        provider = provider.strip().lower()
        if provider not in REMOTE_PROVIDERS:
            provider = "gemini"
        api_key = environ.get("OPENROUTER_API_KEY") if provider == "openrouter" else (
            environ.get("GEMINI_API_KEY") or environ.get("GOOGLE_API_KEY"))
        if not api_key:
            raise ByoKeyRequired(f"operator-key mode is on but the operator has no {provider} key configured")
    else:
        provider, api_key = "host", None
    env["SCHOLAROCR_PROVIDER"] = provider
    if api_key:
        env["OPENROUTER_API_KEY" if provider == "openrouter" else "GEMINI_API_KEY"] = api_key
    s = Settings.from_env(environ=env)
    return s.model_copy(update={
        "data_dir": sandbox.data_dir,
        "allowed_dirs": [sandbox.inbox],
        "max_file_mb": cfg.max_file_mb,
        "max_pages": cfg.max_pages,
        "retention_days": None,  # the remote janitor owns retention
    })


class ServicePool:
    """LRU cache of per-user services, rebuilt when the user's key changes."""

    def __init__(self, cfg: RemoteConfig, store: AuthStore, *, transport_factory: TransportFactory | None = None,
                 environ: Mapping[str, str] | None = None):
        self.cfg = cfg
        self.store = store
        self.transport_factory = transport_factory
        self.environ = operator_environment(environ) if environ is None else dict(environ)
        self._services: OrderedDict[str, tuple[str, ScholarOCR]] = OrderedDict()
        self._lock = threading.Lock()

    def sandbox(self, user_id: str) -> UserSandbox:
        return UserSandbox(self.cfg, user_id)

    def key_for(self, user_id: str) -> UserKey | None:
        return self.store.get_key(user_id)

    def get(self, user_id: str) -> ScholarOCR:
        user_key = self.key_for(user_id)
        sandbox = self.sandbox(user_id)
        settings = user_settings(self.cfg, sandbox, user_key, self.environ)
        fingerprint = f"{settings.effective_provider}:{hash(settings.api_key)}"
        with self._lock:
            hit = self._services.get(user_id)
            if hit and hit[0] == fingerprint:
                self._services.move_to_end(user_id)
                return hit[1]
            # Host mode has no key and therefore no transport at all.
            transport = (self.transport_factory(user_id, settings.effective_provider, settings.api_key)
                         if self.transport_factory and settings.api_key else None)
            svc = ScholarOCR(settings, transport=transport)
            self._services[user_id] = (fingerprint, svc)
            while len(self._services) > self.cfg.max_services:
                self._services.popitem(last=False)
            return svc

    def get_readonly(self, user_id: str) -> ScholarOCR:
        """A service for reading/deleting stored results even when the configured provider is unusable."""
        try:
            return self.get(user_id)
        except ByoKeyRequired:
            sandbox = self.sandbox(user_id)
            env = {k: v for k, v in self.environ.items() if k not in DROPPED_ENV_VARS}
            s = Settings.from_env(environ=env).model_copy(update={
                "data_dir": sandbox.data_dir, "allowed_dirs": [sandbox.inbox], "retention_days": None,
                "max_file_mb": self.cfg.max_file_mb, "max_pages": self.cfg.max_pages})
            return ScholarOCR(s)

    def drop(self, user_id: str) -> None:
        with self._lock:
            self._services.pop(user_id, None)

    def known_users(self) -> list[str]:
        d = self.cfg.users_dir
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []
