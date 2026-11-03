# Web app (`scholarocr-app`)

A local, single-user web interface for ScholarOCR. It has a chat-style layout: a sidebar with the history of
documents read, a composer at the bottom for files and instructions, and results in a conversation column.
It runs on your own computer and binds to `127.0.0.1` only.

```bash
pip install -e ".[webapp]"          # or: uv tool install "scholarocr[webapp] @ git+https://github.com/adiroy911/scholarocr"
scholarocr-app --open               # http://127.0.0.1:8765/
scholarocr-app --port 9000 --data-dir D:\ScholarOCR-app
```

| Option | Default | |
|---|---|---|
| `--port` | `8765` | The app answers only on 127.0.0.1 at this port. |
| `--open` | off | Opens the default browser. |
| `--data-dir` | `SCHOLAROCR_APP_DATA_DIR`, else `%LOCALAPPDATA%\ScholarOCR\app` (`~/.local/share/scholarocr/app`) | Conversations, settings, uploads and the app's own ScholarOCR store. |

## What you can do

- **Read documents.** Attach PDFs, page photos or DOCX files with `+`, by dragging them anywhere onto the
  window, or by pasting. Page photos sent together form one document, in the order attached; each PDF or DOCX
  is its own document. Text typed with the files is sent to the reader as instructions, after the saved
  instructions from Settings (for example "Class 11 chemistry; keep question numbers; Hindi + English").
- **See the result per page**: headings, paragraphs, math (KaTeX), chemistry (`\ce{}` through mhchem), tables
  with merged cells, figure descriptions and their labels, and question numbers and marks in the margin of an
  answer-sheet layout. Content the reader doubted is underlined (some doubt) or highlighted (doubtful), and
  unreadable regions are boxed in red. Click any element (or focus it and press Enter) to see its source
  crop (`get_source_region`), the raw text and LaTeX, and the whole page.
- **Check and export.** Each document shows the verification result (`verify_document`) and one-click exports:
  Markdown, Word, LaTeX, searchable PDF, JSON, and CSV/Excel when the document has tables.
- **Keep asking.** Questions after a document ("make a question bank from this", "what's Q3's answer key?") are
  answered from the extracted content of the conversation's documents, with page citations (`[p. 3]`, clickable).
  The answer says when something is not in the document or when it adds worked answers of its own.
- **History** of conversations and documents is stored locally (`app.db` in the data directory).

The states are explicit: the empty state, uploading (per-file progress), preparing pages, reading page *n* of
*N* (with time and tokens per page), retrying after a validator rejection, partial results, errors and stopped
runs. **Stop** cancels a run; pages already read are kept.

## Engines

The engine picker in the composer chooses who reads the pages that need vision. Pages with a reliable PDF text
layer never need a model.

| Engine | Needs | How it works |
|---|---|---|
| **Antigravity (your Gemini account)** (default) | `agy` on PATH, signed in (`agy` once in a terminal) | Host mode plus `agy -p` per page, below. No API key. |
| **Gemini API key** | a key saved in Settings | ScholarOCR's server-side pipeline (`SCHOLAROCR_PROVIDER=gemini`). |
| **OpenRouter key** | a key saved in Settings | Same with OpenRouter (`SCHOLAROCR_PROVIDER=openrouter`). |

"Automatic" (the default preference) uses Antigravity when `agy` is installed and lists models, else a stored
Gemini key, else a stored OpenRouter key. With none of them the app shows a setup screen. The model list for
Antigravity comes from `agy models` (cached for 10 minutes; "Check engines again" in Settings refreshes it).

Keys are stored in the OS keychain when the `keyring` package (part of the `webapp` extra) has a working
backend, otherwise in the user-level `scholarocr.env`. The API only ever reports "set" or "not set"; a stored key
is never shown again or logged. Keys exported in the environment are not used by the app; only stored ones are.

### Antigravity engine in detail

There is no chat model inside a standalone app, so the app drives Antigravity CLI headless, one call per page:

1. The document is ingested by ScholarOCR in **host mode** (the same deterministic pipeline as over MCP: render,
   document-region crop, deskew, quality checks, routing). Pages that need vision end as
   `awaiting_transcription` with a prepared image (`host_page_image`).
2. For each such page the app writes the image into a fresh run folder and calls

   ```
   agy -p "<prompt>" --output-format stream-json --settings <run>/agy-settings.json
       --json-schema <run>/page-schema.json --model <picked model> --print-timeout <n>s --disable-slash-commands
   ```

   - The settings file allows exactly one thing, `read_file(<run>/page)`. Headless mode cannot ask for
     permission, so every other tool call (shell commands, edits, browsing, MCP tools) is refused by agy. The
     prompt says so too. The user's `~/.gemini` settings and installed plugins are not changed: `--settings`
     applies to that session only.
   - The prompt is host mode's own rules and element shape (`prompts.host_page_brief`, with the route profile and
     the user's instructions as a hint), with a short preface: read this one file, then reply with the page JSON.
   - `--json-schema` is the `PageExtractionResponse` schema, so agy returns `structured_output`.
   - Model API keys are removed from agy's environment, so it uses the signed-in account.
3. The reply goes through `ScholarOCR.submit_page_content`: the same strict validation, conversion (box mapping,
   uncertainty rules, crops) and storage as an MCP submission. If it is rejected, the app continues the same agy
   conversation (`--conversation <id>`, so the image is not read again) with every validation problem and asks
   for the corrected page, at most twice. A reported rotation re-orients the page and reads it again.
4. Each call has a timeout (5 minutes) and is killed with its process tree on Stop.
5. The document is verified (`verify_document`) and shown.

Follow-up questions also go through `agy -p`, with no readable folders, the extracted content (trimmed to fit
the Windows command-line limit, about 18,000 characters of content) and the last few turns as context. Answers
stream into the conversation.

### Measured (2026-11-03, agy 1.4.12, Windows 11, `gemini-3.8-flash-medium`)

One real run through the app in a browser: a 2-page synthetic scanned PDF built with `tests/fixtures/corpus.py`
helpers (page 1: four maths questions with marks; page 2: three chemistry questions with mhchem formulas),
instruction "Class 11 test paper; keep question numbers and marks exactly as printed."

| | Tokens (input / output incl. thinking / total) | agy time | Page total | Attempts |
|---|---|---|---|---|
| Page 1, maths | 47,322 / 3,231 / **50,553** | 43 s | 66 s | 1 |
| Page 2, chemistry | 81,093 / 30,115 (29,291 thinking) / **111,208** | 236 s | 254 s | 1 |
| Whole run, incl. 20 s page preparation and verification | **161,761** | | 5 min 46 s | |
| Follow-up question ("answer key for Q3 and Q3(a)") | **13,200** | | 24 s | |

Both pages were accepted on the first try. All question numbers, marks, LaTeX (`\int_0^1`, `\lim`, `\sqrt{2}`)
and mhchem (`\ce{N2 + 3H2 <=> 2NH3}`, `\ce{SO4^{2-}}`) were transcribed exactly, and verification passed. For
comparison, the earlier MCP plugin run used about 116k tokens for one page. A trivial `agy -p` prompt costs
11,456 input tokens (agy's own system prompt and tool list), and a page call takes at least two model turns
(read the image, answer). The chemistry page cost more because the model thought for 29k tokens.

In that run the model set `legibility: "high"` for a clean page, meaning "very legible". The field is a level of
doubt, so the uncertainty rules raised every element to "some doubt". Fixed in 0.3.0: the field is now
`page_doubt` (the old name is still accepted), it no longer raises element labels at all, and the page prompt
explains each element label (`low` = the model would bet the transcription is exact). The re-run is reported in
CHANGELOG.md.

The Word export link has a tooltip: native Word equations need pandoc on PATH; without it equations are inserted
as LaTeX source and inline math as plain text (docs/INSTALLATION.md).

## Security

- Binds to 127.0.0.1 only, with no option to change it. Every request's `Host` must be 127.0.0.1 or localhost on
  the app's port (DNS-rebinding protection).
- Every `/api` call needs a random per-process token that is embedded in the page. GET links such as downloads,
  images and the event stream may carry it as `?t=`. State-changing requests with a foreign `Origin` and
  `Sec-Fetch-Site: cross-site` requests are refused.
- Uploads are written only under the app's data directory, with random folder names and safe file names. The
  type is decided by magic bytes (PDF, PNG, JPEG, WebP, TIFF, GIF, BMP, DOCX), and size, files-per-message and
  pages-per-document limits come from Settings. The ScholarOCR service may only read that uploads folder.
  Staged files are deleted when the run ends (the service keeps its own preserved copy).
- Content-Security-Policy allows scripts only from the app (`script-src 'self'`, no inline scripts), plus
  `frame-ancestors 'none'`, `nosniff` and `no-referrer`. Model output is placed with DOM text nodes; LaTeX is
  rendered by KaTeX with `trust: false`. No server data is ever parsed as HTML (a test checks the script for
  HTML sinks).
- Exports, crops and documents are served only for documents that belong to a conversation in this app.

## Layout of the code

```
src/scholarocr/webapp/
  server.py    Starlette app, routes, LocalOnly security wrapper, scholarocr-app entry point
  runs.py      background runs (read / chat), event stream, the agy page loop with retries
  agy.py       AgyRunner: command line, per-run settings and schema files, stream-json parsing, timeout, cancel
  engines.py   engine detection and selection, one ScholarOCR service per engine
  prompts.py   page and chat prompts (page prompt = host-mode brief + preface)
  view.py      document payload for the page, verification summary, chat context
  uploads.py   upload intake (type sniffing, limits, safe names)
  store.py     conversations, messages and documents (app.db)
  keys.py      key storage (keychain or scholarocr.env)
  config.py    process config and user preferences (prefs.json)
  static/      index.html, app.css, app.js, icons, vendored KaTeX/mhchem and fonts (works offline)
```

No build step: the frontend is plain HTML, CSS and JavaScript. Third-party files and their licences are listed in
`src/scholarocr/webapp/static/ATTRIBUTION.md`.

## Limits and known gaps

- Stopped, failed or partially read documents cannot be resumed from the page yet: attach the file again. Pages
  already read come from the cache, so only the rest is read.
- Antigravity pages are read one at a time. A dense page can take several minutes (254 s for the chemistry page).
- Chat context through agy is limited by the command-line length (about 18,000 characters of extracted
  content); longer documents are trimmed and the answer is told so. Key engines get up to 120,000 characters.
- The app does not run the host-mode review pass (`submit_review`) for flagged elements; verification runs, and
  doubtful elements are highlighted for the reader to check against the crop.
- One user, one machine. There is no login; anyone who can use this computer's browser session can use the app.
