# Roadmap and open items

## Not yet verified (do first)
0. **Host mode with live clients.** Antigravity CLI ran the `get_page_image` -> `submit_page_content` loop on one scanned page on 2026-11-03. Still to run: Antigravity on multi-page documents, a math page whose LaTeX is rejected and resubmitted, and a long document (context use); Claude Code and ChatGPT (developer mode; the page-image widget path in CHATGPT_APP.md 1.5 is unverified). Measure host-mode accuracy against key mode on the same pages.
1. **Live Gemini run.** Benchmarks ran live on 2026-11-02 (`benchmarks/results/`); re-run the opt-in live tests after changes. Run `SCHOLAROCR_RUN_LIVE_TESTS=1 pytest tests/live -m live` and `python benchmarks/run_benchmark.py --live`. Then check the default model IDs and the extraction quality on real scans.
2. **Windows.** All code paths are cross-platform: pathlib, `os.pathsep`, process checks via `OpenProcess`, and Windows font paths. None of it was executed on Windows. Test installation, the MCP server under Claude Code, and the PowerShell examples.
3. **Antigravity plugin distribution.** After pushing, run the real `agy plugin install https://github.com/adiroy911/scholarocr` (only local installs of the root layout were tested). Replace the placeholder `assets/logo.svg` and submit through the Marketplace interest form.
4. **Real-document benchmarks.** First measurement on 2026-11-04: 8 printed pages of CBSE sample papers (maths, physics, chemistry, Hindi), rasterised from digital PDFs and read through Antigravity at low effort, against a reference checked by an AI agent (QUALITY_ASSURANCE.md). Quote those figures only with that sample and those caveats. Still to do: a reference checked by a human, real scans and phone photos, pages with figures and tables, and handwriting, textbooks and research papers, none of which has been measured.

5. **Word equations in Word itself.** The pandoc path was checked on 2026-11-04 by reading the docx XML (native `m:oMath`), not by opening the file in Word. How Hindi text inside an equation renders is unchecked.

## Planned improvements
- Tell the page reader not to repair misprints and to keep the marks column out of sentences: both happened on the measured exam pages (QUALITY_ASSURANCE.md), and the uncertainty labels did not flag them.
- Detect 180°/270° orientation deterministically (today it relies on a one-time model re-extraction).
- Cross-page structure: questions continued across pages, multi-page tables, and a document-level section tree with element ranges.
- Per-character fallback fonts in searchable-PDF text layers.
- Isolate each LaTeX equation at compile time, so one bad expression can't fail the whole compile.
- DOCX ingestion with page geometry (via LibreOffice conversion when it is installed).
- An MCP resource for page images, and progress notifications for long jobs.
- MCP sampling as an opportunistic host-mode backend, if a target client ever implements it (hook: `ScholarOCR.process(model_override=...)`).
- Host mode: let the client transcribe a dense page in halves (top/bottom crops) with boxes mapped back.
- Optional cost caps per job, based on the pricing file.
- Integration tests for the HTTP transport.
- Perspective correction for angled phone photos (today: axis-aligned crop of the detected page only).
