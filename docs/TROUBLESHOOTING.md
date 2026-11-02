# Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `access_denied` / exit 4 | File is outside `SCHOLAROCR_ALLOWED_DIRS`. Add its folder (`;`-separated on Windows) to the MCP server's `env` and restart the client. |
| Antigravity: `access_denied` for every file | The plugin's server starts in the plugin folder, which is then the only allowed folder. Set `SCHOLAROCR_ALLOWED_DIRS` in `%LOCALAPPDATA%\ScholarOCR\scholarocr.env` (or `~/.local/share/scholarocr/scholarocr.env`, or the file named by `SCHOLAROCR_ENV_FILE`) and restart `agy`. |
| Antigravity: the ScholarOCR server does not start | `scholarocr-mcp` is not on `PATH`, or an older copy comes first. Run `where scholarocr-mcp` (`which` on macOS/Linux); install with `uv tool install "git+https://github.com/adiroy911/scholarocr"`. `agy plugin list` should show 1 MCP server. |
| Antigravity: `agy -p` produces no output, "headless mode cannot prompt" | Print mode cannot approve the model's reads of the tool descriptor files or the tool calls. Pass the allow rules from ANTIGRAVITY_PLUGIN.md with `--settings`. |
| Antigravity: the model lists or reads files under `.gemini/antigravity-cli/mcp` | Expected: that is where Antigravity puts the tool descriptors (`scholarocr_scholarocr/*.json`). Approve the reads. |
| CLI: scanned pages end as `awaiting_transcription` (exit 6) | No key is configured, so the server is in host mode and the CLI has no model to read them. Use ScholarOCR from an MCP client (its model transcribes via `get_page_image` / `submit_page_content`) or set `GEMINI_API_KEY` / `OPENROUTER_API_KEY`. |
| MCP: the model says it cannot see the page image | The client does not pass tool-result images to its model (ChatGPT: see CHATGPT_APP.md section 1.5). Configure a key for server-side OCR. |
| `submission_rejected` repeats | The model keeps sending the same mistake. Each error has `path`, `problem` and `fix`; a LaTeX error is accepted once the element is marked `high`. |
| `model_not_found` | The model ID isn't available to your key. Check https://ai.google.dev/gemini-api/docs/models and set `GEMINI_MODEL` / `GEMINI_REVIEW_MODEL`. |
| `rate_limited` errors / slow jobs | Lower `GEMINI_RPM_LIMIT` and `SCHOLAROCR_CONCURRENCY`, or raise your quota. Resume partial jobs with `scholarocr job resume JOB` / `manage_job`. |
| `response_validation_failed` on a page | The model returned invalid JSON twice. The page is marked failed (nothing is accepted silently). Resume the job, or retry the page with `--reprocess --pages N`. |
| Job stuck in `running` after a crash | Any status call marks jobs from dead processes `interrupted`. Then resume them. |
| "Document … is already being processed by another job" | Another process holds the document lock. Poll its job. Stale locks (no heartbeat for 2 minutes) are taken over automatically. |
| Unexpected `blank` page | A page counts as blank only with almost no dark pixels (< 0.04% at low resolution). Check `manifest.json` notes. Very faint scans may need rescanning. |
| Wrong reading order on complex layouts | Text-layer pages use XY-cut ordering. Use `--force-visual` to let the model order the page. |
| DOCX equations appear as LaTeX text | pandoc isn't installed or isn't on PATH. Install it and re-export (no OCR rerun). |
| LaTeX export says "compiled: false" | See `verification.log_tail`. Install XeLaTeX (TeX Live / MiKTeX). Set `main_font` for non-Latin scripts. |
| Searchable PDF warns about missing glyphs | Set `SCHOLAROCR_TEXT_LAYER_FONT` to a TTF covering your script (e.g. Noto Sans Devanagari). |
| MCP server doesn't appear in Claude Code | Run `claude mcp list`. Check that the command path points to `scholarocr-mcp(.exe)` inside the venv. Run that command manually: it should wait silently on stdin. |
| Output looks stale after changing prompts/models | Cache keys include models, prompt and schema versions. Changed settings reprocess automatically, and `--reprocess` forces it. |
