# Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `access_denied` / exit 4 | File is outside `SCHOLAROCR_ALLOWED_DIRS`. Add its folder (`;`-separated on Windows) to the MCP server's `env` and restart the client. |
| Scanned pages fail with "needs visual OCR but GEMINI_API_KEY is not configured" (exit 6) | Set `GEMINI_API_KEY` in the environment of the process running ScholarOCR (for MCP: the client's server `env`). Text-layer pages still work. |
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
