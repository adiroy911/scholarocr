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
# To allow another folder later, APPEND it after a ';' - replacing the value revokes the others.
SCHOLAROCR_ALLOWED_DIRS=C:\Users\you\Documents;D:\Study

# Optional: ScholarOCR's own OCR pipeline instead of Antigravity's model.
# SCHOLAROCR_PROVIDER=gemini
# GEMINI_API_KEY=...
```

Keys are optional. Without `SCHOLAROCR_PROVIDER` the server stays in host mode even when a key is set
(see CONFIGURATION.md for `auto`, `openrouter` and every other variable). Restart Antigravity after
editing the file. An `access_denied` error names the settings file actually in use (it respects
`SCHOLAROCR_ENV_FILE`) and shows the current `SCHOLAROCR_ALLOWED_DIRS` value with the new folder appended.

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
`~/.gemini/antigravity-cli/mcp/scholarocr_scholarocr/`. The model reads those files before it calls a
tool through `call_mcp_tool`. In interactive use you approve those reads and the tool calls when prompted.
The rules tell the model never to run shell commands (`dir`, `Get-ChildItem`, ...) or to read the plugin's,
the server's or other documents' files to learn formats; it should use `get_page_image(instructions="full")`
and say in `notes` what it could not encode.

### Headless runs (`agy -p`)

A print-mode run cannot ask for approval, so pass permission rules in a `--settings` file. The repository
ships a recommended one, [`antigravity/headless-settings.example.json`](../antigravity/headless-settings.example.json)
(outside `skills/` and `rules/`, so it is not part of the installed plugin). Replace `<you>` with your user
name (forward slashes work on Windows):

```json
{
  "permissions": {
    "allow": [
      "mcp(scholarocr_scholarocr/*)",
      "mcp(scholarocr/*)",
      "read_file(C:/Users/<you>/.gemini/config/plugins/scholarocr)",
      "read_file(C:/Users/<you>/.gemini/antigravity-cli/mcp)"
    ],
    "deny": [
      "command(regex:.*)"
    ]
  }
}
```

```bash
agy -p "Extract D:/Study/page1.png with ScholarOCR and verify it" --settings headless-settings.json --output-format stream-json
```

**Why the explicit `deny` rule matters** (agy 1.4.12, measured 2026-11-03). Precedence is deny > ask >
allow. A command that matches no rule falls to "ask", which headless mode cannot show; agy refuses it,
lists it in `denied_actions`, and the whole turn ends with an EMPTY `response`. That happened in 4 of 10
test runs, each time after the model tried `dir` or `Get-ChildItem` on the plugin folder. A command that
matches an explicit `deny` rule is different: agy returns an error to the model ("Permission denied for
unsandboxed(dir ...). Matches user-configured deny rule."), `denied_actions` stays empty, and the model
carries on. In the test run (prompt: run `dir` on the plugin folder, then call `list_documents`) the model
reported the refusal and finished the ScholarOCR call: 40.6k tokens, 39 s.

Pages are read from the image `get_page_image` returns. Agy's working folder is part of its workspace, so a
model may open an input file in that folder directly; the rules tell it not to (the server's image is
cropped and deskewed and the boxes refer to it). In one test the model read an original greyscale JPEG
from the workspace and the model API answered `INVALID_ARGUMENT (code 400)`. Run headless jobs from a
folder that does not contain the inputs.

`--mode auto` may be unavailable on some accounts (agy then prints "--mode auto is not available for
this account" and continues in the default mode).

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

## Re-verified after the 0.3.0 fixes (2026-11-03)

Same setup (agy 1.4.12, `gemini-3.8-flash-medium`, no key), headless with the settings file above, one run
per case. Logs: `agy-test-results/2026-11-03-fixes/` next to the checkout.

| Case | Before (0.2.0) | After |
|---|---|---|
| Chemistry worksheet (`tests/fixtures` corpus `chemistry.png`) | 3 of 3 runs ended empty after the model went looking for docs with shell commands | Passed: 4 elements, mhchem stored exactly, no shell attempt, 128.5k tokens, 161 s |
| arXiv page image (`arxiv_2205.00096_p3.png`, 150 dpi) | Routed as "blank", nothing to transcribe | Passed: `visual_general`, 21 elements, inline math as `$...$` LaTeX, 121.1k tokens, 168 s |
| Handwritten manuscript (`majorana.jpg`) | 36 of 38 elements "medium": no signal | Model labels 26 low, 8 medium, 1 high, 1 unresolved, every non-low one with a reason; 212k tokens, 307 s |
| Deny-rule experiment | (no deny rule: turn ended empty) | Error returned to the model, turn completed, 40.6k tokens, 39 s |

In the handwriting run the server's "handwriting on a `fair` page" rule still raised 34 of the 36 stored labels
to `medium` (the page was graded `fair` for 11.6 px glyphs). That rule was removed afterwards; replaying the
same submission through the current rules stores the model's labels unchanged. The worksheet examples in the
brief mirror the worksheet's own content, so that pass shows the model no longer went hunting; it is not an
independent accuracy test. The model also ran three web searches about the manuscript before transcribing;
the rules now forbid web searches.

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
