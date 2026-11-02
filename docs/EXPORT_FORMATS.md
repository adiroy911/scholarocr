# Export formats

Exports read only the stored IR, preserved sources and saved crops; they never call Gemini. Each
returns `primary_file`, `files`, `warnings` and format-specific `verification`. Default output:
`<data_dir>/documents/<id>/exports/<format>/`.

| Format | File | What is preserved | Post-generation verification |
|---|---|---|---|
| `json` | `document.json` | full IR | re-parsed with the schema; element count |
| `markdown` | `document.md` + `assets/` | page markers, heading levels, `$$` math, GFM tables (spans: text in the first cell), lists, figure crops with descriptions, captions, footnotes, question numbers/marks, `⟦unclear: …⟧` for unreadable content; optional `<!-- el:ID uncertainty=… -->` anchors (used by the MCP content tool) | page markers, broken image links, unclear-marker count |
| `searchable-pdf` | `document.searchable.pdf` | original PDF pages copied unchanged; image pages rebuilt at original size from the preserved image; invisible text (render mode 3) fitted into each model-extracted element's box; elements already present in an existing PDF text layer are not duplicated | page count, page sizes (±1 pt), appearance (PDF pages pixel-identical at 36 dpi; image pages contain the original image), per-page and overall token recall of the IR text, `text_extractable` |
| `docx` | `document.docx` | Heading 1-3, paragraphs, lists, native tables with merges, figure crops (never whole-page images), captions, footnotes, "Page N" markers, equations as **native Word (OMML) equations via one pandoc call**; LaTeX source fallback (with warning) when pandoc is missing or an equation fails | reopened; counts of headings, tables, images, native/fallback equations |
| `latex` | `document.tex` + `assets/` | sections from heading levels, display math, invalid math isolated as verbatim text with a visible marker, tables, figures with captions, bibliography, source-page comments, uncertain text marked | XeLaTeX compile (no shell escape, 120 s timeout): `compiled`, `log_tail`, `pdf`, fonts actually embedded (`fonts_in_pdf`, `main_font_verified`), `missing_characters` |
| `csv` | `table_<page>_<n>.csv` | one UTF-8 file per table | reopened; cell counts |
| `xlsx` | `tables.xlsx` | Index sheet + one sheet per table; merged cells, bold header rows, uncertain cells highlighted with a comment | reopened; cell counts |

## Options

- markdown: `include_furniture` (headers/footers/page numbers; default off)
- docx: `page_markers` (default on)
- latex: `main_font` (passed to fontspec; default none ⇒ Latin Modern, reported as such), `compile` (default on)

## Fonts and limitations

- The LaTeX default font (Latin Modern) lacks Cyrillic and some math symbols in text; missing
  characters are reported. Use `main_font="DejaVu Serif"` (or any installed font with the glyphs);
  the exporter never claims a font that the compiled PDF does not contain.
- hyperref is used only when `hyperref.sty` and `pzdr.tfm` are installed; mhchem only when
  `mhchem.sty` is installed (otherwise `\ce{}` is approximated with `\mathrm`, with a warning).
- A LaTeX expression that passes the static checks but is still invalid TeX fails the whole compile;
  this is reported with the log tail.
- The searchable-PDF text layer uses one font; characters it lacks are listed in a warning.
- DOCX sources produce no page images, so they cannot be included in searchable PDFs.
- CSV/XLSX raise an error when the document has no tables.
