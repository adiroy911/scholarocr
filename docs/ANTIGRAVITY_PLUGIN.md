# Antigravity plugin

Antigravity CLI (`agy`) is ScholarOCR's agent integration for people who use their own Google
account. Google moved Google AI subscribers and free users to Antigravity CLI in June 2026, and the
plugin needs no API key: Antigravity's model reads the page images (host mode) and ScholarOCR does
everything else. A Gemini or OpenRouter key is optional.

The plugin packages the existing stdio MCP server (`scholarocr-mcp`) with a rule file and three
skills. It contains no Python code; the server is installed separately.

## Install

1. Install the server so that `scholarocr-mcp` is on `PATH`:

   ```bash
   uv tool install "git+https://github.com/adiroy911/scholarocr"
   # pipx works the same way: pipx install "git+https://github.com/adiroy911/scholarocr"
   where scholarocr-mcp      # Windows; macOS/Linux: which scholarocr-mcp
   ```

   If an older `scholarocr-mcp` from another Python install comes first on `PATH`, remove it or put
   uv's tool folder (`~/.local/bin`) earlier.

2. Install the plugin from GitHub:

   ```bash
   agy plugin install https://github.com/adiroy911/scholarocr
   agy plugin list            # scholarocr (enabled): 3 skills, 1 rule, 1 MCP server
   ```

   Local equivalent, from a checkout: `agy plugin install <folder>` copies the whole folder, including
   `.venv/`, `.git/` and any `.env` with keys, so stage the plugin files first:

   ```bash
   python scripts/stage_antigravity_plugin.py          # -> build/antigravity-plugin
   agy plugin validate --strict build/antigravity-plugin
   agy plugin install build/antigravity-plugin
   ```

To update, run the same install again (or `agy plugin uninstall scholarocr` first). To remove it:
`agy plugin uninstall scholarocr`, then `uv tool uninstall scholarocr`.

### Optional: uvx instead of an installed server

The plugin runs `scholarocr-mcp` from `PATH`. To run it with uvx instead, edit `mcp_config.json` in the
installed plugin (`~/.gemini/config/plugins/scholarocr/`):

```json
"command": "uvx",
"args": ["--from", "git+https://github.com/adiroy911/scholarocr", "scholarocr-mcp"]
```

uvx builds the package on first use (more than 4 minutes on the development machine) and later starts
from the cache in about a minute, so `uv tool install` is better for daily use.

## Configure

Antigravity starts plugin MCP servers in the plugin's own folder, not in your workspace. So the server's
working-directory `.env` is of no use, and its default allowed folder is the plugin folder. Put your
settings in the user-level file instead:

- Windows: `%LOCALAPPDATA%\ScholarOCR\scholarocr.env`
- macOS/Linux: `~/.local/share/scholarocr/scholarocr.env`
- or any file named by `SCHOLAROCR_ENV_FILE`

```
# Folders the server may read (; on Windows, : elsewhere). Needed: the default is the plugin folder.
SCHOLAROCR_ALLOWED_DIRS=C:\Users\you\Documents;D:\Study

# Optional: ScholarOCR's own OCR pipeline instead of Antigravity's model.
# SCHOLAROCR_PROVIDER=gemini
# GEMINI_API_KEY=...
```

Keys are optional. Without `SCHOLAROCR_PROVIDER` the server stays in host mode even when a key is set
(see CONFIGURATION.md for `auto`, `openrouter` and every other variable). Restart Antigravity after
editing the file.

`mcp_config.json` declares no variables on purpose: Antigravity keeps a plugin whose MCP server declares
variables disabled until they are filled in, and the plugin should work at once in host mode.

## Use it

Ask in plain language ("read the scanned exam in D:\Study\chem and list anything unreadable"), or name a
skill:

| Skill | Does |
|---|---|
| `ocr` | Extract a file, folder or list of page images; run the host-mode loop; report unreadable regions. |
| `ocr-verify` | Run `verify_document` and compare uncertain elements with the source crop. |
| `ocr-export` | Write Markdown, searchable PDF, DOCX, LaTeX, CSV, XLSX or JSON from the stored extraction. |

`rules/AGENTS.md` is loaded in every session. It explains the two modes, the host-mode loop
(`get_page_image` -> transcribe -> `submit_page_content`, optional `submit_review`), the tools, the
uncertainty rules and the error codes.

In host mode the server makes no model calls; transcription uses Antigravity's model and its context,
about one page image plus the elements per page. With a key, model calls happen only in `ocr_document`
(scanned pages, figures), `classify_document` and `verify_document(review=true)`.

## How Antigravity exposes the tools

Bundled MCP tools are namespaced `<plugin-name>_<server-name>`; for this plugin agy reported the server
as `scholarocr_scholarocr`. Antigravity writes one descriptor file per tool, plus `instructions.md`, to
`~/.gemini/antigravity-cli/mcp/scholarocr_scholarocr/`. The model reads those files (sometimes through
a shell `dir` / `Get-ChildItem`) before it calls a tool through `call_mcp_tool`. In interactive use you
approve those reads and the tool calls when prompted.

### Headless runs (`agy -p`)

A print-mode run cannot ask for approval, so pass allow rules in a `--settings` file. With your home
folder in place of `<home>` (forward slashes work on Windows):

```json
{
  "permissions": {
    "allow": [
      "mcp(scholarocr_scholarocr/*)",
      "read_file(<home>/.gemini/antigravity-cli/mcp)",
      "read_file(<home>/.gemini/config/plugins/scholarocr)",
      "command(regex:(dir|ls|Get-ChildItem|gci|Get-Content|gc|type|cat) .*antigravity-cli.mcp.*)"
    ]
  }
}
```

```bash
agy -p "Extract D:/Study/page1.png with ScholarOCR and verify it" --settings agy-settings.json --output-format stream-json
```

Without the rules a headless run stops with "a tool required the ... permission that headless mode
cannot prompt for". `--mode auto` may be unavailable on some accounts (agy then prints "--mode auto is
not available for this account" and continues in the default mode).

## Verified (2026-11-03)

agy 1.4.12 on Windows 11, plugin installed, `gemini-3.8-flash-medium`, no key configured, headless with
the settings file above. Input: a synthetic scanned page (one image).

- The model called `ocr_document`, which returned `mode: "host"` with the page awaiting transcription,
  then `get_page_image`, transcribed the page and called `submit_page_content`. The submission was
  accepted on the first try. `verify_document` passed.
- The stored text matched the page exactly.
- The run used about 116k tokens, most of them cached.

Not tested yet:

- multi-page documents (the `next_waiting_page` loop over many pages);
- a math page where a LaTeX error makes the server reject the submission and the model resubmits;
- long documents and how much of the context window they use.

## Plugin layout

The repository root is the plugin root, so the plain GitHub URL installs it:

```
.antigravity-plugin/plugin.json   manifest (name scholarocr, version, logo, author, suggested prompts)
mcp_config.json                   the scholarocr server: scholarocr-mcp over stdio, no variables
rules/AGENTS.md                   always-on rule: modes, host-mode loop, tools, uncertainty, errors
skills/ocr|ocr-verify|ocr-export  the three skills
assets/logo.svg                   logo (placeholder)
```

How agy resolves it (checked against agy 1.4.12 and antigravity.google/docs, 2026-11-03):

- A plugin directory has `plugin.json` at its root or, for repositories that also ship
  `.claude-plugin/`, at `.antigravity-plugin/plugin.json` (which takes precedence).
- Components (`skills/`, `rules/`, `mcp_config.json`, `agents/`, `hooks.json`, `bin/`) and the `logo`
  path resolve from the plugin root, the folder that contains `.antigravity-plugin/`, not from
  `.antigravity-plugin/` itself. `agy plugin validate --strict` warns when the logo sits inside
  `.antigravity-plugin/`.
- `agy plugin install https://github.com/<owner>/<repo>` clones the repo and uses its root. A subfolder
  can be named as `https://github.com/<owner>/<repo>/tree/<ref>/<path>` (or `.../<repo>/<path>`); agy
  reports `subpath "..." not found in repository` or `could not detect plugin structure` otherwise.
- A local install copies the whole source folder into `~/.gemini/config/plugins/<name>/` (tested). A
  GitHub install clones to a temporary folder and is expected to do the same with the committed repo
  (not yet run against this repo), so `src/`, `tests/` and `docs/` sit in the plugin folder as inert
  files: nothing loads them, and only the components above are plugin content. This is why the Claude
  Code skill lives in `claude-skill/`, not `skills/`: anything in `skills/` becomes an Antigravity skill.

## Antigravity Marketplace

Listing goes through Google's partner interest form linked from antigravity.google/docs/marketplace,
after `agy plugin validate --strict` passes with no warnings. The manifest has the fields the Marketplace
needs (`name`, `displayName`, `description`, `version`, `logo`, `author`). Before submitting:

- replace `assets/logo.svg`, which is still a placeholder;
- keep the logo out of dot-folders and `__pycache__`, which the Marketplace bundle leaves out.

Once listed, users install it with `agy plugin install scholarocr@antigravity-plugins-official` or from
`/plugin`.
