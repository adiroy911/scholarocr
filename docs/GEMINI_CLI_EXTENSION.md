# Gemini CLI extension (and Antigravity plugin)

ScholarOCR ships a Gemini CLI extension in `extensions/gemini-cli/`. It packages the existing MCP
server (`scholarocr-mcp`, stdio) with a context file and slash commands. User-facing install and
gallery steps are in `extensions/gemini-cli/README.md`; this page records the design decisions.

## Layout

```
extensions/gemini-cli/
  gemini-extension.json      manifest: mcpServers.scholarocr + settings (API keys, provider)
  GEMINI.md                  context file (contextFileName)
  commands/ocr.toml          /ocr <path> [pages|hints]
  commands/ocr/verify.toml   /ocr:verify <document>
  commands/ocr/export.toml   /ocr:export <document> <format>
  commands/ocr/list.toml     /ocr:list [filter]
  README.md
  antigravity-plugin/        same integration as an Antigravity CLI plugin
    plugin.json  mcp_config.json  rules/AGENTS.md  skills/{ocr,ocr-verify,ocr-export}/SKILL.md  assets/logo.svg
```

## Status of Gemini CLI (checked 2026-11-02)

- Google's post "An important update: Transitioning Gemini CLI to Antigravity CLI" (19 May 2026)
  says Gemini CLI stopped serving Google AI Pro/Ultra and free Code Assist users on 18 June 2026.
  Code Assist Standard/Enterprise users and users of paid Gemini API keys keep Gemini CLI.
  Extensions carry over as "Antigravity plugins".
- geminicli.com still carries a banner saying it was replaced for unpaid and Google One users. The
  repo `google-gemini/gemini-cli` is still released (v0.66.0, 27 Oct 2026), and the docs for
  extensions, the gallery and `gemini extensions validate` are still current.
- Antigravity CLI (`agy`) imports installed Gemini extensions with `agy plugin import gemini`
  (TOML commands become skills, MCP servers move to `mcp_config.json`). A native plugin is also
  provided in `antigravity-plugin/`.

## How keys reach the server

Gemini CLI (`packages/core/src/tools/mcp-client.ts`, `services/environmentSanitization.ts`) builds a
stdio server's environment like this:

1. `process.env` plus the extension's resolved `settings` values;
2. names matching `TOKEN|SECRET|PASSWORD|KEY|AUTH|CREDENTIAL|...` and values that look like Google
   API keys (`AIzaSy...`) are removed;
3. settings values are added back, so a `GEMINI_API_KEY` setting reaches the server;
4. `env` entries in the server config are expanded **against the filtered environment**, so
   `"GEMINI_API_KEY": "$GEMINI_API_KEY"` becomes an empty string.

So the manifest declares `GEMINI_API_KEY` and `OPENROUTER_API_KEY` as sensitive `settings`
(stored in the keychain) plus `SCHOLAROCR_PROVIDER`, and has no `env` block. Empty settings are
not stored and set nothing, so a `.env` in the workspace still works: the server runs with
`cwd: ${workspacePath}`, and `config.py` reads `.env` from the working directory. Non-secret
variables (`SCHOLAROCR_ALLOWED_DIRS`, `SCHOLAROCR_DATA_DIR`, model ids) pass through from the
shell.

The same filtering means a `"GEMINI_API_KEY": "$GEMINI_API_KEY"` entry probably yields an empty key in
current Gemini CLI releases (not tested against a running Gemini CLI). `examples/gemini_cli.settings.json`
and the Gemini section of `MCP_INTEGRATION.md` used that pattern; since 2026-11-02 they set `cwd` to a
folder whose `.env` holds the key instead, and `load_dotenv` treats an empty variable as unset so a
blank key from the client no longer hides the `.env` value.

## Working directory and allowed folders

`cwd` is `${workspacePath}`, the folder Gemini CLI runs in. ScholarOCR's default
`SCHOLAROCR_ALLOWED_DIRS` is the working directory, so files in the workspace are readable without
configuration, and files elsewhere need `SCHOLAROCR_ALLOWED_DIRS`.

## Launch command

The manifest runs `scholarocr-mcp` from `PATH` (`uv tool install` / `pipx install`). A published
manifest cannot hold a user-specific path, and uvx needs a published source, so the uvx form
(`uvx --from git+https://github.com/adiroy911/scholarocr scholarocr-mcp`) is documented as an edit to
the user's linked copy.

## Verification done

- JSON and TOML parse; an offline re-implementation of `gemini extensions validate` (context file
  present, semver, server transport, no `trust`, settings shape) passes. Gemini CLI itself was not
  installed, so the real `validate`/`link` commands were not run.
- `agy plugin validate --strict --json` (agy 1.4.12) on `antigravity-plugin/`: 0 errors, 0 warnings,
  1 note (the plugin stays off until its variables are filled in).
- The server started over stdio from the repo `.venv`, with sensitive-looking variables stripped as
  Gemini CLI does, and `list_tools` returned all 11 tools. The same check passed with
  `uvx --offline --from <checkout> scholarocr-mcp` (about 64 s from the uv cache). No tool was called
  and no model API was used.
- On the development machine a stale `scholarocr-mcp.exe` (a Python 3.14 editable install pointing
  at a deleted checkout) comes first on `PATH` and fails with `ModuleNotFoundError`. Install with
  `uv tool install` and check `where scholarocr-mcp` before using the extension.

## Distribution (decided 2026-11-03)

The repo `adiroy911/scholarocr` holds the whole project, so `gemini-extension.json` stays in
`extensions/gemini-cli/` instead of the repo root (a root manifest would make Gemini CLI install the
whole Python repo as the extension). The extension is distributed as a GitHub release asset instead,
following https://geminicli.com/docs/extensions/releasing/:

- `scripts/build_gemini_extension_archive.py` packs the contents of `extensions/gemini-cli/`
  (without `antigravity-plugin/`) plus `LICENSE` into `dist-extension/scholarocr-gemini-extension.tar.gz`,
  with `gemini-extension.json` at the archive root. It checks that the versions in `pyproject.toml`,
  `gemini-extension.json` and `plugin.json` agree with the tag. The output is byte-reproducible.
- Gemini CLI's `findReleaseAsset` (`packages/cli/src/config/extensions/github.ts`) picks a
  `{platform}.{arch}.` or `{platform}.` prefixed asset first, and a generic asset only when it is the
  only asset on the release. The release therefore carries exactly one asset, whose name has no
  platform word.
- `gemini extensions install https://github.com/adiroy911/scholarocr` (`extension-manager.ts`) tries
  the latest GitHub release first and only falls back to `git clone` when the repo has no release
  (or, after a consent prompt, when the download fails). The clone path would fail validation,
  since the repo root has no manifest, so a release with the asset must always exist. This was
  read from the Gemini CLI source; Gemini CLI itself was not run.
- The gallery crawler accepts a manifest at the root of a release archive and needs the repo topic
  `gemini-cli-extension`.
