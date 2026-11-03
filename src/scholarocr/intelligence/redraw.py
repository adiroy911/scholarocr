"""Optional vector redraw of a figure (opt-in: it costs one or two model calls per figure).

The redraw is a faithful vector copy of the drawing, not an interpretation: same strokes, shapes, proportions,
positions, arrows, hatching and labels; only the pen's wobble becomes clean lines. The model gets the figure's
crop (the cleaned one when it exists) and must use the crop's pixel coordinates as the SVG viewBox. The reply is
sanitised (``svg_sanitize``) and checked deterministically against the crop (``pipeline/svg_fidelity``); a redraw
that fails the check gets one retry with specific feedback, and one that fails twice is stored as "not faithful"
and never shown or exported in place of the crop.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import Settings
from ..schemas import Element
from .extractor import encode_image
from .gemini_client import GeminiClient, TextPart

REDRAW_PROMPT_VERSION = "redraw-2026-11-03.2"
MAX_REDRAW_ATTEMPTS = 2

REDRAW_BRIEF = """Copy this figure as ONE SVG, WITHOUT CHANGING A THING. It is a faithful vector copy, not a redesign.
- Coordinates: use the image's own pixels. The root is <svg viewBox="0 0 {w} {h}">; a point at pixel (x, y) in the
  image is at (x, y) in the SVG. Trace each stroke where it is: same start and end, same curve, same length.
- Reproduce exactly the strokes, shapes, proportions, positions, arrows, arrowheads, hatching and every label as
  drawn. Add nothing (no extra labels, legend, title, frame, grid or arrowheads), remove nothing, do not
  straighten, align, tidy, re-space or "improve" anything, and do not correct the content even if it looks
  wrong. The only change allowed: a wobbly pen line becomes a clean line along the same path.
- Labels: <text> at the same place and roughly the same size as written. Sub/superscripts with <tspan dy=...
  font-size=...>, Greek letters as Unicode (θ, Δ); e.g. T_A as T<tspan dy="4" font-size="70%">A</tspan>.
- Style: stroke="#111", fill="none" (fill only what is filled in the drawing), stroke-width about 2.
- Allowed elements only: svg, g, defs, marker, path, line, polyline, polygon, rect, circle, ellipse, text, tspan,
  title, desc; presentation attributes only (no style, class, script, event handlers, links, images,
  foreignObject or external references). Arrowheads: a <marker> in <defs>, marker-end="url(#id)".
The server rasterises your SVG and compares it with the image both ways (strokes missing, strokes added) and
checks every label; a copy that differs is refused. Reply with the SVG only."""


class FigureSvgResponse(BaseModel):
    svg: str = Field(description="The complete SVG document, starting with <svg and ending with </svg>.")
    notes: list[str] = Field(default_factory=list, description="Parts that could not be copied reliably.")


def redraw_brief(el: Element, size: tuple[int, int], feedback: list[str] | None = None) -> str:
    parts = [REDRAW_BRIEF.replace("{w}", str(size[0])).replace("{h}", str(size[1])),
             f"The image is {size[0]} x {size[1]} pixels."]
    fig = el.figure
    if fig and fig.labels:
        parts.append("Labels recorded for this figure (all must appear as <text>): " + ", ".join(fig.labels[:40]))
    if fig and fig.description:
        parts.append(f"Recorded description (for orientation only; copy the image, not this text): "
                     f"{fig.description}")
    if feedback:
        parts.append("Your previous SVG was REFUSED by the fidelity check:\n- " + "\n- ".join(feedback[:14])
                     + "\nCopy the image again, fixing exactly these places, and reply with the complete SVG only.")
    return "\n".join(parts)


def redraw_with_client(client: GeminiClient, settings: Settings, el: Element, crop, *, document_id: str,
                       model: str | None = None, feedback: list[str] | None = None) -> tuple[FigureSvgResponse, dict]:
    """One structured call to a key provider. Returns (response, call info with model and usage)."""
    resp, info = client.generate_structured(
        purpose="redraw_figure", model=model or settings.gemini_model,
        system="You copy figures from academic documents into faithful SVG. You never change, add or improve "
               "anything.",
        parts=[encode_image(crop), TextPart(redraw_brief(el, crop.size, feedback) + "\nReturn JSON only.")],
        schema=FigureSvgResponse, prompt_version=REDRAW_PROMPT_VERSION, document_id=document_id,
        page_number=el.page_number, media_resolution="MEDIA_RESOLUTION_HIGH",
    )
    return resp, {"model": info.model, "usage": dict(getattr(info, "usage", {}) or {})}
