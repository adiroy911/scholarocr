# Architecture

ScholarOCR is one engine with three thin entry points (plus the hosted ChatGPT server). Agents never
get their own OCR path. The vision model is either the MCP client's own model (**host mode**, the default,
no API key) or a server-side key provider (Gemini or OpenRouter).

```
                AI agents: Antigravity CLI (plugin) · Claude Code · any MCP client · scripts
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
 preprocessing/  (OpenCV quality,        key mode: google-genai SDK ──► Gemini API, or httpx ──► OpenRouter
 enhancement, geometry)                  (only gemini_client.py talks to them)
                                         host mode: pipeline/host.py stores a HostTask; the client's model
                                         fetches the page (get_page_image) and submits elements
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
   `force_visual` send a page to visual OCR. Routing does not depend on the mode.
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
6. **Host mode** replaces the model call in step 4. After preprocessing, the page is saved with status
   `awaiting_transcription` and `pages/NNNN/host_task.json` (route, DPI, sizes, the original→processed
   pixel matrix, crop box, text-layer excerpt, hints, review options). The job ends with status
   `awaiting_transcription`. `get_page_image` serves the processed image (≤ `SCHOLAROCR_HOST_IMAGE_MAX_SIDE`)
   with the rules. `submit_page_content` validates the client's elements strictly
   (`host.validate_submission`, all-or-nothing, every problem reported with path and fix), then runs the
   same `convert_response` as provider output (inverse-transform box mapping, uncertainty rules),
   structure annotation and crops, and marks the page `done` with `extraction_method="host_model"`,
   `model="host:<client>"`. A reported rotation re-orients the page and asks again. `submit_review` applies
   a verdict with the same `apply_review` the provider reviewer uses.
7. **Verification / export / retrieval** read only the stored IR.

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

## Modes

`Settings.effective_provider` (CONFIGURATION.md): `SCHOLAROCR_PROVIDER=host` (the default, also when unset)
forces host mode; `gemini` / `openrouter` use that provider when its key is set (else host mode with a
note); `auto` (opt-in) picks
Gemini, then OpenRouter, then host. `ScholarOCR.mode` reports it and `ocr_document` returns it.

Why host mode is built on tool calls and not on MCP sampling: sampling (`sampling/createMessage`) would
let the server run its own prompts through the client's model, but no target client implements it as of
2026-11 (Antigravity CLI: not documented, no evidence of a handler; ChatGPT: undocumented,
listed without sampling in the last MCP clients matrix; Claude Code: issue #1785 open; Claude
Desktop/claude.ai: listed as unsupported), and protocol 2026-07-28 deprecates sampling (SEP-2577).
The hook stays: `Pipeline.run(model_client=…, model_method=…, model_label=…)` and
`ScholarOCR.process(model_override=…)` run one synchronous job through any `GeminiClient`, e.g. one over a
sampling `Transport`, on a separate pipeline instance with its own cache label.

Quality trade-off. Key mode: a dedicated OCR system prompt with structured output, the text-layer hint,
a profile per route and an independent review model for flagged elements. Host mode: the same rules and
schema, but one pass by whatever general model the client runs, with the client's image downscaling,
and a "review" that is the same model looking again. Validation, uncertainty rules, traceability and
verification are identical. Neither mode's accuracy is measured yet.

## Key decisions

| Decision | Reason |
|---|---|
| `client.models.generate_content` with `response_json_schema` | Official, non-deprecated, typed usage metadata and per-part media resolution in google-genai 2.34. The newer Interactions API is an alternative; only `SdkTransport` would change. |
| Schemas flattened for Gemini (`schema_utils.py`) | `$ref`/`anyOf` support is limited in structured output; Pydantic still validates everything client-side. |
| MCP SDK 2.x `MCPServer`, stdio default | `FastMCP` was renamed in mcp 2.x. stdio works for Antigravity CLI and Claude Code on Windows; streamable HTTP is optional for one shared server. |
| SQLite + files | Local-first, no server, safe for multiple processes (WAL). |
| Separate IR from exports | Every export/retrieval reuses the stored IR; no OCR on format changes. |
| Ordinal uncertainty | Model self-reports are not calibrated probabilities. |
| Host submissions use the provider response schema | One conversion path (box mapping, rules, crops) for every model; the model sees the same shape whether it is called by the server or calls the server. |
| `elements` typed as a list of objects in the tool schema | The full nested schema in the tool list would cost context on every turn and stresses Gemini function-declaration limits; the server's own validator gives better errors (paths, "did you mean"). |
| Remote accounts get random ids | The key is optional, so identity cannot come from it; a key can be linked and unlinked without moving the sandbox. |
| Location | Standalone repository `adiroy911/scholarocr`. |
