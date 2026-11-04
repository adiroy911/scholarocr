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
- **Second look at doubtful elements** (Settings, off by default, Antigravity only; see below). A reviewed element
  says "confirmed" or "changed" on the sheet, and its source panel says what happened. A change keeps the reading
  from before it, shown in the panel, with **Undo this change**.
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

## Resuming a document

A document with pages not yet accepted shows a "Not finished" row with **Resume reading**, whatever left it that
way: Stop, failed or rejected pages, an exhausted quota, or the app closing or being killed during the read. The row
is worked out from the stored pages each time the document is shown, so it also appears after a restart; a read that
was running when the app died shows as interrupted, not as reading.

- Only pages not yet accepted are read. Accepted and blank pages stay outside the job's page selection and are not
  rebuilt, re-read or changed, and their token and time figures stay in the message.
- The read continues in the same message, with the engine, model and effort of the first read. Instructions come from
  that read too. Pages at once is the current setting, with the same app-wide limit, throttling, progress display
  and Stop. Pages that are already prepared start at once, and a job runs only for pages that still need preparing.
- A page that failed before is tried again. If it fails again, the message says "Resumed, but some pages still could
  not be read", the page shows the reason from its last attempt, and the row stays so it can be tried once more. Quota
  errors count like any other failure once the retries of that run are used up.
- One run per document in the whole app. A second click, a second tab or a resume during reading answers with the
  run already going (`POST /api/documents/{id}/resume`, 200 `already_running`), and attaching the same file in another
  conversation during a read is refused. Resume is also refused while the conversation answers a question.
- If the stored copy of the source is gone and a page still needs it, the row says so and offers **Attach file**, which
  puts the instructions of that read back in the composer. The same file restores the stored copy, and pages already
  read come from the cache. With different instructions the cache does not apply, and every page is read again.
  Pages already prepared do not need the source, so they can still be resumed.
- A file of a multi-file message whose reading never began has no document and its upload is gone. The message names
  it and asks for it to be attached again.

Measured (2026-11-04, agy 1.4.12, `gemini-3.8-flash-medium` at effort Low, one page at a time, a 2-page synthetic scan):
page 1 was accepted after 27 s and 22,169 tokens and the read was stopped; the app was killed and started again; **Resume
reading** read only page 2 (32 s, 22,034 tokens, first attempt). The log shows `1 page(s) accepted and kept [1]; reading
[2]; preparing []` and one read, page 2. Page 1's stored file was byte-identical afterwards with the same modified time,
verification passed, and the message totals 44,203 tokens and 64.5 s.

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

Checked against the real Windows Credential Manager on 2026-11-04 with keyring 25.7.0 (dummy values): save, read back,
restart, replace and delete both providers, and the state the Settings dialog reads (`key_backend: "keychain"`). The
first provider saved is under the generic target `ScholarOCR`; with both saved, one moves to `<NAME>@ScholarOCR`.
`keyring` comes only with the `webapp` extra: an install with `.[dev]` alone has no keychain and keys go to the plain-text
`scholarocr.env`. A key already in that file stays there when a later save goes to the keychain, until the key is deleted
in Settings; the MCP server reads that file.

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
   number, so the result is in page order. With the second look on, the page's doubtful elements are checked
   next, in the same worker (below).
4. **Throttling.** When agy reports a rate limit or an exhausted quota (`429`, `RESOURCE_EXHAUSTED`, "quota",
   "rate limit", `503`/overloaded; agy retries per-minute 429s itself), or two pages in a row fail, the reader
   pauses (20 s, doubling), halves its concurrency for the rest of the run and reads those pages again (at most
   3 times). The progress line says so.
5. Each call has a timeout (5 minutes, also for the second look; Redraw 10 minutes) and is killed with its process tree on Stop.
6. The document is verified (`verify_document`) and shown.

An image cannot be attached to the first message instead of being read with `read_file` (tested with agy
1.4.12): `--input-format stream-json` accepts only `text` content blocks (`image`, `image_url`, `inline_data`
and `file` blocks end the run with "content block type ... is not supported"), and `@path` in a `-p` prompt
passes only the path.

### Second look at doubtful elements

Settings, "Second look at doubtful elements", **off by default** (measured below). After a page is accepted, the
elements that `reviewer.select_for_review` picks (medium or high doubt or an unreadable region, with a box, at most
8 per page, as `submit_page_content` reports them in `review_suggested`) are checked in one more `agy -p` call. A page
with nothing selected costs nothing. Key engines review in their own pipeline; they ignore this switch, and their
corrections show the same marks and can be undone too.

- **One call per page, not per element.** A call costs about 8k input tokens per model turn whatever it reads, so
  the crops of a page share one call. Each crop is the image the source panel shows, written to a `crops` folder.
- **The same sandbox as a page read.** The folder is the only `read_file` path in `--settings` (the page image is
  not in it), the reader agent has only `read_file` and `finish`, model API keys are removed, and every other tool
  is refused. `--json-schema` asks for one verdict per crop. The run folder is deleted afterwards.
- **Effort:** "Effort for questions, Redraw and the second look" (default Medium), not the reading effort. At low
  effort the verdicts changed from one try to the next (measured below).
- **A verdict counts only if it can be trusted.** The model must have opened that crop (seen in agy's event
  stream). A correction is not applied, and the reading stays, when it fails the schema, has a LaTeX syntax
  problem (`latex_issues`, `chemistry_issues`, inline `$...$`), has an unbalanced `$`, is empty, changes a math
  element's text but not its LaTeX, adds a table structure problem, replaces most of the reading (under 50 %
  similar, for 20 characters or more), or the model itself reports high doubt about it. A confirmation the model
  doubts (its own label above low) is not a confirmation. These show as "not settled"; the element stays marked,
  and the proposal is kept in `revisions`. A "correction" equal to the stored reading counts as a confirmation.
- **What is stored.** Verdicts go through `submit_review`, so labels follow `uncertainty.after_review` and old values
  stay in `revisions`. The app adds a `before_review` revision with the whole element as it was, which is what
  Undo restores (text, cleaned text, LaTeX, table, list items, type, label and reasons). Undone values stay in
  `revisions`, and an undone element is not reviewed again.
- **What the sheet shows.** `confirmed` (tick) and `changed` (pencil) next to the element, counts in the document
  head, and in the source panel the outcome, the model's note, the earlier reading and Undo. A confirmed element
  shows no doubt mark, although a confirmation lowers the stored label only one step (high stays at medium in
  the JSON). An unreadable region stays boxed even when confirmed. A changed element keeps a doubt mark, because
  the two passes disagreed. A doubtful element without a verdict says "Not reviewed" in its panel.
- **Failures.** Whatever goes wrong (agy error, timeout, reply in the wrong format, no answer for some crops), the
  page stays accepted and is not read again; it shows "Not reviewed" with the reason. Rate limiting or an exhausted
  quota during a review also pauses the reader and halves its concurrency as for a page read, and the other pages
  of that document are not reviewed, so the remaining quota goes to reading. There is no retry.
- **Limits and Stop.** The review runs in the page's worker and keeps its slot, so "Pages at once" still caps the agy
  processes, and a page's next neighbour waits for it. Stop kills the review's agy too; the page stays accepted.

#### Measured (2026-11-04, agy 1.4.12, `gemini-3.8-flash`)

Three pages of a real handwritten physics notebook, read through the app at low effort (83.3k tokens). This is a small
sample, and whether a verdict was right is one person's judgement from looking at each crop.

| | Elements flagged / checked | Reading | Added at **medium** (default) | Added at low |
|---|---|---|---|---|
| Page A, graded poor | 13 / 8 (the cap; 2 types are not reviewable) | 38.0k tokens | **45.3k tokens, 139 s** | 33.4k, 55 s |
| Page B | 0 / 0 | 22.6k tokens | nothing | nothing |
| Page C | 2 / 2 | 22.7k tokens | **34.7k tokens, 129 s** | 30.9k-42.2k, 32-37 s |

| Verdicts | Corrections right | Corrections wrong | Confirmations | Other | Failures |
|---|---|---|---|---|---|
| Medium, final prompt (10 elements) | 0 | 0 | 9: 8 right (one lax about layout), 1 missed a wrong letter | 1 not settled (the model doubted its confirmation) | 0 |
| Low, 3 tries on page C (6 verdicts) and 1 on page A (8) | 0 | 2 | 10: all right (one lax about layout) | 2 "unreadable" on readable text | 0 |

At low effort the same two elements got three different results in three tries, including one correction that replaced
a word with one that made more sense in the sentence but was not what was written, and one that added crossed-out
marks to an uncrossed word. The prompt was tightened after those; on the third try the verdict was "unreadable" for
both. Medium effort thought for 14-15k tokens per call, and did not change anything in 10 elements.

So the second look stays off by default: on a page with doubtful elements it more than doubles the tokens and adds about
two minutes, it made no correct correction in this sample, and a confirmation removes a doubt mark that was sometimes
justified. Page A's flags came from the page's quality grade rather than from specific doubts, and the 8 readings checked
there were right. Elements beyond the cap of 8 stay marked and unreviewed.

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
  runs.py      background runs (read / chat / resume), event stream, the agy page loop with retries
  review.py    second look at doubtful elements: one agy call per page, checks on a correction, undo, what the page shows
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

- When the app is killed (not stopped) during a read, an `agy` process it started is not killed with it and may run
  until it ends or times out on its own. Nothing reads its answer, so the page is read again on resume, and the tokens
  of that attempt are not counted in the message.
- A dense page can still take minutes at medium effort (254 s for the chemistry page in 0.3.0). Pages are read
  three at a time by default, which is gentler on the quota than it sounds only if the quota allows it: the app
  slows down by itself when Antigravity reports rate limiting.
- Redraw of a detailed drawing takes several minutes at medium effort and may not pass the fidelity check.
- Chat context through agy is limited by the command-line length (about 18,000 characters of extracted
  content); longer documents are trimmed and the answer is told so. Key engines get up to 120,000 characters.
- The second look runs only for pages read in that run. A page that comes from the cache, or a document read before
  it was switched on, is not reviewed, and there is no button to review it later. A page marked "Not reviewed" stays so.
- The second look has checks for form, not for truth: a well-formed wrong correction is applied. It stays marked
  "changed" with a doubt mark, and can be undone.
- One user, one machine. There is no login; anyone who can use this computer's browser session can use the app.
