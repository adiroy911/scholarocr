# Architecture

ScholarOCR is one engine with three thin entry points. Agents never get their own OCR path.

```
                AI agents: Claude Code · Gemini CLI · any MCP client · scripts
                         │                  │                      │
              MCP server (stdio / HTTP)   CLI (scholarocr)   Python API
              scholarocr.mcp.server        scholarocr.cli     ScholarOCR
                         └──────────────┬───┘                      │
                                        ▼                          │
                         ScholarOCR service facade  (service.py) ◄─┘
                                        │
     ┌──────────────┬───────────────────┼────────────────────┬──────────────────┐
 Ingestion      Pipeline            Intelligence         Verification        Export
 ingestion/     pipeline/           intelligence/        pipeline/           export/
 loader, pdf,   routing, text_layer, gemini_client,       verification.py     json, markdown,
 images, docx,  orchestrator,        extractor, reviewer,                     searchable_pdf,
 validation     checkpoints,         diagrams, classifier,                    docx, latex,
                job_manager,         prompts, responses,                      spreadsheet
                structure            uncertainty, math/tables checks
     │              │                     │
 preprocessing/  (OpenCV quality,        google-genai SDK  ──►  Gemini API
 enhancement, geometry)                  (only gemini_client.py talks to it)
                                        │
                    Storage: SQLite (metadata, pages, jobs, usage, exports, locks)
                           + files (preserved sources, page images, page IR JSON, crops, exports)
```

## Data flow

1. **Ingestion** (`ingestion/loader.py`) validates every input by magic bytes and opens it
   (corrupt/encrypted PDFs fail with typed errors), copies each source into the store as a read-only
   preserved copy, and writes `manifest.json`: one entry per page in caller order, with dimensions,
   content hash, perceptual hash, text-layer profile (chars, cleanliness, image coverage, math-font
   spans, vector drawings) and blank/duplicate flags. Document ID = hash of the ordered source hashes.
2. **Routing** (`pipeline/routing.py`) picks a route per page: `blank` (no work), `embedded_text`
   (reliable text layer, no model call), `docx_text`, or a visual route (`visual_general`,
   `visual_math`, `visual_handwriting`). Math fonts, large images, garbled text layers, scans and
   `force_visual` send a page to visual OCR.
3. **Deterministic extraction** (`pipeline/text_layer.py`) turns a text layer into typed elements
   (XY-cut column-aware reading order, heading levels from font size, header/footer/page-number,
   captions, lists, code, footnotes, bibliography, ruled tables via PyMuPDF's table finder, vector
   diagrams from drawing clusters with their labels, raster images). Figures on such pages get one
   targeted Gemini description call each (crop only) when a key is configured.
4. **Visual extraction**: render the page → crop to the document region when the image is a
   screenshot/photo of a page inside a larger frame → quality assessment (polarity-aware) →
   condition-driven preprocessing (rotation, deskew, upscale, denoise, CLAHE — each only when
   measured necessary) → one Gemini call
   with the route's prompt profile and structured JSON output → conversion to IR with boxes mapped
   back through the inverse preprocessing transform → deterministic uncertainty rules → targeted
   review of flagged elements with the review model → crops saved for traceability.
5. **Checkpoint**: each finished page is written atomically to `pages/NNNN/page.json` and recorded
   in SQLite with a cache key. Re-runs skip pages whose key matches.
6. **Verification / export / retrieval** read only the stored IR.

## Threading

PyMuPDF is not thread-safe, so rendering and text-layer work run on the job thread; only Gemini
work (and crops, checkpoint writes) runs in a pool of `SCHOLAROCR_CONCURRENCY` workers. At most that
many page images are in memory at once, so very large documents stream through.

## Jobs

`JobManager` runs jobs synchronously or on a background thread, records progress in SQLite, honours
cancellation between pages, marks jobs whose process died as `interrupted`, and resumes any
non-completed job by re-running its page selection (finished pages come from checkpoints).
A per-document lock (SQLite row + heartbeat) prevents two processes (e.g. two stdio MCP servers)
from processing the same document at once.

## Key decisions

| Decision | Reason |
|---|---|
| `client.models.generate_content` with `response_json_schema` | Official, non-deprecated, typed usage metadata and per-part media resolution in google-genai 2.34. The newer Interactions API is an alternative; only `SdkTransport` would change. |
| Schemas flattened for Gemini (`schema_utils.py`) | `$ref`/`anyOf` support is limited in structured output; Pydantic still validates everything client-side. |
| MCP SDK 2.x `MCPServer`, stdio default | `FastMCP` was renamed in mcp 2.x. stdio works for Claude Code and Gemini CLI on Windows; streamable HTTP is optional for one shared server. |
| SQLite + files | Local-first, no server, safe for multiple processes (WAL). |
| Separate IR from exports | Every export/retrieval reuses the stored IR; no OCR on format changes. |
| Ordinal uncertainty | Model self-reports are not calibrated probabilities. |
| Location | Built as the self-contained `scholarocr/` folder of this repository (the only one available to the session); no other files were changed, so it can be split out with `git subtree split --prefix scholarocr`. |
