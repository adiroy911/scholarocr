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
| `verify_document` | no* | Quality checks (*`review=true` updates the extraction) |
| `export_document` | no | JSON, Markdown, searchable PDF, DOCX, LaTeX, CSV, XLSX |
| `list_documents` | yes | Catalogue with filters/pagination |
| `classify_document` | no | Document type, subjects, level, topics |
| `delete_document` | no (destructive) | Delete generated artifacts, or everything with `confirm` |
| `manage_job` | no | Cancel / resume |

Input schemas use enums for formats, document types, element types and verification scopes. Errors
are returned as MCP tool errors (`isError`) whose text is `{"error": code, "message": …}`.

## Claude Code (tested)

```bash
claude mcp add --scope user scholarocr \
  -e SCHOLAROCR_ALLOWED_DIRS="/home/me/study:/home/me/Downloads" \
  -e SCHOLAROCR_DATA_DIR="/home/me/.scholarocr" \
  -e GEMINI_API_KEY="$GEMINI_API_KEY" \
  -- /path/to/scholarocr/.venv/bin/scholarocr-mcp
```
Windows: the command is `C:\…\scholarocr\.venv\Scripts\scholarocr-mcp.exe` and folders in
`SCHOLAROCR_ALLOWED_DIRS` are separated by `;`. A project-scoped `.mcp.json` example is in
`examples/claude_code.mcp.json`.

Tested with Claude Code 2.1.316 (headless `claude -p`, Linux) using `--mcp-config`: the server
connected, the `scholarocr` skill was discovered and loaded, and Claude completed a question-answering
task by calling `list_documents` → `ocr_document(pages="2")` → `get_document_content` →
`get_source_region`, answering with a page citation.

## Gemini CLI (configuration documented, not tested here)

Gemini CLI was not available in the development environment. The recommended route is the packaged
extension in `extensions/gemini-cli/` (see `docs/GEMINI_CLI_EXTENSION.md`): it asks for the API keys
as extension settings, stored in the system keychain, which Gemini CLI passes to the server.

For a hand-written entry in `~/.gemini/settings.json` (user) or `.gemini/settings.json` (project),
do NOT pass the key as `"GEMINI_API_KEY": "$GEMINI_API_KEY"`. Gemini CLI strips host variables whose
names match `*KEY*`/`*TOKEN*`/`*SECRET*` (and values that look like Google API keys) before expanding
`$VAR` in `env`, so the server receives an empty key (from reading Gemini CLI's source, not tested live).
Instead, start the server in a folder whose `.env` holds the key; `.env` is read from the working directory:

```json
{"mcpServers": {"scholarocr": {
  "command": "C:\\path\\scholarocr\\.venv\\Scripts\\scholarocr-mcp.exe",
  "cwd": "C:\\path\\scholarocr",
  "env": {"SCHOLAROCR_ALLOWED_DIRS": "C:\\Users\\you\\Documents"},
  "timeout": 600000}}}
```
(`C:\path\scholarocr\.env` contains `GEMINI_API_KEY=...`, with no inline `# comment` on the line.)
An empty `GEMINI_API_KEY` passed by the client no longer hides the `.env` value. Non-secret variables
such as `SCHOLAROCR_ALLOWED_DIRS` pass through normally, also via
`gemini mcp add -s user -e SCHOLAROCR_ALLOWED_DIRS=… scholarocr <path-to-scholarocr-mcp>`.
Keep the server name without underscores (it is `scholarocr`); stdio servers show as disconnected
until the folder is trusted. Gemini CLI documents support for image
content in tool results (used by `get_source_region`).

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
