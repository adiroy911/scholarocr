# Configuration

All settings come from environment variables. Two optional files are read too, in this order, and a
variable already set (non-empty) always wins:

1. `.env` in the working directory;
2. the user-level file `SCHOLAROCR_ENV_FILE`, default `%LOCALAPPDATA%\ScholarOCR\scholarocr.env`
   (Windows) or `~/.local/share/scholarocr/scholarocr.env`. Use it for clients that do not start the
   server in your workspace: the Antigravity plugin starts it in the plugin folder, so its
   `SCHOLAROCR_ALLOWED_DIRS` and any key belong here (ANTIGRAVITY_PLUGIN.md).

`scholarocr info` prints the effective configuration and mode with keys redacted. Model IDs appear only
in `config.py` defaults and these variables.

## Mode and provider precedence

No API key is required, and none is used unless you ask for it: an exported `GEMINI_API_KEY` or
`OPENROUTER_API_KEY` is ignored while `SCHOLAROCR_PROVIDER` is unset. `SCHOLAROCR_PROVIDER` decides who
does the vision work:

| `SCHOLAROCR_PROVIDER` | Keys present | Effective mode |
|---|---|---|
| unset or `host` (default) | any | `host` (keys are ignored) |
| `auto` (opt-in) | `GEMINI_API_KEY`/`GOOGLE_API_KEY` | `gemini` |
| `auto` | only `OPENROUTER_API_KEY` | `openrouter` |
| `auto` | none | `host` |
| `gemini` / `openrouter` | that provider's key | that provider |
| `gemini` / `openrouter` | not that provider's key | `host`, with a `provider_note` warning in `ocr_document` and `info` |

**Host mode**: the server makes no model calls. Text-layer pages are extracted deterministically; every
other page is rendered, cropped, deskewed and stored as `awaiting_transcription` for the MCP client's
own model (`get_page_image` -> `submit_page_content`). See MCP_INTEGRATION.md. The CLI has no model of
its own, so in host mode `scholarocr process` stops with exit code 6 for such pages.

MCP sampling (the server borrowing the client's model through `sampling/createMessage`) is not used:
none of the target clients supports it (see ARCHITECTURE.md).

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | — | Optional. Selects the `gemini` provider for server-side visual OCR, classification, review and figure descriptions. Without any key the server runs in host mode. |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Primary extraction model. Verify IDs at https://ai.google.dev/gemini-api/docs/models |
| `GEMINI_REVIEW_MODEL` | `gemini-3.8-flash` | Used only for targeted review of flagged elements. |
| `SCHOLAROCR_PROVIDER` | `host` | `host`, `auto`, `gemini` or `openrouter` (table above). With `openrouter`, calls go to an OpenAI-compatible `/chat/completions` endpoint with image input and a strict JSON schema. |
| `OPENROUTER_API_KEY` | – | Optional key for the `openrouter` provider (used by `auto` when no Gemini key is set). |
| `OPENROUTER_MODEL` | `nvidia/nemotron-3.5-super-vl:free` | Replaces `GEMINI_MODEL` when the provider is `openrouter`; must accept images and structured outputs. |
| `OPENROUTER_REVIEW_MODEL` / `OPENROUTER_MATH_MODEL` | `OPENROUTER_MODEL` | Optional per-role overrides. |
| `OPENROUTER_MAX_TOKENS` | `16384` | Output cap per call, reasoning included. |
| `OPENROUTER_REASONING_EFFORT` | `low` | Sent as `reasoning.effort`; uncapped reasoning made one model think for 131k tokens on a single page. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Any OpenAI-compatible endpoint that supports `response_format: json_schema` and image input. |
| `GEMINI_MATH_MODEL` | = `GEMINI_MODEL` | Model for `visual_math` pages. |
| `GEMINI_TIMEOUT_SECONDS` | `180` | Per-request timeout. |
| `GEMINI_MAX_RETRIES` | `4` | Retries for 429/5xx/timeouts/network (exponential backoff with jitter, capped at 60 s). |
| `GEMINI_RPM_LIMIT` | `30` | Requests per minute across all threads (0 = unlimited). |
| `SCHOLAROCR_CONCURRENCY` | `2` | Pages processed by Gemini in parallel (also bounds images in memory). |
| `SCHOLAROCR_DATA_DIR` | `%LOCALAPPDATA%\ScholarOCR` / `~/.local/share/scholarocr` | Store: SQLite DB, preserved sources, page images, IR, crops, exports. |
| `SCHOLAROCR_ALLOWED_DIRS` | current working directory | Folders sources may be read from, separated by `;` (Windows) or `:` (Linux/macOS). |
| `SCHOLAROCR_MAX_FILE_MB` | `200` | Max size per input file. |
| `SCHOLAROCR_MAX_PAGES` | `1000` | Max pages per document. |
| `SCHOLAROCR_RENDER_DPI` | `200` | Page render resolution for page images and visual OCR. |
| `SCHOLAROCR_SYNC_PAGE_LIMIT` | `4` | MCP/API `ocr_document` waits for completion when the selection has at most this many pages (otherwise returns a job id). |
| `SCHOLAROCR_PRICING_FILE` | — | JSON price table for cost estimates (no prices are hardcoded). |
| `SCHOLAROCR_TEXT_LAYER_FONT` | auto | TTF used for searchable-PDF text layers (auto-discovers DejaVu Sans / Arial / Noto). |
| `SCHOLAROCR_RETENTION_DAYS` | — (keep forever) | On start-up and via `scholarocr purge`, delete generated artifacts of documents idle this many days. Preserved sources are never purged. |
| `SCHOLAROCR_HOST_IMAGE_MAX_SIDE` | `2000` | Host mode: longest side in pixels of page images handed to the client's model (PNG, JPEG above 1.5 MB). Boxes are normalised, so this never changes coordinates. |
| `SCHOLAROCR_ENV_FILE` | see above | User-level settings file. |
| `SCHOLAROCR_APP_DATA_DIR` | `<data dir>pp` | Web app (`scholarocr-app`): conversations, settings, uploads and the app's own store. The app ignores keys in the environment and uses only keys saved in its Settings (docs/WEB_APP.md). |
| `SCHOLAROCR_LOG_LEVEL` | `INFO` (`WARNING` for the CLI) | Logs go to stderr. |
| `SCHOLAROCR_APP_NAME` | `ScholarOCR` | Display name (MCP server title). |

## Web app settings (`prefs.json`)

Set in the app's Settings dialog and stored in `<app data dir>/prefs.json` (not environment variables):

| Setting | Default | Meaning |
|---|---|---|
| `read_concurrency` | `3` | Antigravity: pages read at the same time (1-6), one `agy` process each, shared by every document the app is reading. Halved for the rest of a run when agy reports rate limiting or an exhausted quota. |
| `read_effort` | `low` | Antigravity reasoning effort for reading pages (`low`, `medium`, `high`, `xhigh`, `max`). Passed as `--model <family> --effort <level>` when `agy models` lists that variant of the chosen model, otherwise ignored. |
| `chat_effort` | `medium` | The same for follow-up questions and Redraw. |
| `text_variant`, `figure_variant` | `auto`, `clean` | Text and figure images shown and exported (docs/WEB_APP.md). |
| `max_file_mb`, `max_files`, `max_pages` | `50`, `30`, `60` | Upload limits. |

Process-level limits (`AppConfig`): one page read times out after 300 s, a Redraw call after 600 s, a question
after 240 s; a page throttled more than 3 times stays unread; the first pause after throttling is 20 s.

## Pricing file

```json
{"_currency": "USD",
 "gemini-3.8-flash": {"input_per_million": 0.0, "output_per_million": 0.0}}
```
Fill in current prices from Google's pricing page; `scholarocr usage` / `ScholarOCR.usage()` then
reports an estimate (thinking tokens are billed as output). Models missing from the file are listed.

## Per-run options (ProcessingOptions / MCP / CLI)

`document_type`, `language_hint`, `context_hint` (hints, never treated as facts), `pages`,
`detail` (`standard` | `high` = review every equation/table), `force_visual`, `auto_crop` (default on:
crop screenshots/photos to the detected page), `review` (default on),
`reprocess` (ignore cache). All options that change output are part of the page cache key, and so is the
mode: a page transcribed in host mode is re-read by the provider when you later configure a key (use the
same mode, or keep the store separate, to keep host transcriptions).
