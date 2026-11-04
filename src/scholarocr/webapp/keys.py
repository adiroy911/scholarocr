"""Local storage for optional model API keys.

The OS keychain (``keyring``) is used when the package is installed and has a working backend; otherwise the
key goes into the user-level ``scholarocr.env`` (the same file the MCP server reads, see config.user_env_file).
Keys are never logged, never returned by the API (only "set" / "not set") and never shown again.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

from ..config import user_env_file
from ..exceptions import InputError
from ..logging_config import get_logger

log = get_logger("webapp.keys")
SERVICE = "ScholarOCR"
ENV_NAMES = {"gemini": "GEMINI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
KEY_RE = re.compile(r"^[A-Za-z0-9_\-.:]{16,256}$")


def _load_keyring() -> Any | None:
    try:
        import keyring  # type: ignore[import-not-found]
        from keyring.backends import fail  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
    except Exception:
        return None
    return keyring


class KeyStore:
    def __init__(self, env_file: Path | None = None, *, keyring_loader: Callable[[], Any | None] = _load_keyring):
        self.env_file = env_file or user_env_file(os.environ)
        self._keyring = keyring_loader()
        self._lock = threading.Lock()

    @property
    def backend(self) -> str:
        return "keychain" if self._keyring is not None else "file"

    @staticmethod
    def _check_provider(provider: str) -> str:
        if provider not in ENV_NAMES:
            raise InputError(f"unknown key provider {provider!r}; expected gemini or openrouter")
        return ENV_NAMES[provider]

    # ------------------------------------------------------------------ file backend
    def _read_file(self) -> list[str]:
        try:
            return self.env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def _file_get(self, name: str) -> str | None:
        for raw in self._read_file():
            line = raw.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == name:
                v = v.strip().strip('"').strip("'")
                return v or None
        return None

    def _file_set(self, name: str, value: str | None) -> None:
        lines = [ln for ln in self._read_file() if not (ln.strip().partition("=")[0].strip() == name
                                                       and not ln.strip().startswith("#"))]
        if value is not None:
            lines.append(f"{name}={value}")
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.env_file.with_name(self.env_file.name + ".tmp")
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.env_file)

    # ------------------------------------------------------------------ API
    def get(self, provider: str) -> str | None:
        name = self._check_provider(provider)
        with self._lock:
            if self._keyring is not None:
                try:
                    v = self._keyring.get_password(SERVICE, name)
                    if v:
                        return v
                except Exception:
                    log.warning("keychain read failed for %s; trying the settings file", provider)
            return self._file_get(name)

    def set(self, provider: str, key: str) -> str:
        name = self._check_provider(provider)
        key = (key or "").strip()
        if not KEY_RE.match(key):
            raise InputError("that does not look like an API key (16-256 letters, digits, - _ . :)")
        with self._lock:
            if self._keyring is not None:
                try:
                    try:  # Windows Credential Manager keeps the replaced value under "<name>@<service>" otherwise
                        self._keyring.delete_password(SERVICE, name)
                    except Exception:
                        pass  # nothing stored yet
                    self._keyring.set_password(SERVICE, name, key)
                    return "keychain"
                except Exception:
                    log.warning("keychain write failed for %s; using the settings file", provider)
            self._file_set(name, key)
            return "file"

    def delete(self, provider: str) -> None:
        name = self._check_provider(provider)
        with self._lock:
            if self._keyring is not None:
                try:
                    self._keyring.delete_password(SERVICE, name)
                except Exception:
                    pass
            if self._file_get(name) is not None:
                self._file_set(name, None)

    def status(self) -> dict[str, dict[str, Any]]:
        return {p: {"set": bool(self.get(p)), "backend": self.backend} for p in ENV_NAMES}
