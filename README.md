# ScholarOCR

A reusable academic-document OCR and intelligence engine for AI agents. You extract a document once
into a structured, source-traceable representation, then retrieve, verify and export it in any
format without running OCR again. Visual OCR uses the **Google Gemini API** (`google-genai`).
Agents connect through an **MCP server** or a **CLI**, and a **Claude Code skill** teaches Claude
how to use it.

```
Claude Code · Gemini CLI · other MCP clients · scripts
        │ MCP (stdio/HTTP)          │ CLI          │ Python
        └────────────► ScholarOCR service ◄────────┘
     ingestion → routing → (text layer | Gemini visual OCR) → IR checkpoints
     → verification → exports (JSON, Markdown, searchable PDF, DOCX, LaTeX, CSV/XLSX)
```

## What it does

- **Per-page hybrid extraction.** Reliable PDF text layers are extracted deterministically with no
  model call. Scans, photos, math-font pages and image-heavy pages go to Gemini with a profile
  (general, math, or handwriting). Screenshots and photos are cropped to the document region
  (app/screen chrome removed), and dark-mode notes are handled. Blank pages are skipped. Duplicate pages are flagged, never
  dropped.
- **Academic structure.**
  - LaTeX for equations and mhchem for chemistry.
  - Tables with spans; figures and diagrams with crops, labels and descriptions.
  - Questions, sub-questions and marks.
  - Headings, captions, footnotes and bibliography entries.
- **Traceability.** Every element has page, source file, bounding box and a source crop.
  `get_source_region` returns the original pixels.
- **Honest uncertainty.** Labels are ordinal (low/medium/high/unresolved). Unreadable regions are
  marked, never guessed. Flagged elements get a targeted review by a second model.
- **Operations.**
  - Page-level checkpoints and a content-hash cache.
  - Background jobs with cancel, resume and crash recovery.
  - Retries, rate limiting and usage tracking.
  - Path allow-listing and retention.

## Status

| Area | State |
|---|---|
| Core engine, IR, storage, ingestion, preprocessing, routing, jobs, verification | implemented, tested |
| Gemini integration (official SDK) | implemented; tested with mocked responses. **Live API not yet exercised** (no key available); opt-in live tests are provided |
| Exports: JSON, Markdown, searchable PDF, DOCX, LaTeX, CSV, XLSX | implemented, tested (LaTeX compiled with XeLaTeX; DOCX native equations via pandoc) |
| MCP server (11 tools) | implemented; tested in-process and over a real stdio subprocess |
| CLI | implemented; tested via subprocess |
| Claude Code skill | written, installed and tested with headless Claude Code 2.1.316 (Linux) |
| Gemini CLI | configuration documented from official docs; not tested |
| Windows | written for Windows (paths, separators, process checks) but **not executed on Windows** |
| Accuracy on real scans | **not measured yet**; evaluation framework ready |

Tests: `129 passed, 3 skipped (live Gemini, opt-in), 0 failed`. See docs/QUALITY_ASSURANCE.md.

## Quick start

```bash
git clone https://github.com/adiroy911/scholarocr && cd scholarocr
python -m venv .venv && . .venv/bin/activate     # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env                              # set GEMINI_API_KEY, SCHOLAROCR_ALLOWED_DIRS
scholarocr process path/to/paper.pdf --type exam-paper
scholarocr content <document_id>
scholarocr export <document_id> --format searchable-pdf
```

Connect Claude Code:

```bash
python scripts/install_skill.py
claude mcp add --scope user scholarocr -e SCHOLAROCR_ALLOWED_DIRS="<folders>" -e GEMINI_API_KEY="<key>" -- "<venv>/bin/scholarocr-mcp"
```

Examples: `examples/01_python_api.py`, `examples/02_cli_workflow.sh`, `examples/03_mcp_client.py`,
and config snippets `examples/claude_code.mcp.json` and `examples/gemini_cli.settings.json`.

## Documentation

| Doc | Contents |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | layers, data flow, threading, decisions |
| [INSTALLATION](docs/INSTALLATION.md) | Windows / Linux / macOS setup |
| [CONFIGURATION](docs/CONFIGURATION.md) | every environment variable |
| [GEMINI_INTEGRATION](docs/GEMINI_INTEGRATION.md) | SDK usage, routing, prompts, retries, usage, verification status |
| [MCP_INTEGRATION](docs/MCP_INTEGRATION.md) | tools, Claude Code, Gemini CLI, shared HTTP server |
| [CLAUDE_CODE_SKILL](docs/CLAUDE_CODE_SKILL.md) | skill content, install, test |
| [CLI_REFERENCE](docs/CLI_REFERENCE.md) | commands and exit codes |
| [DOCUMENT_SCHEMA](docs/DOCUMENT_SCHEMA.md) | the universal IR and storage layout |
| [EXPORT_FORMATS](docs/EXPORT_FORMATS.md) | formats, options, verification, limitations |
| [QUALITY_ASSURANCE](docs/QUALITY_ASSURANCE.md) | uncertainty rules, quality metrics, verification, evaluation, tests |
| [SECURITY](docs/SECURITY.md) | file access, secrets, deletion, network |
| [TROUBLESHOOTING](docs/TROUBLESHOOTING.md) | common problems |
| [ROADMAP](docs/ROADMAP.md) | open items, starting with live Gemini and Windows validation |

## Gemini CLI extension

The Gemini CLI extension lives in `extensions/gemini-cli/` and ships as a GitHub release asset.
Install the server, then the extension:

```bash
uv tool install "git+https://github.com/adiroy911/scholarocr"
gemini extensions install https://github.com/adiroy911/scholarocr
```

Details, keys and the Antigravity CLI plugin: [extensions/gemini-cli/README.md](extensions/gemini-cli/README.md).

## Licence

MIT, see [LICENSE](LICENSE). The DejaVu test fonts in `tests/fixtures/fonts/` keep their own licence
(`LICENSE_DEJAVU`).
