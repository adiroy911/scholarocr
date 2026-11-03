# Changelog

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
