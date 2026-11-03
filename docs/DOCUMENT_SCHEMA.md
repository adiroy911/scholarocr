# Document schema (universal IR)

Defined in `src/scholarocr/schemas/` (Pydantic v2). `SCHEMA_VERSION` and `EXTRACTION_VERSION` live
in `scholarocr/__init__.py` and are part of every cache key.

## Hierarchy

`Document` → `pages: [Page]` → `elements: [Element]`, plus the ingestion `Manifest`.

**Document**: `document_id` (`doc_` + 16 hex of the ordered source hashes), `title`,
`original_filenames`, `document_type`, `languages`, `page_count`, `source_metadata` (per file: PDF
metadata, sha256, media type, pages), `processing` (last options, classification, models, prompt
versions), `status` (`ingested`/`processing`/`processed`/`partial`/`partially_processed`/`awaiting_transcription`/`failed`),
`created_at`, `updated_at`, `extraction_version`, `schema_version`.

**Page**: `page_id` (`<doc>-pNNNN`), `page_number` (1-based position, never reordered),
`width_pt`, `height_pt`, `status` (`pending`/`done`/`blank`/`failed`/`awaiting_transcription`: host mode, waiting for the client's model), `route`, `orientation`
(rotation applied before OCR), `quality` (sharpness, contrast, brightness, ink ratio, noise, skew,
orientation, effective DPI, grade, issues), `transforms` (each preprocessing step + the affine
matrix), `original_image`, `processed_image`, `elements`, `language`, `page_label` (printed number),
`cache_key`, `model`, `prompt_version`, `warnings`, `error`, `processed_at`, `layout_refined_at` (when the boxes were last snapped to the ink; null for pages read before refinement existed, which `refine_document` / `update_layout` / `scholarocr refine` bring up to date).

**Manifest entry** (per page): source file and page index, size, `/Rotate`, image DPI, content hash,
perceptual hash, text hash, text-layer profile, `duplicate_of`, notes (`blank_page`,
`duplicate_page`, `possible_duplicate_page`, `exif_orientation_applied`, ...).

## Element

| Field | Meaning |
|---|---|
| `element_id` | `<page_id>-eNNN`, stable for a given extraction |
| `type` | title, heading, subheading, paragraph, text, handwriting, equation, chemical_equation, table, figure, diagram, image, caption, footnote, header, footer, page_number, list, code, bibliography_entry, reference, **uncertain** |
| `role` | none, chapter, section, question, subquestion, instruction, exercise, definition, theorem, lemma, example, proof, solution, answer, abstract, authors, affiliation, keywords, marginal_note |
| `content` | readable text exactly as written (math linearised; inline math as `$…$`) |
| `clean_content`, `clean_content_source` | text elements: `content` with casing, punctuation and spacing normalised and obvious slips of the pen fixed (same words, numbers, symbols, math and LaTeX; never rephrased). `model` = the vision model's `clean_text`, accepted by a deterministic guard; `rules` = the deterministic normaliser (the model's version was missing or refused; the reason is in `formatting.clean_text_rejected`). `null` for pages read from a text layer, math, tables, figures. Exports and the app choose `clean` or `faithful` (see EXPORT_FORMATS.md) |
| `latex` | LaTeX for math; mhchem `\ce{}` for chemistry |
| `level`, `items` | heading level; list items |
| `table` | `rows` of cells (`text`, `latex`, `row_span`, `col_span`, `is_header`, `uncertain`) using HTML span convention, `header_rows`, `caption`, `label` |
| `figure` | `description`, `labels`, `relationships`, `figure_label`, `kind`; `image_clean` (cleaned crop: paper and shadows white, ruled lines removed, the figure's own strokes only, trimmed); `svg` (optional redrawn vector copy, sanitised); `svg_fidelity` (the deterministic check of `svg` against the crop: `score`, `recall` = original ink the copy covers, `precision` = copy ink the original supports, `labels_missing`, `missing_regions`, `extra_regions`, `tolerance_px`, `size`, `passed`, `attempt`); `redraw_attempts`. Paths are relative to the document directory |
| `question` | `number`, `parent_number`, `marks`, `section` (as printed) |
| `parent_id` | caption → figure/table, subquestion → question |
| `bbox` + `bbox_source` | PDF points on the page as displayed (after /Rotate), top-left origin; `pdf_text_layer` (exact), `model_estimate` (the vision model's estimate, kept when refinement was unreliable) or `refined` (the model's box snapped to the ink on the page image, see below) |
| `bbox_model` | the model's own box when `bbox` was refined (same coordinates) |
| `uncertainty` + `uncertainty_reasons` | `low`/`medium`/`high`/`unresolved` (ordinal; see QUALITY_ASSURANCE.md) |
| `handwritten`, `language`, `formatting` | flags and font info |
| `extraction_method`, `model` | `embedded_text` / `docx` / `gemini` (any server key provider) / `host_model` (submitted by the MCP client's model; `model` is `host` or `host:<client name>`); model ID |
| `source` | `document_id`, `page_number`, `source_file`, `source_page_index`, `page_image`, `region_image` (paths relative to the document directory) |
| `revisions` | every change made by review/description: time, actor, field, previous value, note |

Real example (deterministic extraction):

```json
{
  "element_id": "doc_a107f0006c939316-p0001-e004",
  "type": "paragraph",
  "role": "question",
  "page_id": "doc_a107f0006c939316-p0001",
  "page_number": 1,
  "reading_order": 4,
  "content": "1. Solve the equation number 11 completely. [2]",
  "question": {"number": "1", "marks": "2"},
  "bbox": {"x0": 72.0, "y0": 248.18, "x1": 304.96, "y1": 263.29},
  "bbox_source": "pdf_text_layer",
  "uncertainty": "low",
  "uncertainty_reasons": [],
  "formatting": {"font": "Helvetica", "size": 11.0, "bold": false, "italic": false},
  "extraction_method": "embedded_text",
  "source": {"document_id": "doc_a107f0006c939316", "page_number": 1, "source_file": "clean_digital.pdf",
             "source_page_index": 0, "page_image": "pages/0001/original.png"},
  "revisions": []
}
```

## Traceability

Given an `element_id`, `get_source_region` (MCP) / `scholarocr region` (CLI) /
`ScholarOCR.get_source_region()` returns the crop of the original page image at `bbox` plus metadata.
Figures, diagrams, images, tables, equations and uncertain regions also have a saved crop
(`source.region_image`; for figures `pages/NNNN/figures/<id>.png` at full resolution, with the cleaned version in
`figure.image_clean`). The original source file itself is preserved under `source/`.

### Box refinement

Vision models estimate boxes by eye. After a page's elements are stored, `pipeline/layout_refine.py` snaps each
`model_estimate` box to the ink (deterministic, no model): the page is binarised relative to its paper level
after dividing out shadows; ruled notebook lines and margin rules (also slanted, curled or dotted ones) are found
and removed, keeping pen strokes that cross them; the ink is split into connected components, and each
component is assigned to one element (largest share of its pixels inside the slightly expanded box, then how
deep it sits in the box, then reading order; ink inside overlapping boxes goes to the element whose own ink is
nearest). Each box is fitted to its components. Figures also grow along nearby strokes and label-sized marks
(`T_A`, `dx`, `θ₁`) that are not claimed by another element and are not part of a text line, within a cap;
drawings that touch are split between their figures. When refinement is unreliable (no ink in the box, growth
past the cap, or most ink in the box belongs to a larger structure) the model box is kept and the page gets a
warning. `ScholarOCR.refine_document(document_id)` re-runs refinement and figure crops on stored pages.

## Storage layout

```
<data_dir>/scholarocr.db
<data_dir>/documents/<document_id>/
  manifest.json
  source/00_<original name>           preserved, read-only copy
  pages/0001/original.png             original page render
  pages/0001/processed.png            image the vision model saw (visual routes; Gemini/OpenRouter or the host model)
  pages/0001/host_task.json           host mode: what is needed to convert a submission (matrix, sizes, hints)
  pages/0001/page.json                page IR (checkpoint)
  pages/0001/regions/<element>.png    source crops (equations, tables, unreadable regions)
  pages/0001/figures/<element>.png    figure crop, full resolution (source.region_image)
  pages/0001/figures/<element>.clean.png   cleaned figure (figure.image_clean)
  pages/0001/figures/<element>.svg    optional redrawn copy (figure.svg; used only if svg_fidelity.passed)
  classification.json  verification.json
  exports/<format>/…
```
