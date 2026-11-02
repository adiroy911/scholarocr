"""Export engine: renders the stored IR into JSON, Markdown, searchable PDF, DOCX, LaTeX, CSV, XLSX.

Exports read only the IR, the preserved sources and saved region crops. They never call
Gemini and never re-run OCR.
"""

from __future__ import annotations

from collections.abc import Callable

from ..exceptions import ExportError
from .base import FORMATS, ExportContext, ExportResult
from .docx import export_docx
from .json_export import export_json
from .latex import export_latex
from .markdown import document_to_markdown, export_markdown
from .searchable_pdf import export_searchable_pdf
from .spreadsheet import export_csv, export_xlsx

_EXPORTERS: dict[str, Callable[[ExportContext], ExportResult]] = {
    "json": export_json,
    "markdown": export_markdown,
    "searchable-pdf": export_searchable_pdf,
    "docx": export_docx,
    "latex": export_latex,
    "csv": export_csv,
    "xlsx": export_xlsx,
}


def export_document(ctx: ExportContext, fmt: str) -> ExportResult:
    """Generate one export format into ``ctx.out_dir``."""
    key = (fmt or "").strip().lower()
    exporter = _EXPORTERS.get(key)
    if exporter is None:
        raise ExportError(f"Unknown export format {fmt!r}. Supported: {', '.join(FORMATS)}",
                          details={"supported": list(FORMATS)})
    ctx = ctx.model_copy(update={"out_dir": ctx.out_dir.resolve(), "doc_dir": ctx.doc_dir.resolve()})
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    try:
        return exporter(ctx)
    except OSError as e:
        raise ExportError(f"{key} export failed: {e}") from e


__all__ = ["FORMATS", "ExportContext", "ExportError", "ExportResult", "document_to_markdown", "export_document"]
