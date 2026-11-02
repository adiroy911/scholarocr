"""ScholarOCR MCP server (official MCP Python SDK 2.x, ``MCPServer``).

Agent-neutral: any MCP client (Claude Code, Gemini CLI, others) can use it. Transport is stdio by
default (works everywhere, including Windows); ``--transport streamable-http`` serves one shared
instance on localhost for several agents.

Run:  scholarocr-mcp            (stdio)
      scholarocr-mcp --transport streamable-http --port 8765
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .. import __version__
from ..config import APP_NAME
from ..exceptions import ScholarOCRError
from ..schemas import DocumentType, ElementType, ProcessingOptions
from ..service import ScholarOCR

INSTRUCTIONS = f"""{APP_NAME} extracts structured, source-traceable content from academic documents
(scanned PDFs, images, handwritten notes, exam papers, textbooks, research papers).
Typical flow: list_documents (reuse existing extractions) -> ocr_document -> get_document_status
(if a job id was returned) -> get_document_content / get_page_content -> get_source_region to look at
the original image of any uncertain element -> verify_document before relying on uncertain content ->
export_document only when the user wants a file. Element uncertainty labels (low/medium/high/
unresolved) are ordinal judgements, not probabilities; "unresolved" content was NOT guessed."""

DocTypeLit = Literal[tuple(t.value for t in DocumentType)]  # type: ignore[valid-type]
ElementTypeLit = Literal[tuple(t.value for t in ElementType)]  # type: ignore[valid-type]
FormatLit = Literal["json", "markdown", "searchable-pdf", "docx", "latex", "csv", "xlsx"]
ScopeLit = Literal["completeness", "duplicates", "page_order", "content", "reading_order", "math", "tables",
                   "structure", "uncertainty", "traceability", "exports"]

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ScholarOCRError as e:
        raise ToolError(json.dumps(e.to_dict(), ensure_ascii=False)) from None
    except ValueError as e:  # validation of enum-like inputs inside the service
        raise ToolError(json.dumps({"error": "invalid_input", "message": str(e)})) from None


def build_server(service: ScholarOCR | None = None) -> MCPServer:
    state: dict[str, ScholarOCR] = {}

    def svc() -> ScholarOCR:
        if service is not None:
            return service
        if "svc" not in state:
            state["svc"] = _call(ScholarOCR)
        return state["svc"]

    mcp = MCPServer("scholarocr", title=APP_NAME, instructions=INSTRUCTIONS, version=__version__)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                          open_world_hint=True))
    def ocr_document(
        file_path: Annotated[str | list[str], Field(description="Path to a PDF/image/DOCX, a folder, or an ordered "
                                                    "list of paths forming ONE document (e.g. page photos).")],
        document_type: Annotated[DocTypeLit | None, Field(description="Optional hint, e.g. exam_paper, "
                                                          "handwritten_notes, research_paper.")] = None,
        language_hint: Annotated[str | None, Field(description="e.g. 'en', 'hi, en'")] = None,
        context_hint: Annotated[str | None, Field(description="Free-text context, e.g. 'Class 11 chemistry exam'. "
                                                  "Treated as a hint, not fact.")] = None,
        pages: Annotated[str | None, Field(description="Page range like '1-5,8'. Default: all.")] = None,
        detail: Annotated[Literal["standard", "high"], Field(description="'high' reviews every equation/table "
                                                             "with a second model pass (more cost).")] = "standard",
        force_visual: Annotated[bool, Field(description="Ignore embedded PDF text; always run visual OCR.")] = False,
        auto_crop: Annotated[bool, Field(description="Crop screenshots/photos to the detected document region "
                                         "(drops app/screen chrome). Default true.")] = True,
        review: Annotated[bool, Field(description="Targeted second-pass review of flagged regions.")] = True,
        reprocess: Annotated[bool, Field(description="Ignore cached results for these pages.")] = False,
        wait: Annotated[bool | None, Field(description="True: block until done. False: return a job id at once. "
                                           "Default: wait only for small page selections.")] = None,
    ) -> dict[str, Any]:
        """Extract a document (or selected pages) into structured, source-traceable content.

        Re-running on an already processed document reuses cached pages (no new model calls).
        Returns document_id plus either a completed summary or a job_id to poll with get_document_status."""
        opts = ProcessingOptions(document_type=document_type, language_hint=language_hint, context_hint=context_hint,
                                 pages=pages, detail=detail, force_visual=force_visual, auto_crop=auto_crop, review=review,
                                 reprocess=reprocess)
        return _call(svc().process, file_path, opts, wait=wait)

    @mcp.tool(annotations=READ_ONLY)
    def get_document_status(
        job_id: Annotated[str | None, Field(description="Job id returned by ocr_document.")] = None,
        document_id: Annotated[str | None, Field(description="Alternatively, a document id.")] = None,
    ) -> dict[str, Any]:
        """Progress of a processing job (status, completed/total pages, stage, errors), or a document summary
        (page statuses, outline, uncertainty counts, latest job)."""
        if job_id:
            return _call(svc().job_status, job_id)
        if document_id:
            return _call(svc().document_summary, document_id)
        raise ToolError(json.dumps({"error": "invalid_input", "message": "Provide job_id or document_id"}))

    @mcp.tool(annotations=READ_ONLY)
    def get_document_content(
        document_id: str,
        pages: Annotated[str | None, Field(description="Page range like '3-7'. Default: all.")] = None,
        element_types: Annotated[list[ElementTypeLit] | None, Field(description="Only these element types, "
                                                                    "e.g. ['equation','table'].")] = None,
        format: Annotated[Literal["markdown", "json", "text"], Field(description="markdown (compact, with element "
                                                                     "id anchors), json (full IR), or text.")] = "markdown",
        max_chars: Annotated[int, Field(ge=1000, le=200000)] = 30000,
        cursor: Annotated[int | None, Field(description="next_cursor from a previous call.")] = None,
    ) -> dict[str, Any]:
        """Retrieve extracted content, paginated by page. Markdown includes <!-- el:ID --> anchors that can be
        passed to get_source_region. Unreadable regions appear as ⟦unclear: …⟧."""
        return _call(svc().get_content, document_id, pages=pages, element_types=element_types, fmt=format,
                     max_chars=max_chars, cursor=cursor)

    @mcp.tool(annotations=READ_ONLY)
    def get_page_content(document_id: str, page_number: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """Full structured IR of one page: elements with type, content, LaTeX, tables, bounding boxes,
        uncertainty, reading order, source references, quality metrics and preprocessing applied."""
        return _call(svc().get_page, document_id, page_number)

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def get_source_region(
        document_id: str,
        element_id: Annotated[str | None, Field(description="Element id from extracted content.")] = None,
        page_number: Annotated[int | None, Field(ge=1, description="Page, if no element_id.")] = None,
        bbox: Annotated[list[float] | None, Field(description="[x0,y0,x1,y1] in PDF points with page_number.")] = None,
    ) -> list:
        """Return the ORIGINAL source image for an element (its cropped region) or a page/region, plus metadata.
        Use it to check uncertain equations, tables, handwriting or figures against the source."""
        png, meta = _call(svc().get_source_region, document_id, element_id=element_id, page_number=page_number,
                          bbox=bbox)
        return [Image(data=png, format="png"), meta]

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                          open_world_hint=True))
    def verify_document(
        document_id: str,
        scope: Annotated[list[ScopeLit] | None, Field(description="Checks to run. Default: all.")] = None,
        review: Annotated[bool, Field(description="Also send not-yet-reviewed flagged elements to the review "
                                      "model (costs model calls; updates the extraction).")] = False,
    ) -> dict[str, Any]:
        """Quality checks on an existing extraction: missing/duplicate/failed pages, page-number gaps, empty or
        short extractions, reading order, invalid LaTeX, table structure, question numbering, unresolved regions,
        traceability, stale exports. Returns findings with severity and element/page references."""
        report = _call(svc().verify, document_id, scope=scope, review=review)
        d = report.model_dump(mode="json")
        d["counts"] = report.counts()
        if len(d["findings"]) > 200:
            d["findings_truncated"] = len(d["findings"]) - 200
            d["findings"] = d["findings"][:200]
        return d

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                          open_world_hint=False))
    def export_document(
        document_id: str,
        format: FormatLit,
        options: Annotated[dict[str, Any] | None, Field(description="Format options, e.g. {'main_font': 'DejaVu "
                                                        "Serif'} for latex, {'include_furniture': true} for markdown.")] = None,
    ) -> dict[str, Any]:
        """Generate a file from the stored extraction (no OCR is re-run). Returns file paths, warnings and
        post-generation verification (e.g. searchable-PDF text recall, LaTeX compile status)."""
        return _call(svc().export, document_id, format, options=options)

    @mcp.tool(annotations=READ_ONLY)
    def list_documents(
        query: Annotated[str | None, Field(description="Substring of filename or title.")] = None,
        status: str | None = None,
        document_type: DocTypeLit | None = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """List documents already ingested/processed (check here before re-processing a file)."""
        return _call(svc().list_documents, query=query, status=status, document_type=document_type, limit=limit,
                     offset=offset)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                          open_world_hint=True))
    def classify_document(
        document_id: str,
        context_hint: Annotated[str | None, Field(description="Optional context; used as a prior, may be wrong.")] = None,
        pages: Annotated[str | None, Field(description="Pages to consider (max 10). Default: first pages.")] = None,
    ) -> dict[str, Any]:
        """Classify the document (exam paper, textbook, handwritten notes, research paper, ...) with subjects,
        level, topics, an ordinal certainty and whether the hint is consistent with the evidence."""
        return _call(svc().classify, document_id, context_hint=context_hint, pages=pages)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                                          open_world_hint=False))
    def delete_document(
        document_id: str,
        mode: Annotated[Literal["generated", "all"], Field(description="'generated': delete extraction, crops and "
                                                           "exports (keeps preserved source copy). 'all': also the "
                                                           "preserved source copy and record.")] = "generated",
        confirm: Annotated[bool, Field(description="Required for mode='all'. Only set when the user explicitly "
                                       "asked to delete everything.")] = False,
    ) -> dict[str, Any]:
        """Delete stored artifacts on explicit user instruction. The user's own original file is never touched."""
        return _call(svc().delete, document_id, mode=mode, confirm=confirm)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                          open_world_hint=True))
    def manage_job(job_id: str, action: Literal["cancel", "resume"]) -> dict[str, Any]:
        """Cancel a running job, or resume a cancelled/failed/interrupted one (finished pages are reused)."""
        if action == "cancel":
            return _call(svc().cancel_job, job_id)
        return _call(svc().resume_job, job_id, wait=False)

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="scholarocr-mcp", description=f"{APP_NAME} MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = build_server()
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print("warning: serving beyond localhost exposes document access to the network", file=sys.stderr)
        server.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
