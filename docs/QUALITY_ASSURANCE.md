# Quality assurance

OCR is not "done" because a model returned text. ScholarOCR layers deterministic checks, ordinal
uncertainty, targeted review and an evaluation framework, and it never fills in unreadable content.

## Uncertainty labels

Ordinal: `low` < `medium` < `high` < `unresolved`. They are **not probabilities** and no accuracy
percentage is derived from them. The rules are in `intelligence/uncertainty.py`:

1. **Start** from the extractor's label. Gemini self-reports a label per element. Deterministic
   text-layer extraction starts at `low`, or `medium` if the text layer contains unmapped
   characters (U+FFFD or private-use code points).
2. **Escalate** (never lower) when there is evidence:
   - element type `uncertain` → `unresolved`
   - LaTeX fails the syntax checks → at least `high`
   - chemistry checks fail → `medium` or `high`
   - table structure issues → at least `medium`
   - the model reports page legibility `high` or `unresolved` → at least `medium`
   - page graded `poor` and the element is math, a table or handwriting → at least `medium`
   - handwriting on a `fair` or `poor` page → at least `medium`
   - visual, math or table elements with no box get a "no bounding box" reason
3. **Targeted review** sends a crop of the original page to the review model:
   - `confirmed` → one step lower (`unresolved` stays)
   - `corrected` → content replaced and the old value kept in `revisions`; label becomes at least `medium`
   - `unreadable` → `unresolved`

Reviews are selected per page: medium/high/unresolved elements that have a box, up to 8 per page.
With `detail="high"`, every equation and table is reviewed as well.

## Page quality (OpenCV, `preprocessing/quality.py`)

| Metric | Definition | Threshold |
|---|---|---|
| sharpness | variance of the Laplacian near ink pixels | < 400 → `blurry` |
| contrast | paper (90th pct) − ink (10th pct of dark pixels) | < 110 → `low_contrast` |
| noise | mean abs. residual after a 3×3 median filter, on background pixels | > 6 → `noisy` |
| skew | projection-profile search, ±10° (needs ≥ 0.4% dark pixels) | ≥ 0.4° → `skewed` |
| orientation | aspect ratio of word blobs (dilation sized to the median glyph) | vertical text → `rotated` |
| resolution | median character height (connected components); DPI metadata only as fallback | < 12 px → `low_resolution` |
| polarity | median grey < 110 ⇒ light writing on a dark page; metrics computed on the inverse | `light_on_dark` (informational) |
| blank | contrast < 30 or dark pixels < 0.02% | blank |

Grade: `poor` if two or more of {blurry, noisy, low_resolution, dark}, or blurry together with low
contrast. `fair` if any issue other than `light_on_dark`. Otherwise `good`.

Before assessment, images are cropped to a detected document region when they are a screenshot or
photo of a page inside a larger frame (`preprocessing/cropping.py`; nested frames such as screen →
app → note page are handled). The crop is conservative: a uniform-tone, rectangular, content-bearing
region covering 12-92% of the frame, touching at most two edges, whose tone differs from its
surroundings by at least 60 grey levels. Normal scans and ruled boxes on a page never qualify.
`auto_crop=false` disables it. Preprocessing then runs only for measured issues: crop → rotation →
deskew → upscale (characters < 12 px, to about 22 px; max 2.5×) → denoise → CLAHE contrast. No binarisation or cropping is
applied, and the original image is always kept.

## Verification (`verify_document`, `pipeline/verification.py`)

| Scope | Checks |
|---|---|
| completeness | unprocessed (warning) and failed (error) pages; blank pages (info) |
| duplicates | exact and near-duplicate pages from the manifest |
| page_order | printed page-number gaps (possible missing pages), decreasing or repeated numbers |
| content | empty extraction (error); suspiciously short extraction on an inked page; text-layer coverage < 70%; no headings in a long document |
| reading_order | duplicate or gapped reading order; duplicate element IDs |
| math | invalid LaTeX / chemistry; math-routed page with no equations |
| tables | ragged rows, single column, mostly empty, uncertain cells |
| structure | question-numbering gaps and duplicates (within a section); unlinked captions |
| uncertainty | each unresolved / high element; distribution |
| traceability | missing page images or crops; source reference mismatches; visual elements with no box |
| exports | missing export files; stale JSON exports |

Errors make `passed = false`. `review=true` additionally sends flagged elements that haven't been
reviewed yet to the review model, so they are never reviewed twice.

## Evaluation framework (`evaluation/metrics.py`, `scholarocr evaluate`)

The ground-truth format is documented in the module. Metrics:

- CER and WER (Levenshtein after NFKC/whitespace normalisation)
- math exact match and mean similarity (normalised LaTeX)
- table shape and cell accuracy
- page completeness
- element classification accuracy (best text match ≥ 0.6 similarity, then type equality)
- traceability (source references and image files exist)

### Benchmark: synthetic, deterministic route only

`python benchmarks/run_benchmark.py` on the generated born-digital documents (2026-11-01):

| document | CER | WER | table cells | classification | traceability |
|---|---|---|---|---|---|
| clean_digital.pdf (2 p) | 0.000 | 0.000 | – | 1.000 | 1.000 |
| tables.pdf | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 |
| multilingual.pdf (FR/EL/RU) | 0.000 | 0.000 | – | – | 1.000 |
| research_paper.pdf (2 columns) | – | – | – | 1.000 | 1.000 |

These numbers only show that the deterministic path reproduces clean generated text layers
faithfully. **They are not an accuracy claim for scanned, handwritten or real-world documents.**
`--live` adds scanned fixtures but needs an API key, and it was **not run**. No visual-OCR accuracy
has been measured yet (see ROADMAP.md).

## Test suite

`python -m pytest` (2026-11-01, Linux, Python 3.13): **129 passed, 3 skipped, 0 failed**.

| Group | Result |
|---|---|
| Unit (foundation, schemas, security, ingestion, Gemini client, prompts/schemas, uncertainty, LaTeX/table checks, reviewer, exports, evaluation) | 98 passed |
| Integration (pipeline with a scripted fake Gemini, MCP in-process + real stdio subprocess, CLI subprocess, end-to-end) | 31 passed |
| Live Gemini (`tests/live`, opt-in) | 3 skipped: not run, no API key |

Gemini behaviour in the default suite comes from `tests/fixtures/fake_gemini.py` (mocked responses).
The test corpus (`tests/fixtures/corpus.py`) is generated at test time. It covers clean digital,
poor scan, rotated, skewed, math paper, chemistry, physics with vector and raster diagrams,
handwriting stand-in (italic font, not real handwriting), a screen-share screenshot with a dark note page, tables, two-column research paper, mixed
PDF, multilingual, corrupted, fake-PDF, encrypted, duplicate and blank pages, and DOCX.
