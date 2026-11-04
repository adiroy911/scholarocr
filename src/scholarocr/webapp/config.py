"""Web app configuration (process level) and user preferences (persisted in ``prefs.json``)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..config import _default_data_dir
from ..exceptions import InputError

DEFAULT_PORT = 8765
DEFAULT_AGY_MODEL = "gemini-3.8-flash-medium"
ENGINE_CHOICES = ("auto", "antigravity", "gemini", "openrouter")
MAX_SAVED_INSTRUCTIONS = 4000
MAX_MESSAGE_CHARS = 8000
DEFAULT_READ_CONCURRENCY = 3
MAX_READ_CONCURRENCY = 6


def default_app_dir(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    explicit = env.get("SCHOLAROCR_APP_DATA_DIR")
    return Path(explicit).expanduser() if explicit else _default_data_dir() / "app"


class AppConfig(BaseModel):
    """Process-level settings. The bind address is always loopback; there is no option to change it."""

    data_dir: Path = Field(default_factory=default_app_dir)
    port: int = DEFAULT_PORT
    agy_path: str | None = None  # None: look up ``agy`` on PATH
    page_timeout_s: float = 300.0  # one agy call for one page
    chat_timeout_s: float = 240.0
    redraw_timeout_s: float = 600.0  # one agy call copying one figure as SVG (a detailed drawing takes minutes)
    review_timeout_s: float = 300.0  # one agy call checking a page's doubtful elements against their crops
    max_retries: int = 2  # re-prompts after a rejected page submission
    upload_ttl_s: float = 86400.0  # staged uploads not used by a message are removed after this
    rate_limit_backoff_s: float = 20.0  # first pause after agy reports throttling (doubles each time)
    rate_limit_retries: int = 3  # a page throttled more often than this stays unread
    use_reader_agent: bool = True  # see agy.py: a minimal workspace agent for page reads

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "agy-runs"

    @property
    def engine_data_dir(self) -> Path:
        return self.data_dir / "engine"  # the ScholarOCR service's own data_dir (db, documents, exports)

    @property
    def prefs_path(self) -> Path:
        return self.data_dir / "prefs.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.uploads_dir, self.runs_dir, self.engine_data_dir):
            d.mkdir(parents=True, exist_ok=True)


class Prefs(BaseModel):
    """What the user sets in Settings. Keys are not stored here (see keys.py)."""

    saved_instructions: str = ""
    engine: Literal["auto", "antigravity", "gemini", "openrouter"] = "auto"
    agy_model: str = DEFAULT_AGY_MODEL
    gemini_model: str | None = None  # None: the package default (config.DEFAULT_MODEL)
    openrouter_model: str | None = None
    max_file_mb: float = Field(default=50.0, gt=0, le=500)
    max_files: int = Field(default=30, ge=1, le=200)
    max_pages: int = Field(default=60, ge=1, le=1000)
    theme: Literal["system", "light", "dark"] = "system"
    # Text shown and exported: auto = cleaned for handwritten documents, as written for printed ones.
    text_variant: Literal["auto", "clean", "faithful"] = "auto"
    figure_variant: Literal["clean", "original", "redrawn"] = "clean"  # figure images in exports
    # Antigravity: pages read at the same time (one agy process each, shared by every document being read).
    read_concurrency: int = Field(default=DEFAULT_READ_CONCURRENCY, ge=1, le=MAX_READ_CONCURRENCY)
    # Antigravity reasoning effort (agy --effort): page transcription, and follow-up chat + Redraw.
    read_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    chat_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Antigravity: after a page is accepted, its doubtful elements get a second look against their crops.
    review_flagged: bool = False

    @field_validator("saved_instructions")
    @classmethod
    def _cap(cls, v: str) -> str:
        v = (v or "").replace("\x00", "").strip()
        if len(v) > MAX_SAVED_INSTRUCTIONS:
            raise ValueError(f"saved instructions are limited to {MAX_SAVED_INSTRUCTIONS} characters")
        return v

    @field_validator("agy_model", "gemini_model", "openrouter_model")
    @classmethod
    def _model_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > 120 or not all(c.isalnum() or c in "-_.:/" for c in v):
            raise ValueError("model ids contain only letters, digits and - _ . : /")
        return v


class PrefsStore:
    """``prefs.json`` with atomic writes."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> Prefs:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return Prefs.model_validate(data)
        except (OSError, ValueError, ValidationError):
            return Prefs()

    def update(self, changes: dict[str, Any]) -> Prefs:
        with self._lock:
            current = self.load().model_dump()
            unknown = sorted(set(changes) - set(current))
            if unknown:
                raise InputError(f"unknown setting(s): {', '.join(unknown)}")
            current.update(changes)
            try:
                prefs = Prefs.model_validate(current)
            except ValidationError as e:
                first = e.errors(include_url=False)[0]
                field = ".".join(str(p) for p in first.get("loc", ())) or "settings"
                raise InputError(f"{field}: {first.get('msg')}") from None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(prefs.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            return prefs
