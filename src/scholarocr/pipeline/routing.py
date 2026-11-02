"""Per-page routing: decide whether a page needs Gemini at all, and with which prompt profile."""

from __future__ import annotations

from ..schemas import DocumentType, ManifestEntry, ProcessingOptions, Route

TEXT_MIN_CHARS = 40
TEXT_MIN_QUALITY = 0.97
MAX_IMAGE_COVERAGE_FOR_TEXT = 0.25

HANDWRITING_TYPES = {DocumentType.HANDWRITTEN_NOTES}
MATH_HINT_WORDS = ("math", "physics", "chemistry", "calculus", "algebra", "equation", "jee", "olympiad", "statistics")


def decide_route(entry: ManifestEntry, options: ProcessingOptions, media_type: str) -> tuple[Route, str]:
    """Return (route, human-readable reason)."""
    p = entry.profile
    if media_type.endswith("wordprocessingml.document"):
        return Route.DOCX_TEXT, "DOCX: deterministic text extraction"
    if p.is_blank:
        return Route.BLANK, "blank page"
    if not options.force_visual and p.has_text_layer and not p.is_scanned:
        reliable = (p.text_chars >= TEXT_MIN_CHARS and p.text_quality >= TEXT_MIN_QUALITY
                    and p.image_coverage <= MAX_IMAGE_COVERAGE_FOR_TEXT and p.math_font_signals == 0)
        if reliable:
            return Route.EMBEDDED_TEXT, "reliable embedded text layer"
    reason_bits = []
    if p.is_scanned or not p.has_text_layer:
        reason_bits.append("scanned/no text layer")
    elif p.math_font_signals:
        reason_bits.append(f"{p.math_font_signals} math-font spans (text layer unreliable for math)")
    elif p.image_coverage > MAX_IMAGE_COVERAGE_FOR_TEXT:
        reason_bits.append(f"images cover {p.image_coverage:.0%} of the page")
    elif p.text_quality < TEXT_MIN_QUALITY:
        reason_bits.append("garbled text layer")
    elif options.force_visual:
        reason_bits.append("force_visual requested")
    else:
        reason_bits.append("too little embedded text")
    hint = (options.context_hint or "").lower()
    if options.document_type in HANDWRITING_TYPES or "handwrit" in hint:
        return Route.VISUAL_HANDWRITING, "; ".join(reason_bits + ["handwriting profile (hint)"])
    if p.math_font_signals or any(w in hint for w in MATH_HINT_WORDS):
        return Route.VISUAL_MATH, "; ".join(reason_bits + ["math profile"])
    return Route.VISUAL_GENERAL, "; ".join(reason_bits)
