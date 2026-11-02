# Roadmap and open items

## Not yet verified (do first)
1. **Live Gemini run.** No API key was available during development. Run `SCHOLAROCR_RUN_LIVE_TESTS=1 pytest tests/live -m live` and `python benchmarks/run_benchmark.py --live`. Then check the default model IDs and the extraction quality on real scans.
2. **Windows.** All code paths are cross-platform: pathlib, `os.pathsep`, process checks via `OpenProcess`, and Windows font paths. None of it was executed on Windows. Test installation, the MCP server under Claude Code, and the PowerShell examples.
3. **Gemini CLI.** Its configuration is documented from the official docs but has not been tested.
4. **Real-document benchmarks.** Build manually verified ground truth (`scholarocr evaluate`) for real exam papers, handwriting, textbooks and research papers. Until that exists, no accuracy figures should be quoted.

## Planned improvements
- Detect 180°/270° orientation deterministically (today it relies on a one-time model re-extraction).
- Cross-page structure: questions continued across pages, multi-page tables, and a document-level section tree with element ranges.
- Per-character fallback fonts in searchable-PDF text layers.
- Isolate each LaTeX equation at compile time, so one bad expression can't fail the whole compile.
- DOCX ingestion with page geometry (via LibreOffice conversion when it is installed).
- An MCP resource for page images, and progress notifications for long jobs.
- Optional cost caps per job, based on the pricing file.
- Integration tests for the HTTP transport.
- Perspective correction for angled phone photos (today: axis-aligned crop of the detected page only).
