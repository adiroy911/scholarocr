"""Remote-mode settings (``SCHOLAROCR_REMOTE_*`` environment variables).

Kept apart from ``scholarocr.config.Settings`` so the local server's behaviour cannot change. The
per-user ``Settings`` used by the service layer are derived in ``services.py``.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path
from typing import Literal, Mapping
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from ..config import _default_data_dir, load_dotenv

AuthMode = Literal["oauth", "none"]
REMOTE_PROVIDERS = ("gemini", "openrouter")
# Input types accepted remotely. DOCX is opt-in (zip parsing of untrusted uploads).
DEFAULT_ALLOWED_TYPES = ("application/pdf", "image/png", "image/jpeg", "image/webp", "image/tiff", "image/gif",
                         "image/bmp")
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


class RemoteConfig(BaseModel):
    public_url: str = "http://127.0.0.1:8000"  # origin ChatGPT reaches, e.g. https://ocr.example.com
    host: str = "127.0.0.1"
    port: int = 8000
    auth: AuthMode = "oauth"
    root_dir: Path = Field(default_factory=lambda: _default_data_dir() / "remote")
    secret: str = Field(default="", repr=False)  # encrypts stored keys, signs download links
    secret_is_ephemeral: bool = False

    # Billing: bring-your-own-key unless the operator explicitly opts in to paying.
    operator_key_mode: bool = False

    # Upload limits
    max_file_mb: float = 20.0
    max_files_per_call: int = 10
    max_call_mb: float = 50.0
    max_base64_mb: float = 3.0
    allow_base64: bool = True
    allow_docx: bool = False
    max_pages: int = 100
    user_quota_mb: float = 500.0
    download_timeout_s: float = 30.0
    download_allowed_hosts: list[str] = Field(default_factory=list)  # empty: any public https host

    # Abuse controls
    calls_per_minute: int = 30
    pages_per_day: int = 300
    ocr_calls_per_hour: int = 30

    # Retention
    retention_hours: float = 24.0  # uploaded documents and all derived artifacts
    key_retention_days: float = 30.0  # idle users: stored key, tokens and sandbox
    sweep_interval_s: float = 900.0
    download_link_ttl_s: int = 900

    # Transport
    stateless_http: bool = False
    json_response: bool = False
    allowed_origins: list[str] = Field(default_factory=list)
    widget_domain: str | None = None  # _meta.ui.domain; defaults to public_url origin
    domain_challenge: str | None = None  # served at /.well-known/openai-apps-challenge
    max_services: int = 64  # cached per-user ScholarOCR instances

    @field_validator("public_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        p = urlparse(v)
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("SCHOLAROCR_REMOTE_PUBLIC_URL must be an http(s) origin")
        return v

    @property
    def mcp_url(self) -> str:
        return f"{self.public_url}/mcp"

    @property
    def public_host(self) -> str:
        return urlparse(self.public_url).netloc

    @property
    def allowed_types(self) -> set[str]:
        return set(DEFAULT_ALLOWED_TYPES) | ({DOCX_TYPE} if self.allow_docx else set())

    @property
    def users_dir(self) -> Path:
        return self.root_dir / "users"

    @property
    def auth_db(self) -> Path:
        return self.root_dir / "auth.db"

    @property
    def max_request_body_bytes(self) -> int:
        # base64 inflates by 4/3; leave room for JSON-RPC framing.
        return max(4 * 1024 * 1024, int(self.max_base64_mb * 1024 * 1024 * 1.4) + 1024 * 1024)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "RemoteConfig":
        env = dict(os.environ if environ is None else environ)
        if environ is None:
            load_dotenv(Path.cwd() / ".env", env)

        def get(name: str) -> str | None:
            v = env.get(name)
            return v.strip() if v not in (None, "") else None

        kw: dict = {}
        simple = {
            "SCHOLAROCR_REMOTE_PUBLIC_URL": "public_url", "SCHOLAROCR_REMOTE_HOST": "host",
            "SCHOLAROCR_REMOTE_PORT": "port", "SCHOLAROCR_REMOTE_AUTH": "auth",
            "SCHOLAROCR_REMOTE_DATA_DIR": "root_dir", "SCHOLAROCR_REMOTE_MAX_FILE_MB": "max_file_mb",
            "SCHOLAROCR_REMOTE_MAX_FILES": "max_files_per_call", "SCHOLAROCR_REMOTE_MAX_CALL_MB": "max_call_mb",
            "SCHOLAROCR_REMOTE_MAX_BASE64_MB": "max_base64_mb", "SCHOLAROCR_REMOTE_MAX_PAGES": "max_pages",
            "SCHOLAROCR_REMOTE_USER_QUOTA_MB": "user_quota_mb",
            "SCHOLAROCR_REMOTE_DOWNLOAD_TIMEOUT_S": "download_timeout_s",
            "SCHOLAROCR_REMOTE_CALLS_PER_MINUTE": "calls_per_minute",
            "SCHOLAROCR_REMOTE_PAGES_PER_DAY": "pages_per_day",
            "SCHOLAROCR_REMOTE_OCR_CALLS_PER_HOUR": "ocr_calls_per_hour",
            "SCHOLAROCR_REMOTE_RETENTION_HOURS": "retention_hours",
            "SCHOLAROCR_REMOTE_KEY_RETENTION_DAYS": "key_retention_days",
            "SCHOLAROCR_REMOTE_SWEEP_INTERVAL_S": "sweep_interval_s",
            "SCHOLAROCR_REMOTE_DOWNLOAD_LINK_TTL_S": "download_link_ttl_s",
            "SCHOLAROCR_REMOTE_WIDGET_DOMAIN": "widget_domain",
            "SCHOLAROCR_REMOTE_DOMAIN_CHALLENGE": "domain_challenge",
            "SCHOLAROCR_REMOTE_MAX_SERVICES": "max_services",
        }
        for name, field in simple.items():
            if get(name) is not None:
                kw[field] = get(name)
        for name, field in (("SCHOLAROCR_REMOTE_OPERATOR_KEY_MODE", "operator_key_mode"),
                            ("SCHOLAROCR_REMOTE_ALLOW_BASE64", "allow_base64"),
                            ("SCHOLAROCR_REMOTE_ALLOW_DOCX", "allow_docx"),
                            ("SCHOLAROCR_REMOTE_STATELESS", "stateless_http"),
                            ("SCHOLAROCR_REMOTE_JSON_RESPONSE", "json_response")):
            if get(name) is not None:
                kw[field] = _truthy(get(name))
        for name, field in (("SCHOLAROCR_REMOTE_DOWNLOAD_HOSTS", "download_allowed_hosts"),
                            ("SCHOLAROCR_REMOTE_ALLOWED_ORIGINS", "allowed_origins")):
            if get(name):
                kw[field] = [h.strip() for h in get(name).split(",") if h.strip()]
        secret = get("SCHOLAROCR_REMOTE_SECRET")
        if secret:
            if len(secret) < 32:
                raise ValueError("SCHOLAROCR_REMOTE_SECRET must be at least 32 characters")
            kw["secret"] = secret
        else:
            kw["secret"] = secrets.token_urlsafe(48)
            kw["secret_is_ephemeral"] = True
        cfg = cls(**kw)
        cfg.root_dir = Path(cfg.root_dir).expanduser()
        return cfg

    def warn_insecure(self) -> list[str]:
        out = []
        if self.secret_is_ephemeral:
            out.append("SCHOLAROCR_REMOTE_SECRET is not set: stored keys and tokens become unreadable on restart.")
        if self.auth == "none":
            out.append("SCHOLAROCR_REMOTE_AUTH=none: every caller shares ONE sandbox. Local testing only.")
        if self.operator_key_mode:
            out.append("SCHOLAROCR_REMOTE_OPERATOR_KEY_MODE=1: users without their own key spend YOUR model quota.")
        if not self.public_url.startswith("https://") and urlparse(self.public_url).hostname not in (
                "127.0.0.1", "localhost", "::1"):
            out.append("public URL is not https; ChatGPT requires a public HTTPS endpoint.")
        return out


def print_warnings(cfg: RemoteConfig) -> None:
    for w in cfg.warn_insecure():
        print(f"warning: {w}", file=sys.stderr)
