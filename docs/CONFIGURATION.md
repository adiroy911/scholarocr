# Configuration

All settings come from environment variables (a `.env` file in the working directory is read too;
real environment variables win). `scholarocr info` prints the effective configuration with the API
key redacted. Model IDs appear only in `config.py` defaults and these variables.

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | — | Required for visual OCR, classification, review and figure descriptions. Text-layer pages work without it. |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Primary extraction model. Verify IDs at https://ai.google.dev/gemini-api/docs/models |
| `GEMINI_REVIEW_MODEL` | `gemini-3.8-flash` | Used only for targeted review of flagged elements. |
| `SCHOLAROCR_PROVIDER` | `gemini` | `gemini` or `openrouter`. With `openrouter`, calls go to an OpenAI-compatible `/chat/completions` endpoint with image input and a strict JSON schema. |
| `OPENROUTER_API_KEY` | – | Key for the `openrouter` provider. |
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
| `SCHOLAROCR_LOG_LEVEL` | `INFO` (`WARNING` for the CLI) | Logs go to stderr. |
| `SCHOLAROCR_APP_NAME` | `ScholarOCR` | Display name (MCP server title). |

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
`reprocess` (ignore cache). All options that change output are part of the page cache key.
