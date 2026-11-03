"""JSON export: the full IR document, re-parsed after writing as a round-trip check.

Both text variants are in the IR (``content`` exactly as written, ``clean_content`` normalised), and figure image
paths (``source.region_image``, ``figure.image_clean``, ``figure.svg``) are relative to the document directory."""

from __future__ import annotations

from pydantic import ValidationError

from ..schemas import Document
from .base import ExportContext, ExportResult, resolve_text_variant, write_text


def export_json(ctx: ExportContext) -> ExportResult:
    out = ctx.out_dir / "document.json"
    write_text(out, ctx.document.model_dump_json(indent=2))
    expected = sum(len(p.elements) for p in ctx.document.pages)
    warnings: list[str] = []
    try:
        parsed = Document.model_validate_json(out.read_text(encoding="utf-8"))
        found: int | None = sum(len(p.elements) for p in parsed.pages)
        valid = True
    except ValidationError as e:
        found, valid = None, False
        warnings.append(f"Written JSON does not validate against the Document schema: {e.error_count()} error(s)")
    if valid and found != expected:
        warnings.append(f"Element count mismatch after round trip: {found} != {expected}")
    verification = {
        "schema_valid": valid,
        "elements_expected": expected,
        "elements_found": found,
        "pages": len(ctx.document.pages),
        # JSON keeps both variants; this is the one other formats would use by default (or as requested)
        "text_variant": resolve_text_variant(ctx.document, ctx.option("text_variant")),
        "figure_images": sum(1 for p in ctx.document.pages for e in p.elements if e.source.region_image
                             and e.figure is not None),
    }
    return ExportResult(format="json", primary_file=str(out), files=[str(out)], warnings=warnings,
                        verification=verification)
