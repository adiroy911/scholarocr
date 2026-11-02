"""Configuration loaded from environment variables (and an optional ``.env`` file).

All model identifiers live here and nowhere else. Nothing in this module logs secrets;
``Settings.redacted()`` is what callers should print.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel, Field, field_validator

APP_NAME = os.environ.get("SCHOLAROCR_APP_NAME", "ScholarOCR")

# Defaults verified against https://ai.google.dev/gemini-api/docs/models (2026-11).
# Override with GEMINI_MODEL / GEMINI_REVIEW_MODEL; they are not referenced anywhere else.
DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_REVIEW_MODEL = "gemini-3.8-flash"
# Used when SCHOLAROCR_PROVIDER=openrouter and OPENROUTER_MODEL is unset (free, image input, JSON schema).
DEFAULT_OPENROUTER_MODEL = "nvidia/nemotron-3.5-super-vl:free"


def _default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "ScholarOCR"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "scholarocr"


def user_env_file(environ: Mapping[str, str]) -> Path:
    """Optional user-level settings file: ``SCHOLAROCR_ENV_FILE``, else ``<default data dir>/scholarocr.env``
    (``%LOCALAPPDATA%/ScholarOCR/scholarocr.env`` on Windows, ``~/.local/share/scholarocr/scholarocr.env``)."""
    explicit = environ.get("SCHOLAROCR_ENV_FILE")
    return Path(explicit).expanduser() if explicit else _default_data_dir() / "scholarocr.env"


def load_dotenv(path: Path, environ: dict[str, str]) -> None:
    """Minimal ``KEY=VALUE`` parser. Existing non-empty environment variables win.

    An empty variable counts as unset: MCP clients (Antigravity CLI and others) may pass a blank
    ``GEMINI_API_KEY=""`` when a setting is left empty or a ``$VAR`` expands to nothing, and that
    must not hide the key in ``.env``.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and not environ.get(key):
            environ[key] = value


# "host" (default from the environment): no model API; pages that need vision are transcribed by the MCP
#   client's own model. An exported GEMINI_API_KEY / OPENROUTER_API_KEY is ignored unless a key provider
#   is chosen explicitly with SCHOLAROCR_PROVIDER.
# "auto" (opt-in): a key provider when its key is configured, otherwise host mode.
# "gemini" / "openrouter": that provider (host mode if its key is missing). See Settings.effective_provider.
PROVIDERS = ("auto", "host", "gemini", "openrouter")
KEY_PROVIDERS = ("gemini", "openrouter")
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class Settings(BaseModel):
    # Requested provider: "host", "auto", "gemini" (google-genai SDK) or "openrouter" (OpenAI-compatible
    # chat completions with image input). ``effective_provider`` is what is used. ``from_env`` (CLI, MCP
    # servers, every end user) defaults to "host" when SCHOLAROCR_PROVIDER is unset, so an exported key is
    # never used by itself. Settings built in code default to "auto": a caller that passes a key or a
    # transport explicitly has opted in.
    # The gemini_* model / timeout / retry / rpm fields apply to whichever key provider is active.
    provider: str = "auto"
    host_image_max_side: int = 2000  # longest side of page images handed to the host model
    openrouter_api_key: str | None = Field(default=None, repr=False)
    openrouter_base_url: str = DEFAULT_OPENROUTER_BASE_URL
    openrouter_max_tokens: int = 16384  # output cap per call, reasoning included
    openrouter_reasoning_effort: str = "low"  # none | minimal | low | medium | high
    gemini_api_key: str | None = Field(default=None, repr=False)
    gemini_model: str = DEFAULT_MODEL
    gemini_review_model: str = DEFAULT_REVIEW_MODEL
    gemini_math_model: str | None = None
    gemini_timeout_seconds: float = 180.0
    gemini_max_retries: int = 4
    gemini_rpm_limit: int = 30
    concurrency: int = 2

    data_dir: Path = Field(default_factory=_default_data_dir)
    log_level: str = "INFO"
    allowed_dirs: list[Path] = Field(default_factory=lambda: [Path.cwd()])
    max_file_mb: float = 200.0
    max_pages: int = 1000

    render_dpi: int = 200
    sync_page_limit: int = 4
    pricing_file: Path | None = None
    retention_days: float | None = None  # delete generated artifacts of documents idle this long
    text_layer_font: Path | None = None

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        v = (v or "auto").strip().lower()
        if v not in PROVIDERS:
            raise ValueError(f"unknown provider {v!r}; expected one of {', '.join(PROVIDERS)}")
        return v

    def _key_of(self, provider: str) -> str | None:
        return self.openrouter_api_key if provider == "openrouter" else self.gemini_api_key

    @property
    def effective_provider(self) -> str:
        """Provider actually used. Precedence:

        1. ``provider="host"`` (what ``from_env`` uses when SCHOLAROCR_PROVIDER is unset): host mode,
           even when keys are configured, so an exported key never switches on a paid provider by itself.
        2. ``provider="gemini"`` / ``"openrouter"``: that provider when its key is set; without the key
           there is no way to call it, so host mode is used and ``provider_note`` says why.
        3. ``provider="auto"`` (opt-in via SCHOLAROCR_PROVIDER; the default for Settings built in code): Gemini if a Gemini key is set, else OpenRouter if an OpenRouter
           key is set, else host mode.
        """
        p = self.provider
        if p == "auto":
            if self.gemini_api_key:
                return "gemini"
            if self.openrouter_api_key:
                return "openrouter"
            return "host"
        if p in KEY_PROVIDERS and not self._key_of(p):
            return "host"
        return p

    @property
    def provider_note(self) -> str | None:
        if self.provider in KEY_PROVIDERS and not self._key_of(self.provider):
            return (f"SCHOLAROCR_PROVIDER={self.provider} but {self.api_key_env_name} is not set; using host mode "
                    "(the MCP client's own model transcribes pages that need vision).")
        return None

    @property
    def api_key(self) -> str | None:
        """Key for the effective provider (None in host mode)."""
        p = self.effective_provider
        return self._key_of(p) if p in KEY_PROVIDERS else None

    @property
    def api_key_env_name(self) -> str:
        p = self.provider if self.provider in KEY_PROVIDERS else self.effective_provider
        return "OPENROUTER_API_KEY" if p == "openrouter" else "GEMINI_API_KEY"

    @field_validator("concurrency", "gemini_max_retries", "gemini_rpm_limit", "render_dpi", "max_pages")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be >= 0")
        return v

    @property
    def math_model(self) -> str:
        return self.gemini_math_model or self.gemini_model

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scholarocr.db"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    def redacted(self) -> dict:
        d = self.model_dump(mode="json")
        d["gemini_api_key"] = "set" if self.gemini_api_key else "not set"
        d["openrouter_api_key"] = "set" if self.openrouter_api_key else "not set"
        d["effective_provider"] = self.effective_provider
        if self.provider_note:
            d["provider_note"] = self.provider_note
        return d

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, dotenv: Path | None = None) -> "Settings":
        env = dict(os.environ if environ is None else environ)
        if environ is None:
            load_dotenv(dotenv or Path.cwd() / ".env", env)
            # Then the user-level file (lowest precedence): for clients that start the server outside the
            # workspace, such as Antigravity plugins (cwd = plugin directory).
            load_dotenv(user_env_file(env), env)

        def get(name: str) -> str | None:
            v = env.get(name)
            return v if v not in (None, "") else None

        kw: dict = {}
        mapping = {
            "GEMINI_API_KEY": "gemini_api_key",
            "GEMINI_MODEL": "gemini_model",
            "GEMINI_REVIEW_MODEL": "gemini_review_model",
            "GEMINI_MATH_MODEL": "gemini_math_model",
            "GEMINI_TIMEOUT_SECONDS": "gemini_timeout_seconds",
            "GEMINI_MAX_RETRIES": "gemini_max_retries",
            "GEMINI_RPM_LIMIT": "gemini_rpm_limit",
            "SCHOLAROCR_CONCURRENCY": "concurrency",
            "SCHOLAROCR_DATA_DIR": "data_dir",
            "SCHOLAROCR_LOG_LEVEL": "log_level",
            "SCHOLAROCR_MAX_FILE_MB": "max_file_mb",
            "SCHOLAROCR_MAX_PAGES": "max_pages",
            "SCHOLAROCR_RENDER_DPI": "render_dpi",
            "SCHOLAROCR_SYNC_PAGE_LIMIT": "sync_page_limit",
            "SCHOLAROCR_PRICING_FILE": "pricing_file",
            "SCHOLAROCR_TEXT_LAYER_FONT": "text_layer_font",
            "SCHOLAROCR_RETENTION_DAYS": "retention_days",
            "SCHOLAROCR_HOST_IMAGE_MAX_SIDE": "host_image_max_side",
        }
        for env_name, field in mapping.items():
            v = get(env_name)
            if v is not None:
                kw[field] = v
        if kw.get("gemini_api_key") is None and get("GOOGLE_API_KEY"):
            kw["gemini_api_key"] = get("GOOGLE_API_KEY")
        kw["provider"] = (get("SCHOLAROCR_PROVIDER") or "host").strip().lower()
        if get("OPENROUTER_API_KEY"):
            kw["openrouter_api_key"] = get("OPENROUTER_API_KEY")
        if get("OPENROUTER_BASE_URL"):
            kw["openrouter_base_url"] = get("OPENROUTER_BASE_URL")
        if get("OPENROUTER_MAX_TOKENS"):
            kw["openrouter_max_tokens"] = get("OPENROUTER_MAX_TOKENS")
        if get("OPENROUTER_REASONING_EFFORT"):
            kw["openrouter_reasoning_effort"] = get("OPENROUTER_REASONING_EFFORT")
        # Resolve "auto" now so the model defaults below match the provider that will be used.
        provider = kw["provider"] if kw["provider"] in KEY_PROVIDERS else cls(
            provider=kw["provider"], gemini_api_key=kw.get("gemini_api_key"),
            openrouter_api_key=kw.get("openrouter_api_key")).effective_provider
        if provider == "openrouter":
            # OPENROUTER_* model ids replace GEMINI_* ones, so one .env can hold both setups.
            for env_name, field in (("OPENROUTER_MODEL", "gemini_model"),
                                    ("OPENROUTER_REVIEW_MODEL", "gemini_review_model"),
                                    ("OPENROUTER_MATH_MODEL", "gemini_math_model")):
                if get(env_name):
                    kw[field] = get(env_name)
            if not get("OPENROUTER_MODEL"):
                kw["gemini_model"] = DEFAULT_OPENROUTER_MODEL
            if not get("OPENROUTER_REVIEW_MODEL"):
                kw["gemini_review_model"] = kw["gemini_model"]
            if not get("OPENROUTER_MATH_MODEL"):
                kw.pop("gemini_math_model", None)
        allowed = get("SCHOLAROCR_ALLOWED_DIRS")
        if allowed:
            kw["allowed_dirs"] = [Path(p).expanduser() for p in allowed.split(os.pathsep) if p.strip()]
        settings = cls(**kw)
        settings.data_dir = settings.data_dir.expanduser()
        return settings
