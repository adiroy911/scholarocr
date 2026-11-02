"""Page-level checkpoints and content-based cache keys.

A page's IR is written atomically to pages/<NNNN>/page.json as soon as it is finished. A page is
reused (not re-processed) when its stored cache key equals the key computed for the current run:
the key covers the page content hash, route, models, prompt/schema/extraction versions, render
DPI and every option that affects output.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import EXTRACTION_VERSION, SCHEMA_VERSION
from ..config import Settings
from ..intelligence.prompts import PROMPT_VERSION
from ..schemas import ManifestEntry, Page, ProcessingOptions, Route
from ..storage.artifacts import ArtifactStore
from ..utils import sha256_bytes

VISUAL_ROUTES = {Route.VISUAL_GENERAL, Route.VISUAL_MATH, Route.VISUAL_HANDWRITING, Route.VISUAL_TABLE}


def cache_key(entry: ManifestEntry, route: Route, options: ProcessingOptions, settings: Settings,
              gemini_available: bool) -> str:
    models: dict[str, str | None] = {}
    if route in VISUAL_ROUTES:
        models["extract"] = settings.math_model if route == Route.VISUAL_MATH else settings.gemini_model
        if options.review:
            models["review"] = settings.gemini_review_model
    elif route == Route.EMBEDDED_TEXT:
        # Figure descriptions on text-layer pages use the primary model when Gemini is available.
        models["describe"] = settings.gemini_model if gemini_available else None
    payload = {
        "content": entry.content_hash,
        "route": route.value,
        "models": models,
        "prompt": PROMPT_VERSION if route in VISUAL_ROUTES or models.get("describe") else None,
        "schema": SCHEMA_VERSION,
        "extraction": EXTRACTION_VERSION,
        "dpi": settings.render_dpi,
        "options": options.fingerprint(),
    }
    return sha256_bytes(json.dumps(payload, sort_keys=True).encode())[:32]


def page_json_path(store: ArtifactStore, document_id: str, page_number: int) -> Path:
    return store.page_dir(document_id, page_number) / "page.json"


def load_page(store: ArtifactStore, document_id: str, page_number: int) -> Page | None:
    p = page_json_path(store, document_id, page_number)
    if not p.exists():
        return None
    return Page.model_validate_json(p.read_text(encoding="utf-8"))


def save_page(store: ArtifactStore, page: Page) -> None:
    store.write_json(page_json_path(store, page.document_id, page.page_number), page)
