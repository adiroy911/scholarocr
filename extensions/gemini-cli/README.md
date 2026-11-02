# ScholarOCR extension for Gemini CLI

Gives Gemini CLI the ScholarOCR MCP server: source-traceable OCR for scanned PDFs, page photos,
handwritten notes, exam papers, textbooks and research papers, with equations as LaTeX, tables,
ordinal uncertainty labels, a quality check (`verify_document`) and exports (Markdown, searchable
PDF, DOCX, LaTeX, CSV, XLSX).

The extension ships:

| File | Purpose |
|---|---|
| `gemini-extension.json` | Manifest. Starts `scholarocr-mcp` over stdio and declares the API-key settings. |
| `GEMINI.md` | Context loaded every session: when to use each tool, uncertainty rules, typical flow, errors. |
| `commands/ocr.toml` | `/ocr <path> [pages or hints]`: extract a document and list what was unreadable. |
| `commands/ocr/verify.toml` | `/ocr:verify <document>`: run checks and compare uncertain elements with the source image. |
| `commands/ocr/export.toml` | `/ocr:export <document> <format>`: write a file from the stored extraction. |
| `commands/ocr/list.toml` | `/ocr:list [filter]`: list documents already processed. |
| `antigravity-plugin/` | The same server, context and commands packaged as an Antigravity CLI plugin (see below). |

## Gemini CLI or Antigravity CLI?

On 18 June 2026 Google stopped serving Gemini CLI for Google AI Pro/Ultra subscribers and free Gemini
Code Assist users and moved them to Antigravity CLI (`agy`). Gemini CLI still works with a paid Gemini
API key or a Gemini Code Assist Standard/Enterprise licence, and it is still released. If you sign in
with a Google account on a consumer plan, use the Antigravity plugin in `antigravity-plugin/`
instead, or import this extension with `agy plugin import gemini`.

## 1. Install the ScholarOCR server

The extension does not bundle Python code. It runs the `scholarocr-mcp` command, so that command must
be on your `PATH`. Pick one:

```bash
# from GitHub
uv tool install "git+https://github.com/adiroy911/scholarocr"
# pipx works the same way
pipx install "git+https://github.com/adiroy911/scholarocr"
# or from a local checkout (quote paths that contain spaces)
uv tool install "path/to/scholarocr"
```

Then check that the right executable is found first:

```bash
where scholarocr-mcp        # Windows
which scholarocr-mcp        # macOS / Linux
```

If an older `scholarocr-mcp` from another Python install comes first, remove it (for example
`py -3.14 -m pip uninstall scholarocr`) or put `~/.local/bin` (uv) earlier on `PATH`.

### Alternative: uvx, no install step

`uvx` builds and caches the package on first use. To use it, edit the `command` and `args` of the
`scholarocr` server in your copy of `gemini-extension.json` (easiest with `gemini extensions link`,
below):

```json
"command": "uvx",
"args": ["--from", "git+https://github.com/adiroy911/scholarocr", "scholarocr-mcp"]
```

For a local checkout use `"args": ["--from", "path/to/scholarocr", "scholarocr-mcp"]`.
uvx is slower to start. On the development machine the first build took more than 4 minutes, and
later starts from the cache took about a minute before the server answered. `uv tool install` is the
better choice for daily use.

## 2. Install the extension

```bash
# from GitHub: installs the archive attached to the latest release
gemini extensions install https://github.com/adiroy911/scholarocr
# pin a release
gemini extensions install https://github.com/adiroy911/scholarocr --ref v0.1.0
# or, while developing, link the local folder so edits apply at once
gemini extensions link "path/to/scholarocr/extensions/gemini-cli"
```

The repository root is the Python package, not the extension. `gemini extensions install` with a
GitHub URL first downloads the latest GitHub release and uses its archive asset, whose root holds
this folder's files (`gemini-extension.json`, `GEMINI.md`, `commands/`). Only if the repo has no
release does it `git clone` the repo, and a clone has no `gemini-extension.json` at its root, so
that path fails. Keep a release with the archive attached (see section 5).

Gemini CLI only starts stdio MCP servers in trusted folders. Trust the folder you work in when
prompted, or run `gemini trust` there.

## 3. Configure keys

Gemini CLI removes environment variables whose names contain `KEY`, `TOKEN`, `SECRET` and similar
from MCP server processes, and a `"$GEMINI_API_KEY"` reference in an `env` block expands from that
filtered environment, so it comes out empty. A key exported in your shell therefore does **not**
reach ScholarOCR. Give it the key one of two ways:

1. **Extension settings (recommended).** At install time Gemini CLI asks for:
   - `GEMINI_API_KEY`: used when the provider is `gemini` (the default);
   - `OPENROUTER_API_KEY`: only when the provider is `openrouter`;
   - `SCHOLAROCR_PROVIDER`: `gemini` or `openrouter`; leave empty for `gemini`.

   Press Enter to skip the ones you don't use. Keys are stored in the system keychain. Change them
   later with `gemini extensions config scholarocr` (add `--scope workspace` for one project).
2. **A `.env` file in the folder you start Gemini CLI from.** The server runs with that folder as its
   working directory and reads `.env` from there (`GEMINI_API_KEY=...`, `SCHOLAROCR_PROVIDER=...`).
   Gemini CLI also reads that `.env`, so it shares the key for its own requests.

Non-secret variables pass through from your shell unchanged. The useful ones:

| Variable | Default | Notes |
|---|---|---|
| `SCHOLAROCR_ALLOWED_DIRS` | the folder Gemini CLI was started in | Folders the server may read. `;`-separated on Windows, `:` elsewhere. |
| `SCHOLAROCR_DATA_DIR` | `%LOCALAPPDATA%\ScholarOCR` / `~/.local/share/scholarocr` | Set it to share one document store across clients. |
| `GEMINI_MODEL`, `GEMINI_REVIEW_MODEL` | `gemini-3.8-flash` | Model ids for the gemini provider. |
| `OPENROUTER_MODEL` | `nvidia/nemotron-3.5-super-vl:free` | Model id for the openrouter provider. |
| `GEMINI_RPM_LIMIT` | `30` | Lower it on a free key. |

The full list is in `docs/CONFIGURATION.md` of the ScholarOCR repo.

## 4. Use it

```text
/ocr "scans/chem-exam.pdf" 1-4 exam_paper
/ocr:verify chem-exam
/ocr:export chem-exam docx
/ocr:list
```

Or ask in plain language ("read the handwritten notes in notes/ and list the equations"); the
context file tells Gemini when to call ScholarOCR. Check the server is connected with `/mcp`.

Model calls happen only in `ocr_document` (scanned pages, figures), `classify_document`, and
`verify_document(review=true)`. Listing, reading, verifying without review and exporting are free.
Re-running OCR on unchanged pages uses the cache.

## 5. Releases and the Gemini CLI extensions gallery

The gallery at geminicli.com/extensions is self-serve: it crawls public GitHub repos with the topic
`gemini-cli-extension` daily and lists those whose `gemini-extension.json` (at the repo root or at
the root of a release archive) validates. For this repo:

- `adiroy911/scholarocr` is public, MIT-licensed, and has the topic `gemini-cli-extension`.
- `gemini-extension.json` is deliberately **not** at the repo root; otherwise installing the
  extension would install the whole Python repo. It ships in the release asset
  `scholarocr-gemini-extension.tar.gz`, whose root is the contents of this folder plus `LICENSE`
  (`antigravity-plugin/` is left out).
- Gemini CLI prefers `{platform}.{arch}.*` and `{platform}.*` assets, and uses a generic asset only
  if it is the **only** asset on the release. Attach exactly one file.
- The release tag must match `version` in the manifest (`v0.1.0` for `0.1.0`).

To cut a release:

```bash
# bump version in pyproject.toml, gemini-extension.json and antigravity-plugin/plugin.json first
python scripts/build_gemini_extension_archive.py --tag v0.2.0
gh release create v0.2.0 dist-extension/scholarocr-gemini-extension.tar.gz --title v0.2.0 --notes "..."
```

Before releasing, run `gemini extensions validate .` in this folder if Gemini CLI is installed.

## Antigravity CLI plugin

`antigravity-plugin/` holds the same integration in Antigravity's plugin layout: `plugin.json`,
`mcp_config.json` (server plus `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `SCHOLAROCR_PROVIDER`
variables), `rules/AGENTS.md` (the context file) and three skills (`ocr`, `ocr-verify`,
`ocr-export`; Antigravity has no TOML commands).

```bash
agy plugin validate --strict "path/to/scholarocr/extensions/gemini-cli/antigravity-plugin"
agy plugin install "path/to/scholarocr/extensions/gemini-cli/antigravity-plugin"
```

The plugin stays off until you fill in its variables. Listing on the Antigravity Marketplace goes
through Google's interest form on antigravity.google/docs/marketplace. It requires `displayName`,
`description`, `version`, `logo` and `author`, all present. `author` is set to adiroy911;
`assets/logo.svg` is still a placeholder logo and should be replaced before a Marketplace submission.
