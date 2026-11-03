# Changelog

## 0.4.0 (2026-11-03)

- **Reading with Antigravity is about 7 times faster** (web app). Measured on pages 1-4 of a real handwritten
  notebook (`gemini-3.8-flash-medium` selected): 483 s and 223.6k tokens before, 70 s and 130.0k tokens now; every
  page accepted on the first attempt in both runs.
  - Several pages at once: Settings "Pages at once" (1-6, default 3), one agy process each, with one limit shared
    by every document the app reads. Progress says "Reading pages 4-6 of 12"; pages appear as each finishes and
    are stored by page number. Stop kills every agy process of the run.
  - Pipelining: reading starts as soon as page 1 is prepared, while the job prepares the rest (first page call
    after 1.2 s instead of 12 s). Box refinement, figure crops and cleaned text run in the page's worker right
    after it is accepted.
  - Throttling: when agy reports a rate limit or an exhausted quota (`429`, `RESOURCE_EXHAUSTED`, quota, rate
    limit, `503`), or two pages in a row fail, the reader pauses (20 s, doubling), halves its concurrency for the
    rest of the run and reads those pages again (at most 3 times).
  - Effort: Settings "Effort for reading pages" (default Low) and "Effort for questions and Redraw" (default
    Medium). agy refuses `--model gemini-3.8-flash-medium --effort low` as a conflict, so the app passes the
    model family with `--effort` when `agy models` lists that variant. Low thinks 0 tokens instead of 6-12k per
    page; compared with the earlier transcription it dropped one phrase on one of four pages (8 of 276 prose words
    missing, against 6 for the old run and 4 for a medium run), so Medium remains one click away.
  - Fewer model calls: an image cannot be attached to the first message in headless mode (`--input-format
    stream-json` takes only `text` blocks; `@path` passes only the path). Instead each page uses a workspace
    agent (`--agent scholarocr-reader`, written into the run folder) with only `read_file` and `finish`, told to
    return the JSON through `finish`: two model calls with a short system prompt instead of three with agy's
    ~12k-token prompt (a probe: 39.1k -> 16.8k input tokens; medium pages: about 43k -> 21k). Unknown to agy:
    falls back to the default agent.
  - A compact page brief for the app (2.6k instead of 6.4k characters): the JSON schema already defines the
    fields, element types and doubt levels. `host_page_image(instructions="none")` returns the route, hints
    and text layer for a caller that writes its own brief.
- **Layout PDF: one font size.** Instead of fitting each element to its own box (paragraphs came out at different
  sizes), one body size is chosen per document from the paragraphs' fitted sizes (the size at which three in four
  fit; the median over pages, a clearly different page keeps its own) and every paragraph is set at it; headings,
  captions, page numbers and equations at fixed ratios. A box too small for its text first extends right (one-line
  boxes, equations) and down into free paper, checking the next element; only then is the text shrunk, down to a
  floor (equations 0.9x the body), with a warning. Punctuation touching inline math and a leading bullet stay on
  the math's line. `layout_fidelity` now reports `body_size` per page, `shared_size_share` (also as `fit_share`),
  `moved` (grown boxes), `shrunk`, `overflowed` and `fitted_to_box`. On the real pages 1-4: one size (13.6 pt) on
  pages 2-4, 20 pt on the landscape page 1; 34 of 37 text elements at the shared size, 3 shrunk by 3-8 %, none
  overflowing; before, 68-81 % per page were at the page's common size.
- **Update layout for documents read before refinement existed**: `ScholarOCR.refine_document()` now also adds
  cleaned text with the deterministic rule normaliser where an element has none (no model call) and reports
  totals; `layout_outdated()` says whether a document needs it (pages carry `layout_refined_at`). Web app: an
  "Older reading" row with **Update layout** on such documents (`POST /api/documents/{id}/refine`); MCP:
  `update_layout(document_id, pages)`; CLI: `scholarocr refine DOC [--pages R] [--no-clean-text]`.
- Box refinement: the "Thermal resistance" element on a real page (an arrow whose head sits under the equation
  above, then the words) did keep its words; the refined boxes overlap and the review overlay drew the label in
  the shared band. A regression test on a synthetic page checks the words stay in the text's box, out of the
  equation's, and survive the layout PDF's overlap split.
- Redraw through Antigravity: 10-minute timeout per call (a real 3D slab took 4-5 minutes per attempt at medium
  effort), and when the retry fails (timeout, quota) the first attempt's verdict is returned instead of an error.
  The response lists each attempt's fidelity (`history`). Real test (3D slab, page 3): attempt 1 recall 0.90,
  precision 0.89, failed on misplaced regions; attempt 2 recall 0.84, stored as not faithful; 105k tokens. The
  thresholds were left as they are: the rejected copies were visibly displaced.
- Web app design fixes: AA contrast everywhere (white on the dark-theme red Delete button; the muted ink tokens in
  both themes, checked in a browser on every visible text), padding in the drop zone and dialogs, no clipping
  container around positioned layers, no card inside a card (the Settings switches and the figure blocks are
  flat), doubtful equations marked with a tint or dotted outline instead of a side stripe, a steady "working"
  dot in history instead of a pulsing one, and progress bars that scale instead of animating width.
- Windows: replacing a stored file retries briefly when another thread has it open.

- **Layout PDF export (`layout-pdf`).** A typeset remake of the document: each page at the original size and
  orientation, every element (headings, paragraphs, equations, tables, lists, figures, page numbers, question
  numbers and marks) placed at its box. Text is set at one body size per document (see above), equations are
  typeset with the vendored KaTeX and mhchem, Hindi is set in Noto Serif Devanagari, and figures use the cleaned
  crop (or the original crop, the redrawn SVG, a crop cut from the page image, or a described placeholder).
  Options `text_variant`, `figures`, `background`, `show_boxes`, `pages`, `renderer`. Overlapping boxes are
  nudged apart; text that cannot fit overflows downward with a warning. Verification adds `layout_fidelity`
  (share set at the target size, shrunk and overflowed elements, words found inside their boxes). Printed by a
  headless Chromium-family browser (Edge, Chrome, Chromium, Brave, Playwright's Chromium or
  `SCHOLAROCR_BROWSER`), with a basic PyMuPDF fallback. Available in the CLI, MCP `export_document` (local and
  remote) and the web app's export links. The existing `searchable-pdf` is unchanged.
- **Boxes snapped to the ink.** After a page's elements are stored (host submissions and key-provider
  extractions), every `model_estimate` box is refined deterministically (`pipeline/layout_refine.py`, no model):
  the page is binarised relative to its paper level after dividing out shadows, ruled notebook lines and margin
  rules are found (straight seeds, a robust curve fit, then tracing to follow slant and page curl; faint dotted
  rules too) and removed while strokes crossing them are kept, and each box is fitted to the ink components it
  owns. A component goes to one element only (best pixel share, then depth inside the box, then reading order,
  with ink covered by overlapping boxes going to the element whose own ink is nearest). Figures grow along
  their strokes and nearby labels (`T_A`, `dx`, `θ₁`) but not into another element's ink or into a text line, and
  drawings that touch are split between their figures. The model box is kept when refinement is unreliable (no
  ink, growth past a cap, ink belonging to a larger structure). Refined boxes have `bbox_source: "refined"` and
  keep the model's box in `bbox_model`. `ScholarOCR.refine_document()` re-runs it on stored pages.
- **Figure images.** Every figure, diagram or image gets a full-resolution crop
  (`pages/NNNN/figures/<id>.png`, `source.region_image`) and, on scanned pages, a cleaned version
  (`<id>.clean.png`, `figure.image_clean`): paper and shadows whitened, ruled lines removed, only the figure's own
  strokes kept in their colour, contrast stretched, trimmed. Markdown writes the images next to the `.md` with
  relative links, Word inserts them inline at the same share of the text width as on the page, LaTeX uses
  `\includegraphics`, JSON carries the paths; option `figure_variant` = `clean` (default) | `original` |
  `redrawn`. The web app shows the cleaned image with the description as alt text and caption, and a toggle to
  the original crop.
- **Redraw as vector (opt-in).** A faithful SVG copy of a figure, "without changing a thing": the brief asks for
  the crop's pixel coordinates as the viewBox and forbids added, removed, straightened or corrected content. The
  SVG is sanitised strictly (allowlisted elements and attributes; no scripts, event handlers, styles, links,
  images, `foreignObject` or external references) and then checked deterministically against the crop
  (`pipeline/svg_fidelity.py`): rasterised at the crop size with PyMuPDF and compared both ways with a distance
  tolerance (recall: original ink the copy covers; precision: copy ink the original supports), any sizeable
  missing or extra region fails on its own, and every `figure.labels` entry must appear as SVG text. A copy that
  fails gets one retry with specific feedback (where strokes are missing or extra, which labels are absent); a
  second failure is stored but marked not faithful and is never shown or exported instead of the crop. Stored as
  `figure.svg` with `figure.svg_fidelity`. Web app: Redraw button per figure (Antigravity or a key engine), the
  score next to "Redrawn". MCP: `redraw_figure` (crop + instructions) and `submit_figure_svg` for host mode.
- **Cleaned text.** Text elements get `clean_content`: casing, punctuation and spacing slips fixed and obvious
  slips of the pen corrected (`temperture` -> `temperature`), never other words, numbers, symbols or math.
  `content` stays exactly as written. The model proposes it in the same call (`clean_text`, host prompt
  `host-2026-11-03.4`, extraction prompt `2026-11-03.1`); a deterministic guard accepts it only if math, LaTeX,
  numbers, symbols and bracketed markup are identical and the words are the same (spacing, a capitalised word's
  case, or a one- or two-letter slip in a long word), otherwise a rule normaliser is used
  (`clean_content_source`: `model` | `rules`). Exports, MCP `get_document_content` / `export_document`
  (`text_variant`: `clean` | `faithful`) and the web app ("Cleaned / As written" per document, default in
  Settings) choose the variant: cleaned by default for handwritten documents, as written for printed ones.
- Host rules: rough boxes are fine (the server snaps them), still estimated by eye and never with code.

## 0.3.0 (2026-11-03)

- **Web app (`scholarocr-app`).** A local chat-style interface on 127.0.0.1: attach PDFs, page photos or DOCX
  (several photos = one document), add per-message and saved instructions, and get the structured result per page
  (KaTeX math, mhchem chemistry, tables, figure descriptions, question numbers and marks), doubtful and unreadable
  parts highlighted with the source crop on click, verification, one-click exports, and follow-up questions
  answered from the extracted content with page citations. History is stored locally. Light and dark themes,
  phone-width layout, keyboard support. Install with the new `webapp` extra (docs/WEB_APP.md).
- **Antigravity engine without a key.** The app runs the pipeline in host mode and reads each waiting page with
  one headless `agy -p` call (per-run `--settings` that only allows reading the page image folder,
  `--json-schema` with the page schema). Replies go through `submit_page_content`, and rejections are re-prompted
  with the validator's errors (at most twice). There is a per-page timeout and Stop. Measured on a 2-page scan:
  50.6k and 111.2k tokens per page, 66 s and 254 s.
- Optional Gemini / OpenRouter keys for the app, stored in the OS keychain (`keyring`) or the user-level
  `scholarocr.env`, never shown again.
- Fix: several `ScholarOCR` services in one process sharing a database (the web app keeps one per engine) no
  longer mark each other's running jobs as interrupted (`JobManager` tracks in-process jobs process-wide).

Fixes from the 2026-11-03 Antigravity test runs (host prompt version `host-2026-11-03.3`, so host-transcribed
pages are prepared again on the next `ocr_document`):

- **Clean digital pages were skipped as "blank".** Blank detection counted pixels darker than 128 on a 400 px
  thumbnail, where the thin strokes of a 150 dpi arXiv page are anti-aliased to light grey (ink 0.0002). Ink is
  now measured relative to the paper level, and a page is blank only when the thumbnail and the page at its own
  resolution (PDFs: a 100 dpi render) agree. A page flagged blank by an older manifest is still assessed at full
  resolution before it is skipped. `force_visual=true` never takes the blank route, `reprocess=true` re-measures
  the manifest, and `ocr_document` warns about every page skipped as blank (`blank_pages`).
- **Doubt semantics.** The page-level field is now `page_doubt` (overall DOUBT, `low` for a clean page);
  `legibility` is still accepted as its old name, with a warning. Page-level signals no longer raise element
  labels: `page_doubt` is a page warning, and a page graded `fair` no longer puts every handwritten element at
  `medium` (`poor` still does). Host rules, the web app's page prompt, `rules/AGENTS.md` and the skills explain
  each label (`low` = you would bet the transcription is exact, `medium` = a specific character or word could
  differ, `high` = a guess among alternatives, `unresolved` = cannot read) and ask for per-element judgements.
  Elements accept `doubt` as an alias of `uncertainty` and `none` for `low`.
- **No shell commands, no plugin-file hunting.** Host rules and `rules/AGENTS.md` forbid shell commands, reading
  the plugin's, server's or other documents' files to learn formats, web searches, and opening the original
  file instead of the returned page image. The model is told to use the examples, pick the closest element
  type and explain in `notes`, and to tell the user when a page comes back blank but seems to have content.
- **Recommended headless settings** in `antigravity/headless-settings.example.json`: allow rules for the
  ScholarOCR tools plus an explicit `deny` for commands. Measured: a command that matches no rule ends a headless
  turn with an empty answer; one that matches a `deny` rule returns an error to the model and the turn continues
  (docs/ANTIGRAVITY_PLUGIN.md).
- **Chemistry examples in the element shape** (a question with `$\ce{H2 + O2 -> H2O}$`, a `chemical_equation`
  with `\ce{N2 + 3H2 <=> 2NH3}`, an ion `\ce{SO4^2-}`). The examples are valid JSON.
- **Malformed `elements`** (`[0, 1, ...]`, or the array sent as a JSON string) reach the validator in both the
  local and the remote MCP server and get its path / problem / fix list instead of a raw pydantic error.
- **Chemistry checks** accept `=`, `<-`, `<=>`, `<=>>`, `<<=>`, `⟶` and other arrows as reaction signs. A
  `submit_review(verdict="confirmed")` is no longer re-raised by the rule that flagged the element.
- The text-layer note says not to copy Unicode math from the text layer and to write inline math as `$...$`.
- **DOCX export**: no "Q4 4. If ..." double numbering; inline `$...$` math becomes native Word math with pandoc
  and readable text (`x²`, `γ₀`, `SO₄²⁻`) without it. pandoc stays optional (docs/INSTALLATION.md; the web app's
  Word export link explains it in a tooltip).
- **Smaller outputs**: `verify_document` reports pages outside the last page selection as one `info` finding;
  `get_page_content(compact=true)` or `fields=[...]` returns a fraction of the full record. AGENTS.md says to pass
  a finding's `element_id` straight to `get_source_region`.
- Host rule 9: a continuation of a question uses `parent_index` and carries no question number.
- `access_denied` names the settings file in use (respecting `SCHOLAROCR_ENV_FILE`), shows the current
  `SCHOLAROCR_ALLOWED_DIRS` value with the folder appended, and says to append with `;` rather than replace.
- Re-verified with real `agy -p` runs (`gemini-3.8-flash-medium`): the chemistry worksheet that failed 3 of 3
  times passed (128.5k tokens, 161 s); the arXiv page image was transcribed instead of skipped (21 elements,
  121.1k tokens, 168 s); on the handwritten Majorana page the model's labels varied (26 low, 8 medium, 1 high,
  1 unresolved, each non-low one with a reason; 212k tokens, 307 s; the fair-page rule that still raised 34 of
  them to medium in that run was removed afterwards, and a replay of the same submission keeps the model's
  labels); the deny-rule experiment completed instead of ending empty (40.6k tokens, 39 s).

## 0.2.0 (2026-11-03)

- **Host mode is the default and needs no API key.** The MCP client's own model transcribes pages that
  need vision: `get_page_image` returns one processed page with the rules and the element shape,
  `submit_page_content` validates the transcription strictly (every problem with its path and a fix)
  and stores it, and `submit_review` handles a second look at flagged elements. `SCHOLAROCR_PROVIDER`
  decides the mode (unset = `host`; `gemini`, `openrouter` or `auto` use a key). Keys are read from the
  environment, a working-directory `.env`, or the user-level `scholarocr.env` (`SCHOLAROCR_ENV_FILE`).
- **Antigravity CLI is the agent integration.** The repository root is now an Antigravity plugin
  (`.antigravity-plugin/plugin.json`, `mcp_config.json`, `rules/AGENTS.md`, skills `ocr`, `ocr-verify`,
  `ocr-export`), installed with `agy plugin install https://github.com/adiroy911/scholarocr`.
  `scripts/stage_antigravity_plugin.py` stages a clean copy for local installs. Verified live on one
  scanned page with no key (docs/ANTIGRAVITY_PLUGIN.md).
- **Gemini CLI support was removed** (the extension, its commands and its release-archive script);
  Google moved subscription and free users to Antigravity CLI in June 2026.
- **ChatGPT app with optional keys.** The hosted server (`scholarocr-remote`) works without a key in
  host mode; a user can link their own Gemini or OpenRouter key and unlink it later (docs/CHATGPT_APP.md).
- The Claude Code skill moved from `skills/scholarocr/` to `claude-skill/scholarocr/`, because every
  folder in the root `skills/` is loaded as an Antigravity skill. `scripts/install_skill.py` installs it
  as before.

## 0.1.0 (2026-11-02)

- Initial public release: engine, IR, exports, local MCP server, CLI, Gemini and OpenRouter providers,
  Claude Code skill.
