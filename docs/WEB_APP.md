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
- **Cleaned / As written.** Handwritten slips of casing, punctuation and spacing (a capital in mid-sentence, a
  missing full stop, `temperture`) are fixed in a cleaned version of each text element; the words, numbers and
  math are never changed, and the transcription as written is always kept. Each document has a switch; the
  default is in Settings ("Text shown and exported": Automatic = cleaned for handwritten documents, as written
  for printed ones). Export links follow the switch.
- **Figures** show the cleaned image (paper whitened, ruled lines removed) with the description as alt text and
  caption, and a toggle to the original crop. **Redraw** (per figure, opt-in, one or two model calls with the
  selected engine) makes a faithful vector copy; the server checks it against the crop and shows its fidelity
  score next to "Redrawn". A copy that fails twice is labelled "not faithful" and is never used in place of the
  crop or in exports.
- **Check and export.** Each document shows the verification result (`verify_document`) and one-click exports:
  Markdown, Word, LaTeX, searchable PDF, layout PDF, JSON, and CSV/Excel when the document has tables. The
  searchable PDF is the original scan with an invisible text layer; the layout PDF is a typeset remake with every
  element at its original position (docs/EXPORT_FORMATS.md). The layout PDF is printed by a Chromium-family
  browser (Edge, Chrome, Chromium or Brave, or `SCHOLAROCR_BROWSER`); without one it falls back to a basic
  renderer with equations as plain text and says so in the export warnings.
- **Keep asking.** Questions after a document ("make a question bank from this", "what's Q3's answer key?") are
  answered from the extracted content of the conversation's documents, with page citations (`[p. 3]`, clickable).
  The answer says when something is not in the document or when it adds worked answers of its own.
- **History** of conversations and documents is stored locally (`app.db` in the data directory).

- **Update layout.** A document read before box refinement, figure crops and cleaned text existed shows an
  "Older reading" row with **Update layout**: it snaps the boxes to the ink, crops and cleans the figures and adds
  cleaned text with the deterministic rule normaliser, without reading anything again (`refine_document`; also
  `scholarocr refine <doc>` and the MCP tool `update_layout`).

The states are explicit: the empty state, uploading (per-file progress), preparing pages, reading pages *4-6* of
*12* (several at once, with time and tokens per page), retrying after a validator rejection, a pause after
Antigravity reports rate limiting, partial results, errors and stopped runs. **Stop** cancels a run and kills
every Antigravity process it started; pages already read are kept.

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

There is no chat model inside a standalone app, so the app drives Antigravity CLI headless, one call per page,
several pages at a time:

1. The document is ingested by ScholarOCR in **host mode** (the same deterministic pipeline as over MCP: render,
   document-region crop, deskew, quality checks, routing). Each page that needs vision ends as
   `awaiting_transcription` with a prepared image (`host_page_image`). The job prepares the pages in the
   background and the reader starts on page 1 as soon as it is prepared (pipelining).
2. Up to **Pages at once** (Settings, 1-6, default 3) pages are read at the same time, one agy process each. The
   limit is shared by every document the app is reading. For each page the app writes the image into a fresh run
   folder and calls

   ```
   agy -p "<prompt>" --output-format stream-json --settings <run>/agy-settings.json --agent scholarocr-reader
       --json-schema <run>/page-schema.json --model <family> --effort <level> --print-timeout <n>s
       --disable-slash-commands
   ```

   - The settings file allows exactly one thing, `read_file(<run>/page)`. Headless mode cannot ask for
     permission, so every other tool call (shell commands, edits, browsing, MCP tools) is refused by agy. The
     prompt says so too. The user's `~/.gemini` settings and installed plugins are not changed: `--settings`
     applies to that session only.
   - `--agent scholarocr-reader` is a workspace agent written into the run folder
     (`.agents/agents/scholarocr-reader.md`) with only `read_file` and `finish`, told to return the JSON through
     `finish`. agy's default agent takes three model calls per page (the read, a text reply, then agy's own
     request for the schema output), each with its ~12k-token system prompt and tool list; the reader agent
     takes two with a short system prompt. An agy that does not know the agent ("unknown agent") falls back to
     the default agent.
   - **Effort** (Settings: "Effort for reading pages", default Low; "Effort for questions and Redraw", default
     Medium). agy chooses effort through the model variant: `--model gemini-3.8-flash-medium --effort low` is
     refused as a conflict, so the app passes the family (`gemini-3.8-flash`) with `--effort` when `agy models`
     lists that variant, and the chosen model unchanged otherwise (models without effort variants, such as
     `claude-*`, refuse `--effort`).
   - The prompt is the app's compact version of host mode's rules (`webapp/prompts.page_brief`, about 2,600
     characters instead of 6,400): the JSON schema already defines the fields, element types and doubt levels,
     so the MCP call shape and the type list are not repeated. The route profile and the user's instructions (as
     a hint) are included as in host mode.
   - `--json-schema` is the `PageExtractionResponse` schema, so agy returns `structured_output`.
   - Model API keys are removed from agy's environment, so it uses the signed-in account.
3. The reply goes through `ScholarOCR.submit_page_content`: the same strict validation, conversion (box mapping,
   uncertainty rules), box refinement, figure crops, cleaned text and storage as an MCP submission, right away in
   that page's worker. If it is rejected, the app continues the same agy conversation (`--conversation <id>`, so
   the image is not read again) with every validation problem and asks for the corrected page, at most twice. A
   reported rotation re-orients the page and reads it again. Pages finish in any order; they are stored by page
   number, so the result is in page order.
4. **Throttling.** When agy reports a rate limit or an exhausted quota (`429`, `RESOURCE_EXHAUSTED`, "quota",
   "rate limit", `503`/overloaded; agy retries per-minute 429s itself), or two pages in a row fail, the reader
   pauses (20 s, doubling), halves its concurrency for the rest of the run and reads those pages again (at most
   3 times). The progress line says so.
5. Each call has a timeout (5 minutes; Redraw 10 minutes) and is killed with its process tree on Stop.
6. The document is verified (`verify_document`) and shown.

An image cannot be attached to the first message instead of being read with `read_file` (tested with agy
1.4.12): `--input-format stream-json` accepts only `text` content blocks (`image`, `image_url`, `inline_data`
and `file` blocks end the run with "content block type ... is not supported"), and `@path` in a `-p` prompt
passes only the path.

Follow-up questions also go through `agy -p`, with no readable folders, the extracted content (trimmed to fit
the Windows command-line limit, about 18,000 characters of content) and the last few turns as context. Answers
stream into the conversation.

### Measured: reading several pages at once (2026-11-03, agy 1.4.12)

Pages 1-4 of a real 12-page handwritten physics notebook, `gemini-3.8-flash-medium` selected:

| | Wall time | Input tokens | Output (thinking) | Total tokens |
|---|---|---|---|---|
| 0.3.0: one page at a time, default agent, full host brief, medium | 483 s | 173.3k | 50.3k (38.8k) | 223.6k |
| now: 3 at once, pipelined, reader agent, compact brief, effort **low** (default) | **70 s** | 119.9k | 10.1k (0) | 130.0k |
| now, effort medium | 170 s | 83.1k | 46.9k (41.5k) | 130.0k |

Every page was accepted on the first attempt. Compared with the earlier transcription of the same pages, the
low-effort run dropped one phrase on one page (8 of 276 prose words missing, against 6 for the 0.3.0 run and 4
for the medium run); choose Medium when every word matters more than time.

### Measured (2026-11-03, agy 1.4.12, Windows 11, `gemini-3.8-flash-medium`, 0.3.0 sequential reader)

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
- A dense page can still take minutes at medium effort (254 s for the chemistry page in 0.3.0). Pages are read
  three at a time by default, which is gentler on the quota than it sounds only if the quota allows it: the app
  slows down by itself when Antigravity reports rate limiting.
- Redraw of a detailed drawing takes several minutes at medium effort and may not pass the fidelity check.
- Chat context through agy is limited by the command-line length (about 18,000 characters of extracted
  content); longer documents are trimmed and the answer is told so. Key engines get up to 120,000 characters.
- The app does not run the host-mode review pass (`submit_review`) for flagged elements; verification runs, and
  doubtful elements are highlighted for the reader to check against the crop.
- One user, one machine. There is no login; anyone who can use this computer's browser session can use the app.
