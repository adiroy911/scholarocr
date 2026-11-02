# Output formats

| Need | Format | Notes |
|---|---|---|
| Hand data to another agent/program | `json` | Full IR, including uncertainty and source references |
| Read/search/summarize | `markdown` | `$$…$$` math, GFM tables, figure crops in `assets/`, `⟦unclear: …⟧` markers |
| Keep the original look but make it searchable | `searchable-pdf` | Original pages unchanged + invisible text layer; `verification.overall_recall` reports how much IR text is extractable |
| Editable document | `docx` | Native Word equations when pandoc is installed (else LaTeX source + warning); tables, figure crops, page markers |
| Typesetting | `latex` | XeLaTeX; compiled automatically when an engine exists (`verification.compiled`); `options.main_font` (e.g. "DejaVu Serif") — the default is Latin Modern; `verification.fonts_in_pdf` shows the real fonts |
| Tables only | `xlsx` / `csv` | One sheet/file per table; error if the document has no tables |

Options (via `options={...}`): markdown `include_furniture` (headers/footers), docx `page_markers`,
latex `main_font`, `compile`. Exports are written under the data directory
(`documents/<id>/exports/<format>/`) unless the CLI `--out-dir` is used.

Always read `warnings` and `verification` from the result and tell the user about anything relevant
(e.g. LaTeX failed to compile, missing glyphs, low text-layer recall).
