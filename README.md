# ScholarOCR

A reusable academic-document OCR and intelligence engine for AI agents. You extract a document once
into a structured, source-traceable representation, then retrieve, verify and export it in any
format without running OCR again. The main agent integration is an **Antigravity CLI plugin**:
install it, sign in with your own Google account, and Antigravity's model reads the pages. Other agents
connect through the same **MCP server** or the **CLI**, and a **Claude Code skill** teaches Claude how to
use it.

**No API key is needed.** By default the model doing the reading is the one in your own chat client
(Antigravity, Claude Code, Claude Desktop, and ChatGPT through the hosted app): the server
does every deterministic step and hands that model one page image at a time (**host mode**). A Gemini or
OpenRouter key is optional and switches to ScholarOCR's dedicated server-side OCR pipeline.

```
Antigravity CLI (plugin) · Claude Code · ChatGPT (hosted) · web app · other MCP clients · scripts
        │ MCP (stdio/HTTP)          │ CLI          │ Python
        └────────────► ScholarOCR service ◄────────┘
     ingestion → routing → text layer (no model)
                         → visual pages: render, crop, deskew →  host mode: your client's model
                                                                    (get_page_image → submit_page_content)
                                                                 key mode: Gemini / OpenRouter
     → validation → IR checkpoints → verification → exports (JSON, Markdown, searchable PDF, DOCX, LaTeX, CSV/XLSX)
```

## What it does

- **Per-page hybrid extraction.** Reliable PDF text layers are extracted deterministically with no
  model call. Scans, photos, math-font pages and image-heavy pages need vision. Screenshots and photos
  are cropped to the document region (app/screen chrome removed), skew and rotation are corrected and
  dark-mode notes are handled before any model sees the page. Blank pages are skipped. Duplicate pages
  are flagged, never dropped.
- **Two ways to read the visual pages.**
  - *Host mode (default)*: the MCP client's model gets one page image with the rules and the exact
    JSON shape, transcribes it, and submits it. The server validates the submission strictly (every
    problem reported with its path and a fix) and stores it like any other extraction.
  - *Key mode*: Gemini or OpenRouter with a profile per page type (general, math, handwriting),
    structured output, and a targeted review of flagged elements by a second model.
- **Academic structure.**
  - LaTeX for equations and mhchem for chemistry.
  - Tables with spans; figures and diagrams with crops, labels and descriptions.
  - Questions, sub-questions and marks.
  - Headings, captions, footnotes and bibliography entries.
- **Traceability.** Every element has page, source file, bounding box and a source crop.
  `get_source_region` returns the original pixels.
- **Honest uncertainty.** Labels are ordinal (low/medium/high/unresolved). Unreadable regions are
  marked, never guessed. LaTeX, tables and legibility are checked deterministically.
- **Operations.**
  - Page-level checkpoints and a content-hash cache.
  - Background jobs with cancel, resume and crash recovery.
  - Retries, rate limiting and usage tracking (key mode).
  - Path allow-listing and retention.

**Quality trade-off.** Host mode is a single pass by a general chat model, at whatever resolution the
client passes to it, and its optional review pass is the same model looking at a crop again. Key mode
uses a dedicated OCR prompt, the PDF text layer as a hint and an independent review model. Validation,
uncertainty rules, traceability and verification are the same in both. Accuracy on real scans has not
been measured for either mode yet.

## Status

| Area | State |
|---|---|
| Core engine, IR, storage, ingestion, preprocessing, routing, jobs, verification | implemented, tested |
| Host mode (client model transcribes, no key) | implemented; tested in-process and over a real stdio subprocess with the MCP SDK client. Run live once in Antigravity CLI (one scanned page, no key, 2026-11-03); not yet run live in Claude or ChatGPT |
| Antigravity plugin | `agy plugin validate --strict` passes (agy 1.4.12). One live headless run with `gemini-3.8-flash-medium` and no key transcribed a scanned page exactly and passed `verify_document` (2026-11-03). Multi-page and LaTeX-resubmission runs not tested yet (docs/ANTIGRAVITY_PLUGIN.md) |
| Gemini and OpenRouter providers (key mode) | implemented; tested with mocked responses; exercised live in benchmark runs (`benchmarks/results/`, 2026-11-02); opt-in live tests in `tests/live` |
| MCP sampling | not used: no target client supports it (see docs/ARCHITECTURE.md); a hook is kept |
| Exports: JSON, Markdown, searchable PDF, DOCX, LaTeX, CSV, XLSX | implemented, tested (LaTeX compiled with XeLaTeX; DOCX native equations via pandoc; both skipped when not installed) |
| Local MCP server (14 tools) | implemented; tested in-process and over a real stdio subprocess |
| Hosted ChatGPT app server (`scholarocr-remote`, OAuth, optional key) | implemented; tested in-process and over ASGI HTTP. **No live ChatGPT test yet**; the page-image widget path for host mode is unverified (docs/CHATGPT_APP.md) |
| CLI | implemented; tested via subprocess |
| Web app (`scholarocr-app`) | implemented; API, engine selection, agy runner (subprocess mocked) and key path (fake transport) tested; driven in a real browser; one live Antigravity run on a 2-page scan (161.8k tokens, 5 min 46 s, docs/WEB_APP.md) |
| Claude Code skill | written; tested with headless Claude Code 2.1.316 (Linux) in key mode |
| Windows | developed and tested on Windows 11; one known test failure (`test_path_policy_blocks_outside_and_traversal`) |
| Accuracy on real scans | **not measured yet**; evaluation framework ready |

Tests: `pytest --ignore=tests/live`, see docs/QUALITY_ASSURANCE.md for the current count.

## Quick start: Antigravity CLI (no key)

```bash
uv tool install "git+https://github.com/adiroy911/scholarocr"     # the server: scholarocr-mcp on PATH
agy plugin install https://github.com/adiroy911/scholarocr         # the plugin
```

Antigravity starts the server in the plugin folder, so list the folders it may read in
`%LOCALAPPDATA%\ScholarOCR\scholarocr.env` (`~/.local/share/scholarocr/scholarocr.env` on macOS/Linux):

```
SCHOLAROCR_ALLOWED_DIRS=C:\Users\you\Documents;D:\Study
```

Start `agy` and ask it to read a document ("extract the scanned exam in D:\Study\chem and list anything
unreadable"). Keys are optional. Details, headless runs and the verified result:
[docs/ANTIGRAVITY_PLUGIN.md](docs/ANTIGRAVITY_PLUGIN.md).

## Quick start: web app (no key)

```bash
pip install -e ".[webapp]"      # from a checkout; see Quick start: CLI and Python
scholarocr-app --open           # http://127.0.0.1:8765/ on this computer only
```

A chat-style page: attach PDFs, page photos or DOCX files, add instructions, and read the result page by page
(math, chemistry, tables, question numbers and marks, doubtful parts highlighted, source crop on click),
with verification, exports and follow-up questions answered from the extracted content. By default the pages are
read by Antigravity CLI (`agy`) signed in with your Google account, one headless `agy -p` call per page; a Gemini
or OpenRouter key saved in Settings switches to the server-side pipeline. Details and measured cost per page:
[docs/WEB_APP.md](docs/WEB_APP.md).

## Quick start: CLI and Python

```bash
git clone https://github.com/adiroy911/scholarocr && cd scholarocr
python -m venv .venv && . .venv/bin/activate     # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
scholarocr process path/to/paper.pdf --type exam-paper   # text-layer PDFs work without any key
scholarocr content <document_id>
scholarocr export <document_id> --format searchable-pdf
```

The CLI has no model of its own: for scans it needs a key (`cp .env.example .env`, set `GEMINI_API_KEY`
or `OPENROUTER_API_KEY`), otherwise it stops with exit code 6 and lists the pages awaiting
transcription. Through MCP, your client's model transcribes them instead.

Connect Claude Code (no key needed):

```bash
python scripts/install_skill.py      # copies claude-skill/scholarocr to ~/.claude/skills
claude mcp add --scope user scholarocr -e SCHOLAROCR_ALLOWED_DIRS="<folders>" -- "<venv>/bin/scholarocr-mcp"
# optional key mode: add -e GEMINI_API_KEY="<key>"
```

Examples: `examples/01_python_api.py`, `examples/02_cli_workflow.sh`, `examples/03_mcp_client.py`,
and the config snippet `examples/claude_code.mcp.json`.

## Documentation

| Doc | Contents |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | layers, data flow, threading, decisions |
| [INSTALLATION](docs/INSTALLATION.md) | Windows / Linux / macOS setup |
| [CONFIGURATION](docs/CONFIGURATION.md) | every environment variable |
| [GEMINI_INTEGRATION](docs/GEMINI_INTEGRATION.md) | key mode: SDK usage, routing, prompts, retries, usage, verification status |
| [ANTIGRAVITY_PLUGIN](docs/ANTIGRAVITY_PLUGIN.md) | the Antigravity CLI plugin: install, config, headless runs, layout, Marketplace |
| [MCP_INTEGRATION](docs/MCP_INTEGRATION.md) | tools, host mode loop, client support, Antigravity, Claude Code, shared HTTP server |
| [WEB_APP](docs/WEB_APP.md) | the local web app: engines, the Antigravity page loop, measured cost, security |
| [CHATGPT_APP](docs/CHATGPT_APP.md) | hosted server for ChatGPT: OAuth, optional key, host mode in ChatGPT, deployment |
| [CLAUDE_CODE_SKILL](docs/CLAUDE_CODE_SKILL.md) | skill content, install, test |
| [CLI_REFERENCE](docs/CLI_REFERENCE.md) | commands and exit codes |
| [DOCUMENT_SCHEMA](docs/DOCUMENT_SCHEMA.md) | the universal IR and storage layout |
| [EXPORT_FORMATS](docs/EXPORT_FORMATS.md) | formats, options, verification, limitations |
| [QUALITY_ASSURANCE](docs/QUALITY_ASSURANCE.md) | uncertainty rules, quality metrics, verification, evaluation, tests |
| [SECURITY](docs/SECURITY.md) | file access, secrets, deletion, network |
| [TROUBLESHOOTING](docs/TROUBLESHOOTING.md) | common problems |
| [ROADMAP](docs/ROADMAP.md) | open items, starting with live Gemini and Windows validation |

## Repository layout

The repository root is also the Antigravity plugin root: `.antigravity-plugin/plugin.json`,
`mcp_config.json`, `rules/`, `skills/` and `assets/` are the plugin. `src/scholarocr/` is the Python
package and `claude-skill/scholarocr/` is the Claude Code skill. Changes are listed in
[CHANGELOG.md](CHANGELOG.md).

## Licence

MIT, see [LICENSE](LICENSE). The DejaVu test fonts in `tests/fixtures/fonts/` keep their own licence
(`LICENSE_DEJAVU`).
