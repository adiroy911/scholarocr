# Installation

For Antigravity CLI you only need the two commands in "Antigravity CLI" below; the rest of this page
is for a development checkout, the CLI and other MCP clients.

Requirements: Python 3.11+ (developed and tested on 3.12 and 3.13), ~600 MB disk for dependencies.
Optional: **pandoc** (native Word equations in DOCX), **TeX Live / MiKTeX with XeLaTeX** (LaTeX
compile verification), a **Gemini API key** (visual OCR of scanned pages, classification, review).

> Tested on Linux (Python 3.13). The Windows steps below follow the same commands but were not
> executed in a Windows environment during development.

## Windows (PowerShell)

```powershell
# 1. Get the code from GitHub
git clone https://github.com/adiroy911/scholarocr ; cd scholarocr

# 2. Create a virtual environment and install
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"

# 3. Configure (copy and edit)
Copy-Item .env.example .env
notepad .env        # optional GEMINI_API_KEY (no key = host mode), SCHOLAROCR_ALLOWED_DIRS (use ; between folders), SCHOLAROCR_DATA_DIR

# 4. Check
scholarocr info
python -m pytest -q
```

Optional tools: `winget install JohnMacFarlane.Pandoc` and MiKTeX or TeX Live (XeLaTeX).
`.env` is read from the current working directory; for the MCP server prefer setting variables in
the MCP client configuration (see MCP_INTEGRATION.md).

## Linux / macOS

```bash
cd scholarocr
python3 -m venv .venv && . .venv/bin/activate      # or: uv venv && uv pip install -e ".[dev]"
pip install -e ".[dev]"
cp .env.example .env && $EDITOR .env
scholarocr info && python -m pytest -q
```

Optional: `apt install pandoc texlive-xetex texlive-latex-recommended texlive-latex-extra`
(`texlive-fonts-recommended` enables hyperref; `texlive-science` provides mhchem for chemistry).

## Antigravity CLI

```bash
uv tool install "git+https://github.com/adiroy911/scholarocr"   # or: uv tool install "path/to/checkout"
agy plugin install https://github.com/adiroy911/scholarocr
```

Then set `SCHOLAROCR_ALLOWED_DIRS` in `%LOCALAPPDATA%\ScholarOCR\scholarocr.env`
(`~/.local/share/scholarocr/scholarocr.env` on macOS/Linux). No key is needed. To install the plugin
from a checkout instead, stage it with `python scripts/stage_antigravity_plugin.py` and run
`agy plugin install build/antigravity-plugin` (installing the checkout folder itself would copy
`.venv/` and `.env` into the plugin). See ANTIGRAVITY_PLUGIN.md.

## Claude Code integration

```bash
python scripts/install_skill.py            # copies claude-skill/scholarocr to ~/.claude/skills/scholarocr
claude mcp add --scope user scholarocr -e SCHOLAROCR_ALLOWED_DIRS="C:\Users\you\Documents" -- "<path>\.venv\Scripts\scholarocr-mcp.exe"
```

See MCP_INTEGRATION.md and CLAUDE_CODE_SKILL.md.

## Uninstall

Delete the virtual environment, `~/.claude/skills/scholarocr`, the MCP registration
(`claude mcp remove scholarocr`) and, if wanted, the data directory (`scholarocr info` shows it).
For Antigravity: `agy plugin uninstall scholarocr` and `uv tool uninstall scholarocr`.
