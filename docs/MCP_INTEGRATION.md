# MCP integration

Server: `scholarocr-mcp` (module `scholarocr.mcp.server`), built on the official MCP Python SDK 2.x
(`mcp.server.mcpserver.MCPServer`). Default transport **stdio**; optional **streamable HTTP**
(`scholarocr-mcp --transport streamable-http --host 127.0.0.1 --port 8765`, endpoint `/mcp`).

## Tools

| Tool | Read-only | Summary |
|---|---|---|
| `ocr_document` | no | Ingest + extract; waits for small selections, otherwise returns a job id |
| `get_document_status` | yes | Job progress, or document summary/outline |
| `get_document_content` | yes | Paginated content (markdown with element anchors / json / text) |
| `get_page_content` | yes | Full IR of one page |
| `get_source_region` | yes | Original image crop of an element or region (image + metadata) |
| `get_page_image` | yes | Host mode: one page image awaiting transcription + rules + element shape |
| `submit_page_content` | no | Host mode: validate and store the client model's transcription of a page |
| `submit_review` | no | Second pass on one element (verdict, correction, or a figure description) |
| `verify_document` | no* | Quality checks (*`review=true` updates the extraction) |
| `export_document` | no | JSON, Markdown, searchable PDF, DOCX, LaTeX, CSV, XLSX |
| `list_documents` | yes | Catalogue with filters/pagination |
| `classify_document` | no | Document type, subjects, level, topics (needs a key; `model_unavailable` in host mode) |
| `update_layout` | no | Bring a document read by an older version up to date without reading it again: boxes snapped to the ink, figure crops, rule-cleaned text |
| `delete_document` | no (destructive) | Delete generated artifacts, or everything with `confirm` |
| `manage_job` | no | Cancel / resume |

Input schemas use enums for formats, document types, element types and verification scopes. Errors
are returned as MCP tool errors (`isError`) whose text is `{"error": code, "message": …}`.

## Host mode: the client's model is the vision model

Without a key (the default) the server never calls a model. The MCP `instructions`, the tool
descriptions, the Antigravity plugin's `rules/AGENTS.md` and the Claude skill all describe this loop:

```
ocr_document(file)                 -> mode "host"; text-layer pages done; others in awaiting_transcription
repeat for each waiting page:
  get_page_image(document_id)      -> [image, text]: ONE processed page (deskewed, cropped), the rules,
                                      the element JSON shape (full the first time, short afterwards),
                                      page_number, next_waiting_page
  submit_page_content(document_id, page_number, elements=[...], page_language, page_label, page_doubt,
                      rotation_detected, notes)
     rejected -> error submission_rejected, details.errors[] = {path, problem, fix}; nothing stored
     accepted -> counts, warnings, review_suggested, next_waiting_page
optional: get_source_region(element_id) -> submit_review(document_id, element_id, verdict, ...)
then get_document_content / verify_document / export_document as usual
```

- The submitted shape is the one the key providers return (`PageExtractionResponse`), with
  `box_2d = [ymin, xmin, ymax, xmax]` in 0-1000 of the image the client was given. The server maps boxes
  back through the inverse preprocessing transform to PDF points, applies the same deterministic
  uncertainty rules, structure annotation and crops, and records `extraction_method = "host_model"` and
  `model = "host:<client name>"`.
- Validation is strict and all-or-nothing: unknown fields (with "did you mean" hints such as
  `bbox -> box_2d`), wrong types/enums, reversed or out-of-range boxes, bad `caption_of`/`parent_index`,
  empty text, tables without rows, and LaTeX syntax errors at low/medium uncertainty. A LaTeX problem the
  model cannot fix is accepted once it marks the element `high`/`unresolved`.
- `rotation_detected` 90/180/270 makes the server re-orient the page and ask again (at most twice).
- Each element's `uncertainty` (alias `doubt`; `none` is read as `low`) is the model's doubt about that
  element. `page_doubt` is the overall doubt and does not change element labels; `legibility` is its
  deprecated name. `elements` is typed loosely on purpose: a malformed list or a JSON string reaches the
  validator and gets the usual path / problem / fix answer instead of a schema error.
- `get_page_content(compact=true)` or `fields=[...]` returns a fraction of the full page record.
- Context use: one image per `get_page_image` call; nothing returns images except it and
  `get_source_region`. The full rules are sent until the first page of a document is submitted.

Quality trade-off: host mode is one pass by a general chat model, guided by the same rules. The key
pipeline uses a dedicated OCR prompt with structured output, the text-layer hint, a math/handwriting
profile and an independent second model for flagged elements. Host-mode "review" is the same model
looking again at a crop, which catches fewer errors. Neither has measured accuracy yet
(QUALITY_ASSURANCE.md).

## Which clients can do host mode (2026-11)

| Client | Model sees images in tool results | MCP sampling | Host mode |
|---|---|---|---|
| Antigravity CLI (`agy`) | yes: verified 2026-11-03, the model transcribed the image returned by `get_page_image` exactly (changelog 1.1.5: inline media from MCP results surface in the conversation) | no evidence | works (one page verified; see ANTIGRAVITY_PLUGIN.md) |
| Claude Code | yes (code.claude.com/docs/en/mcp, "Images in tool results") | no (issue anthropics/claude-code#1785 open) | works |
| Claude Desktop / claude.ai | yes ("Text and image-based tool results", claude.com/docs/connectors/building) | no (same page lists Sampling as unsupported) | works |
| ChatGPT apps | not reliably (Plugins reference lists `content` as "surfaced to the model" but never vision; OpenAI forum threads 1375446, 1399928, 1360430) | no (not documented; community request 1370474 unanswered) | via the page-image widget (CHATGPT_APP.md) |
| Gemini app custom connectors | unknown; Google says to put data in TextContent (developers.google.com/connected-apps/mcp/server-optimization) | not documented | not supported; needs the hosted server and is US/English-only |

MCP sampling would let the server run its own prompts through the client's model, but no target client
implements it, and protocol 2026-07-28 deprecates sampling (SEP-2577; the MCP SDK marks
`create_message` deprecated). ScholarOCR therefore does not use it. `ScholarOCR.process(...,
model_override=(client, method, label))` and `Pipeline.run(model_client=...)` remain as the hook: a
sampling-backed `Transport` would plug in there without other changes.

## Claude Code (tested)

```bash
# host mode: Claude reads the page images, no key needed
claude mcp add --scope user scholarocr \
  -e SCHOLAROCR_ALLOWED_DIRS="/home/me/study:/home/me/Downloads" \
  -e SCHOLAROCR_DATA_DIR="/home/me/.scholarocr" \
  -- /path/to/scholarocr/.venv/bin/scholarocr-mcp
# optional: add -e GEMINI_API_KEY="$GEMINI_API_KEY" for the server-side pipeline
```
Windows: the command is `C:\…\scholarocr\.venv\Scripts\scholarocr-mcp.exe` and folders in
`SCHOLAROCR_ALLOWED_DIRS` are separated by `;`. A project-scoped `.mcp.json` example is in
`examples/claude_code.mcp.json`.

Tested with Claude Code 2.1.316 (headless `claude -p`, Linux) using `--mcp-config`, in key mode: the
server connected, the `scholarocr` skill was discovered and loaded, and Claude completed a
question-answering task by calling `list_documents` → `ocr_document(pages="2")` →
`get_document_content` → `get_source_region`, answering with a page citation. The host-mode loop has
not been run live in Claude Code yet (it has in Antigravity CLI, above).

## Antigravity CLI (tested, the main integration)

Use the plugin (docs/ANTIGRAVITY_PLUGIN.md):

```bash
uv tool install "git+https://github.com/adiroy911/scholarocr"
agy plugin install https://github.com/adiroy911/scholarocr
```

It needs no key: Antigravity's model transcribes in host mode. The plugin's `mcp_config.json` runs
`scholarocr-mcp` over stdio and declares no variables. Antigravity starts the server in the plugin
folder, so `SCHOLAROCR_ALLOWED_DIRS` (and an optional key with `SCHOLAROCR_PROVIDER`) go in the user-level
`scholarocr.env` (CONFIGURATION.md). Tools appear to the model under the server name
`scholarocr_scholarocr`; Antigravity writes a descriptor file per tool to
`~/.gemini/antigravity-cli/mcp/scholarocr_scholarocr/`, which the model reads before `call_mcp_tool`.
Headless runs (`agy -p`) need allow rules in a `--settings` file; the exact rules are in
ANTIGRAVITY_PLUGIN.md.

Tested 2026-11-03 with agy 1.4.12 and `gemini-3.8-flash-medium`, no key: `ocr_document` ->
`get_page_image` -> `submit_page_content` (accepted first time) -> `verify_document` (passed) on one
synthetic scanned page, transcribed exactly. Multi-page documents and a rejected-then-resubmitted math
page were not tested.

## Other MCP clients

Any client that launches a stdio server works: command `scholarocr-mcp` (or `python -m
scholarocr.mcp.server`), environment as above. `examples/03_mcp_client.py` is a minimal client using
the official SDK. Clients that ignore `structuredContent` still receive the same JSON as text content.

## Shared server (several agents at once)

stdio starts one server process per agent session. They share the data directory safely (SQLite WAL,
per-document processing lock), but a background job lives in the process that started it: if that
agent session closes, the job is marked `interrupted` and can be resumed by any agent with
`manage_job(job_id, "resume")`. For long jobs shared by several agents, run one HTTP server:
`scholarocr-mcp --transport streamable-http` and register `http://127.0.0.1:8765/mcp` in each client
(`claude mcp add --transport http scholarocr http://127.0.0.1:8765/mcp`). The HTTP mode is not covered
by the automated tests.

## Long-running work

Selections above `SCHOLAROCR_SYNC_PAGE_LIMIT` pages return immediately with a `job_id`; poll
`get_document_status(job_id)`. Partial failures yield `status=partial`; `manage_job(resume)` re-runs
only unfinished pages.
