"""ScholarOCR MCP server (official MCP Python SDK 2.x, ``MCPServer``).

Agent-neutral: any MCP client (Antigravity CLI, Claude Code, others) can use it. Transport is stdio by
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

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import TextContent, ToolAnnotations
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
export_document only when the user wants a file.

HOST MODE (the default when the server has no model API key; ocr_document reports "mode"): the server does
all deterministic work and YOU are the vision model. Pages with a reliable PDF text layer are done at once.
Every other page is listed in "awaiting_transcription". Loop, one page per call:
  get_page_image(document_id)  -> one page image + rules + the exact element JSON shape
  submit_page_content(document_id, page_number, elements=[...]) -> validated and stored
If submit_page_content is rejected, fix every listed problem and resubmit the whole page. Never re-request
an image you already transcribed. Afterwards all other tools work as usual. Optional second pass: for
elements in review_suggested, get_source_region(element_id) then submit_review(...).

Element uncertainty labels (low/medium/high/unresolved) are ordinal judgements, not probabilities;
"unresolved" content was NOT guessed. Transcribe exactly what is visible; never fill in unreadable text."""

DocTypeLit = Literal[tuple(t.value for t in DocumentType)]  # type: ignore[valid-type]
ElementTypeLit = Literal[tuple(t.value for t in ElementType)]  # type: ignore[valid-type]
FormatLit = Literal["json", "markdown", "searchable-pdf", "layout-pdf", "docx", "latex", "csv", "xlsx"]
TextVariantLit = Literal["clean", "faithful", "auto"]
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


def client_name(ctx: Context | None) -> str | None:
    """The MCP client's self-reported name (e.g. "antigravity"), recorded as the transcriber in host mode."""
    try:
        info = ctx.session.client_params.client_info if ctx is not None and ctx.session.client_params else None
        name = (info.name if info else None) or None
    except (AttributeError, ValueError):
        return None
    if not name:
        return None
    return "".join(c for c in str(name) if c.isalnum() or c in "-_.")[:40] or None


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
        review: Annotated[bool, Field(description="Targeted second-pass review of flagged regions (host mode: "
                                      "suggested to you after each submission).")] = True,
        reprocess: Annotated[bool, Field(description="Ignore cached results for these pages.")] = False,
        wait: Annotated[bool | None, Field(description="True: block until done. False: return a job id at once. "
                                           "Default: wait only for small page selections.")] = None,
    ) -> dict[str, Any]:
        """Extract a document (or selected pages) into structured, source-traceable content.

        Returns document_id, "mode" and either a summary or a job_id to poll with get_document_status.
        mode "host" (no server key): text-layer pages are done; pages listed in awaiting_transcription need
        YOUR vision via get_page_image -> submit_page_content. mode "gemini"/"openrouter": the server's
        model reads them. Re-running reuses cached pages."""
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
        text_variant: Annotated[TextVariantLit | None, Field(description=(
            "clean: casing, punctuation and spacing normalised, slips of the pen fixed (same words, numbers and "
            "math); faithful: exactly as written. Default: clean for handwritten documents, faithful for "
            "printed ones. format=json always carries both (content, clean_content)."))] = None,
    ) -> dict[str, Any]:
        """Retrieve extracted content, paginated by page. Markdown includes <!-- el:ID --> anchors that can be
        passed to get_source_region. Unreadable regions appear as ⟦unclear: …⟧."""
        return _call(svc().get_content, document_id, pages=pages, element_types=element_types, fmt=format,
                     max_chars=max_chars, cursor=cursor, text_variant=text_variant)

    @mcp.tool(annotations=READ_ONLY)
    def get_page_content(
        document_id: str,
        page_number: Annotated[int, Field(ge=1)],
        compact: Annotated[bool, Field(description="Only id, type, role, content, latex, uncertainty (+reasons), "
                                       "question and bbox per element: a fraction of the full size.")] = False,
        fields: Annotated[list[str] | None, Field(description="Keep only these element fields, e.g. "
                                                  "['type','latex','uncertainty']; element_id is always kept.")] = None,
    ) -> dict[str, Any]:
        """Structured IR of one page: elements with type, content, LaTeX, tables, bounding boxes, uncertainty,
        reading order, source references, quality metrics and preprocessing. The full record is large; use
        compact=true or fields=[...] unless you need everything."""
        return _call(svc().get_page, document_id, page_number, compact=compact, fields=fields)

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

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def get_page_image(
        document_id: str,
        page_number: Annotated[int | None, Field(ge=1, description="Page to transcribe. Default: the next page "
                                                 "awaiting transcription.")] = None,
        instructions: Annotated[Literal["auto", "full", "short"], Field(
            description="'full': rules + element JSON examples; 'short': a reminder; 'auto': full until the "
                        "first page of this document is submitted.")] = "auto",
    ) -> list:
        """HOST MODE: return ONE page image that needs your vision, with transcription rules and the exact
        element shape. Transcribe it, then call submit_page_content. Boxes are box_2d = [ymin, xmin, ymax,
        xmax] in 0-1000 of THIS image. Call it once per page; do not re-request pages you already did."""
        data, mime, info = _call(svc().host_page_image, document_id, page_number, instructions=instructions)
        text = info.pop("instructions", None)
        meta = json.dumps(info, ensure_ascii=False)
        if data is None:
            return [TextContent(type="text", text=meta)]
        return [Image(data=data, format=mime.split("/", 1)[1]),
                TextContent(type="text", text=f"{text}\n\nPage info: {meta}")]

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                          open_world_hint=False))
    def submit_page_content(
        document_id: str,
        page_number: Annotated[int, Field(ge=1)],
        # Typed loosely on purpose: a malformed list (e.g. [0, 1, 2]) or a JSON string must reach
        # validate_submission, which answers with path/problem/fix instead of a raw pydantic error.
        elements: Annotated[list[Any] | str, Field(description=(
            "Elements in reading order. Each: {type, text, latex?, role?, level?, items?, table?, figure?, "
            "question?, box_2d:[ymin,xmin,ymax,xmax] 0-1000, uncertainty (doubt): low|medium|high|unresolved, "
            "uncertainty_reasons?, handwritten?, language?, caption_of?, parent_index?}. "
            "See get_page_image(instructions='full') for examples."))],
        page_language: Annotated[str | None, Field(description="BCP-47 code, e.g. 'en'.")] = None,
        page_label: Annotated[str | None, Field(description="Printed page number, if visible.")] = None,
        page_doubt: Annotated[str | None, Field(description="Overall DOUBT about the page (not quality): low "
                                                "for a clean page | medium | high | unresolved.")] = None,
        rotation_detected: Annotated[int, Field(description="0, or 90/180/270 if the IMAGE is rotated clockwise "
                                                "(the server re-orients it and asks you again).")] = 0,
        notes: Annotated[list[str] | None, Field(description="Problems such as cut-off text.")] = None,
        legibility: Annotated[str | None, Field(description="Deprecated old name of page_doubt.")] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """HOST MODE: store your transcription of one page (from get_page_image). Validated strictly against
        the document schema; on rejection you get every problem with its path and a fix, nothing is stored,
        and you resubmit the complete page. On success: element/uncertainty counts, review_suggested and the
        next page waiting."""
        payload: dict[str, Any] = {"elements": elements, "page_language": page_language, "page_label": page_label,
                                   "rotation_detected": rotation_detected, "notes": notes or []}
        if page_doubt is not None:
            payload["page_doubt"] = page_doubt
        if legibility is not None:
            payload["legibility"] = legibility
        return _call(svc().submit_page_content, document_id, page_number, payload, transcriber=client_name(ctx))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                          open_world_hint=False))
    def submit_review(
        document_id: str,
        element_id: str,
        verdict: Annotated[Literal["confirmed", "corrected", "unreadable"], Field(
            description="After comparing the element with its crop from get_source_region.")],
        text: Annotated[str, Field(description="Corrected text (verdict 'corrected').")] = "",
        latex: Annotated[str | None, Field(description="Corrected LaTeX.")] = None,
        table: Annotated[dict[str, Any] | None, Field(description="Corrected table {rows, header_rows}.")] = None,
        figure: Annotated[dict[str, Any] | None, Field(description="For figures/diagrams: {description, labels, "
                                                       "relationships, kind}.")] = None,
        uncertainty: Annotated[str, Field(description="Your judgement after the check: low|medium|high|"
                                          "unresolved.")] = "low",
        notes: list[str] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Second pass on one element: look at get_source_region(element_id) first, then record your verdict.
        Replaced values are kept in the element's revisions. Use it for review_suggested / review_candidates
        elements, and to describe figures on text-layer pages."""
        payload: dict[str, Any] = {"verdict": verdict, "text": text, "uncertainty": uncertainty, "notes": notes or []}
        for k, v in (("latex", latex), ("table", table), ("figure", figure)):
            if v is not None:
                payload[k] = v
        return _call(svc().submit_review, document_id, element_id, payload, transcriber=client_name(ctx))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                          open_world_hint=True))
    def verify_document(
        document_id: str,
        scope: Annotated[list[ScopeLit] | None, Field(description="Checks to run. Default: all.")] = None,
        review: Annotated[bool, Field(description="Key mode: send not-yet-reviewed flagged elements to the review "
                                      "model (costs model calls). Host mode: return review_candidates for you to "
                                      "re-check with get_source_region + submit_review.")] = False,
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
        text_variant: Annotated[TextVariantLit | None, Field(description=(
            "clean (normalised casing/punctuation) or faithful (exactly as written). Default: clean for "
            "handwritten documents, faithful for printed ones. JSON keeps both."))] = None,
        figure_variant: Annotated[Literal["clean", "original", "redrawn"] | None, Field(description=(
            "Figure images: clean (default; paper whitened, ruled lines removed), original (the crop as "
            "scanned) or redrawn (the SVG from redraw_figure, where one exists).") )] = None,
    ) -> dict[str, Any]:
        """Generate a file from the stored extraction (no OCR is re-run). Returns file paths, warnings and
        post-generation verification (e.g. searchable-PDF text recall, LaTeX compile status)."""
        opts = dict(options or {})
        if text_variant is not None:
            opts["text_variant"] = text_variant
        if figure_variant is not None:
            opts["figure_variant"] = figure_variant
        return _call(svc().export, document_id, format, options=opts)

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    def redraw_figure(document_id: str, element_id: Annotated[str, Field(description=(
            "Id of a figure, diagram or image element."))]) -> list:
        """OPTIONAL, costs model output: copy one figure as clean vector SVG WITHOUT CHANGING ANYTHING. Returns the
        figure's cleaned crop and the strict instructions (viewBox = the crop's pixels). Trace it faithfully and
        store it with submit_figure_svg, which checks it against the crop. The crop is never replaced. Only do
        this when the user asks for it."""
        data, info = _call(svc().figure_redraw_request, document_id, element_id)
        text = info.pop("instructions")
        return [Image(data=data, format="png"),
                TextContent(type="text", text=text + "\n\nFigure info: " + json.dumps(info, ensure_ascii=False))]

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                          open_world_hint=False))
    def submit_figure_svg(
        document_id: str,
        element_id: str,
        svg: Annotated[str, Field(description="The complete SVG document (<svg viewBox=...>...</svg>).")],
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        """Store a redrawn SVG for a figure (after redraw_figure). It is sanitised strictly (scripts, styles,
        event handlers, links, images, foreignObject and external references removed), then rasterised and
        compared with the crop both ways plus every label. accepted=false with retry_allowed=true: nothing was
        stored; fix exactly the places in feedback and submit once more. A second failure is stored but marked
        not faithful and is never used instead of the crop. Exports use a faithful SVG with
        figure_variant='redrawn'."""
        return _call(svc().submit_figure_svg, document_id, element_id, svg, by=client_name(ctx) or "host")

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
        level, topics, an ordinal certainty and whether the hint is consistent with the evidence. Needs a
        server-side model key; in host mode it returns model_unavailable (classify from the content yourself)."""
        return _call(svc().classify, document_id, context_hint=context_hint, pages=pages)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                          open_world_hint=False))
    def update_layout(
        document_id: str,
        pages: Annotated[str | None, Field(description="Page range like '1-5,8'. Default: all read pages.")] = None,
    ) -> dict[str, Any]:
        """Bring a document read by an older ScholarOCR up to date without reading it again (no model call):
        boxes snapped to the ink, figure crops (original and cleaned), and cleaned text from the deterministic rule
        normaliser for text elements that have none (existing cleaned text and every transcription are kept).
        Returns per-page counts."""
        return _call(svc().refine_document, document_id, pages=pages)

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
