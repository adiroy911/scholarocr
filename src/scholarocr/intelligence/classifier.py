"""Optional document classification. Uses already-extracted text when available (cheap) and
falls back to a few low-resolution page images otherwise."""

from __future__ import annotations

from PIL import Image

from ..config import Settings
from . import prompts
from .extractor import encode_image
from .gemini_client import GeminiClient, ImagePart, TextPart
from .responses import ClassificationResponse

MAX_TEXT_CHARS = 12000
MAX_IMAGES = 3


def classify(client: GeminiClient, settings: Settings, *, document_id: str, page_count: int, text_sample: str | None,
             page_images: list[Image.Image], context_hint: str | None) -> ClassificationResponse:
    parts: list[ImagePart | TextPart] = []
    use_images = not text_sample or len(text_sample.strip()) < 200
    if use_images:
        for img in page_images[:MAX_IMAGES]:
            small = img.copy()
            small.thumbnail((1200, 1200))
            parts.append(encode_image(small))
    sample = (text_sample or "")[:MAX_TEXT_CHARS] or None
    parts.append(TextPart(prompts.classify_prompt(context_hint, sample, page_count)))
    resp, _ = client.generate_structured(
        purpose="classify", model=settings.gemini_model, system=prompts.CLASSIFY_SYSTEM, parts=parts,
        schema=ClassificationResponse, prompt_version=prompts.PROMPT_VERSION, document_id=document_id,
        media_resolution="MEDIA_RESOLUTION_MEDIUM" if use_images else None,
    )
    return resp
