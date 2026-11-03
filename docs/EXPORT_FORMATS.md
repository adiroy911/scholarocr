# Export formats

Exports read only the stored IR, preserved sources and saved crops; they never call Gemini. Each
returns `primary_file`, `files`, `warnings` and format-specific `verification`. Default output:
`<data_dir>/documents/<id>/exports/<format>/`.

| Format | File | What is preserved | Post-generation verification |
|---|---|---|---|
| `json` | `document.json` | full IR | re-parsed with the schema; element count |
| `markdown` | `document.md` + `assets/` | page markers, heading levels, `$$` math, GFM tables (spans: text in the first cell), lists, figure crops with descriptions, captions, footnotes, question numbers/marks, `⟦unclear: …⟧` for unreadable content; optional `<!-- el:ID uncertainty=… -->` anchors (used by the MCP content tool) | page markers, broken image links, unclear-marker count |
| `searchable-pdf` | `document.searchable.pdf` | original PDF pages copied unchanged; image pages rebuilt at original size from the preserved image; invisible text (render mode 3) fitted into each model-extracted element's box; elements already present in an existing PDF text layer are not duplicated | page count, page sizes (±1 pt), appearance (PDF pages pixel-identical at 36 dpi; image pages contain the original image), per-page and overall token recall of the IR text, `text_extractable` |
| `layout-pdf` | `document.layout.pdf` | a typeset remake of each page: same page size and orientation, every element at its box (headings, paragraphs, equations typeset with KaTeX + mhchem, real tables, lists, question numbers and marks, figures, page numbers); one body size for the document (headings, captions and equations at fixed ratios of it); vendored fonts embedded (Source Serif 4, Noto Serif Devanagari, Anek) | page count and sizes (±0.5 pt), `layout_fidelity` (the body size per page, the share of text elements set at the shared size, elements whose box grew into free space (`moved`), shrunk or overflowed, boxes nudged apart, unplaced elements, `text_in_box_recall` of each element's words found inside its (possibly grown) box in the PDF), figure sources, equations typeset vs plain text, fonts in the PDF |
| `docx` | `document.docx` | Heading 1-3, paragraphs, lists, native tables with merges, figure crops (never whole-page images), captions, footnotes, "Page N" markers, equations as **native Word (OMML) equations via one pandoc call**; LaTeX source fallback (with warning) when pandoc is missing or an equation fails | reopened; counts of headings, tables, images, native/fallback equations |
| `latex` | `document.tex` + `assets/` | sections from heading levels, display math, invalid math isolated as verbatim text with a visible marker, tables, figures with captions, bibliography, source-page comments, uncertain text marked | XeLaTeX compile (no shell escape, 120 s timeout): `compiled`, `log_tail`, `pdf`, fonts actually embedded (`fonts_in_pdf`, `main_font_verified`), `missing_characters` |
| `csv` | `table_<page>_<n>.csv` | one UTF-8 file per table | reopened; cell counts |
| `xlsx` | `tables.xlsx` | Index sheet + one sheet per table; merged cells, bold header rows, uncertain cells highlighted with a comment | reopened; cell counts |

## Options

All formats except CSV/XLSX:

- `text_variant`: `clean` (casing, punctuation and spacing slips fixed, from `clean_content`; same words,
  numbers and math) or `faithful` (exactly as written, `content`). Default (`auto`): `clean` for handwritten
  documents (most text elements handwritten), `faithful` for printed ones. JSON always carries both fields; the
  searchable PDF's invisible text layer uses the chosen variant. `verification.text_variant` says which.
- `figure_variant` (markdown, docx, latex): `clean` (default; the cleaned crop `figure.image_clean`), `original`
  (the crop as scanned, `source.region_image`) or `redrawn` (the vector copy `figure.svg`, only when it passed the
  fidelity check; otherwise the cleaned crop is used and a warning says so). Missing files fall back
  svg -> clean -> original. Markdown copies the image files into `assets/` next to `document.md` with relative
  links and the description as alt text and caption; Word inserts them inline at the same share of the text
  width as the figure had of the page width (an SVG is rasterised with PyMuPDF); LaTeX uses
  `\includegraphics` at the same share of `\linewidth` (an SVG is converted to a vector PDF); JSON keeps the
  paths. The searchable PDF is unchanged (it shows the original page image).
- Shared helpers for exporters (`export/base.py`): `element_text(el, variant)`, `figure_image_path(doc_dir, el,
  prefer)` with `prefer` = `clean` | `original` | `svg`.

Per format:

- markdown: `include_furniture` (headers/footers/page numbers; default off)
- docx: `page_markers` (default on)
- latex: `main_font` (passed to fontspec; default none ⇒ Latin Modern, reported as such), `compile` (default on)
- layout-pdf: `text_variant` (`clean` | `faithful`; default `clean` for handwritten documents, `faithful` for
  printed ones), `figures` (`clean` | `original` | `svg`; default `clean`), `background` (`blank` | `paper`, a
  subtle paper tint), `show_boxes` (debug outlines with element id, fit status and size; default off), `pages`
  (e.g. `"1-4"`), `renderer` (`auto` | `chromium` | `pymupdf`), `browser` (path to a Chromium-family browser)

## Layout PDF

`searchable-pdf` keeps the original scan and adds invisible text; `layout-pdf` is a digital remake that looks
like the original page. Each element is placed at its `bbox` (page points, refined to the ink when
`bbox_source` is `refined`; model estimates are approximate and the export says so in a warning).

- **Rendering.** The pages are built as positioned HTML (page size via CSS `@page` in points, each element an
  absolutely positioned box) and printed by a headless Chromium-family browser. Math uses the KaTeX and mhchem
  copies vendored for the web app; fonts are inlined, so nothing is fetched and the browser runs with a
  throwaway profile. The browser runs twice: a layout pass (KaTeX, auto-fit, per-element status read back from
  the DOM) and a print pass of the finished, script-free page. The browser is found in this order: the `browser`
  option, `SCHOLAROCR_BROWSER`, Microsoft Edge, Google Chrome, Chromium, Brave (standard install places, then
  PATH), then a Chromium downloaded by Playwright (`ms-playwright`). Without one, a basic PyMuPDF renderer is
  used with a warning: same positions and auto-fit, equations as readable plain text, MuPDF's built-in fonts.
  `renderer="chromium"` turns the missing browser into an error instead. PyMuPDF alone is not the main renderer
  because it cannot typeset LaTeX (matplotlib's mathtext is not a dependency and has no mhchem or `aligned`).
- **Text.** `element_text(el, variant)`: `clean` uses `clean_content` when present, else `content`. Inline
  `$...$` math is typeset, `~~struck~~` and `[crossed out: …]` are drawn struck through, list items keep their
  bullets, question numbers are added when the text lacks them and marks sit at the right. Fonts: Source Serif 4
  (Latin), Noto Serif Devanagari (Hindi), Anek (page numbers, headers, figure placeholders).
- **Font sizes: one body size.** Handwritten boxes differ in height, so fitting each paragraph to its own box
  gave every paragraph a different size. Instead one body size is chosen: for each page, the largest size at
  which each paragraph fits its box is measured, and the page's size is the one at which three in four
  paragraphs fit (the 25th percentile of those sizes; the refined boxes carry the written line count and ink
  height, so this follows the handwriting's own scale). The document size is the median of the page sizes;
  every page within 20 % of it uses it, and a page that differs clearly (a landscape page in large writing)
  keeps its own (`body_size`, `body_size_scope` per page). The size is between 7 and 20 pt. Every paragraph is
  set at it; titles at 1.5×, headings 1.3×, subheadings 1.15×, captions and footnotes 0.88×, page numbers and
  headers 0.85×, equations 1.05×.
- **When text does not fit** its box at that size: a one-line box (a heading, a short label, an equation) first
  extends to the right into free paper, then any box grows down into free paper as far as the text needs,
  stopping 2 pt before the next element below (or 6 pt before the page edge); such elements are listed in
  `moved` with the new box. A short text that would wrap only because its box is narrow ("• T ≡ T(x, t)") is kept
  on one line the same way. Only when there is no free space is the text shrunk, down to a floor (0.75× the body
  for text, 0.9× for equations, the body size for headings), with a warning and the element in `shrunk`; below
  2 % smaller it still counts as the shared size. Text that does not fit even at the floor overflows downward,
  is listed in `overflowed` and named in a warning. Tables, figure placeholders and labels over a figure are
  still fitted to their own box (never above the body size; `fitted_to_box`). Punctuation touching inline math
  ("(dx)", "T_B.") and a leading bullet stay on the math's line. Multi-line text in a taller box has its leading
  opened so the lines span the box like the written lines did.
- **Overlaps.** Text boxes that overlap share the overlap band (stacked boxes split it vertically, side-by-side
  ones horizontally); a text box overlapping a figure is trimmed out of it if it keeps at least half its size.
  Nested boxes are not split: text inside a figure gets a light backing, nested text boxes are listed in
  `overlaps_unresolved`.
- **Figures.** `clean` uses `figure.image_clean`, falling back to the original crop (`source.region_image`);
  that crop is a photo of the paper, so it is whitened by a conservative flat-field correction (ruled lines stay,
  faintly; counted as `whitened_here`). `original` uses the crop as stored; `svg` uses `figure.svg`, falling back
  to the crops. Without any stored image the region is cut from `pages/NNNN/original.png` (or `processed.png`) at
  the box; without a page image either, a dashed box shows the figure label, description and labels. A crop cut
  at the page's resolution with a small margin is drawn at its natural size, centred on its box.

## Fonts and limitations

- The LaTeX default font (Latin Modern) lacks Cyrillic and some math symbols in text; missing
  characters are reported. Use `main_font="DejaVu Serif"` (or any installed font with the glyphs);
  the exporter never claims a font that the compiled PDF does not contain.
- hyperref is used only when `hyperref.sty` and `pzdr.tfm` are installed; mhchem only when
  `mhchem.sty` is installed (otherwise `\ce{}` is approximated with `\mathrm`, with a warning).
- A LaTeX expression that passes the static checks but is still invalid TeX fails the whole compile;
  this is reported with the log tail.
- The searchable-PDF text layer uses one font; characters it lacks are listed in a warning.
- Layout PDF needs a Chromium-family browser for typeset math (see above). With model-estimated boxes the
  positions are only as good as the estimates; refined boxes give a closer layout. Text extracted from the
  layout PDF's equations is KaTeX output (spacing characters, split glyphs), not LaTeX.
- DOCX sources produce no page images, so they cannot be included in searchable PDFs.
- CSV/XLSX raise an error when the document has no tables.
