# Changelog

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
