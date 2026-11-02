"""Targeted description of figures/diagrams found on otherwise deterministic (text-layer) pages.

Only the crop of the figure is sent. The description is written from the source image and is
stored alongside the preserved crop; it never replaces the original figure."""

from __future__ import annotations

from PIL import Image

from ..config import Settings
from ..schemas import Element, FigureData, Revision
from ..utils import now_iso
from . import prompts
from .extractor import encode_image
from .gemini_client import GeminiClient, TextPart
from .responses import XFigure

DESCRIBE_SYSTEM = prompts.EXTRACTION_SYSTEM + """
You are given a crop containing a single figure, diagram, graph or image from an academic document.
Describe only what is visible: kind, all text labels, arrows/relationships, axes and units. Do not
interpret beyond the image and do not invent values."""

MAX_FIGURES_PER_PAGE = 4


def describe(client: GeminiClient, settings: Settings, el: Element, crop: Image.Image, *, document_id: str) -> None:
    known = el.figure.labels if el.figure else []
    prompt = "Describe this figure. Return JSON only."
    if known:
        prompt += f"\nText labels already found in the PDF text layer near it: {known}"
    resp, info = client.generate_structured(
        purpose="describe_figure", model=settings.gemini_model, system=DESCRIBE_SYSTEM,
        parts=[encode_image(crop), TextPart(prompt)], schema=XFigure, prompt_version=prompts.PROMPT_VERSION,
        document_id=document_id, page_number=el.page_number, media_resolution="MEDIA_RESOLUTION_MEDIUM",
    )
    previous = el.figure.model_dump() if el.figure else None
    labels = list(dict.fromkeys((known or []) + resp.labels))
    el.figure = FigureData(description=resp.description, labels=labels, relationships=resp.relationships,
                           figure_label=(el.figure.figure_label if el.figure else None) or resp.figure_label,
                           kind=resp.kind)
    el.revisions.append(Revision(at=now_iso(), by=f"describe:{info.model}", field="figure", previous=previous))
    el.uncertainty_reasons = [r for r in el.uncertainty_reasons if "not described" not in r]
    # The description is auxiliary (the preserved crop is authoritative), so the label is unchanged.
    el.uncertainty_reasons.append("figure description generated from crop (model)")
