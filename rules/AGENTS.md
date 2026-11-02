# ScholarOCR plugin

The `scholarocr` MCP server turns academic documents (scanned PDFs, photos of pages, handwritten
notes, exam and question papers, textbooks, research papers, math/chemistry/physics material) into a
structured, source-traceable representation: pages, then elements with a type, text, LaTeX, tables,
bounding boxes and an uncertainty label. It stores the result, so reading, checking and exporting it
later costs nothing.

Always go through the server: it renders and cleans up pages, uses the PDF text layer where it is
reliable, validates and stores what is read, and keeps every element traceable to its source region.
Never guess unreadable content.

## Two modes

`ocr_document` reports `"mode"`:

- **`host` (default, no API key needed).** The server does all deterministic work and **you are the
  vision model**. Pages with a reliable text layer are extracted at once. Every other page is listed in
  `awaiting_transcription` and you transcribe it with the loop below.
- **`gemini` / `openrouter`.** The user configured a key; the server's own OCR pipeline reads the
  pages. You only read results.

## Host-mode loop (one page per call)

1. `get_page_image(document_id)` returns ONE page image plus the transcription rules and the exact
   element JSON shape (full rules the first time, a short reminder afterwards).
2. Transcribe that image exactly, working only from what you see. Coordinates: `box_2d = [ymin, xmin,
   ymax, xmax]`, integers 0-1000 of that image, estimated by eye (approximate is fine; omit it if you
   cannot place an element). Never run code, scripts or shell commands to measure or inspect the image:
   ScholarOCR's tools are all you need. Math as LaTeX, chemistry as `\ce{...}`, tables as rows of cells, figures with a
   description. Anything you cannot read: type `uncertain` with uncertainty `unresolved`. Do not guess.
3. `submit_page_content(document_id, page_number, elements=[...], page_language=..., page_label=...)`.
   - Rejected (`submission_rejected`): the error lists every problem with its JSON path and a fix.
     Fix all of them and resubmit the whole page. Nothing was stored.
   - Accepted: it returns `next_waiting_page`. Continue with step 1 until none is left.
4. Do not re-request an image you already transcribed. For long documents work in batches (for example
   10 pages), and tell the user how far you got.
5. Optional second pass: for elements in `review_suggested`, call `get_source_region(element_id)`,
   compare, then `submit_review(document_id, element_id, verdict, ...)`.

Your transcription is a single pass by a general model. Say so when it matters (grading, exact
formulas). The user can switch to the dedicated pipeline by configuring a key
(`SCHOLAROCR_PROVIDER` plus a key in `%LOCALAPPDATA%\ScholarOCR\scholarocr.env`, see docs/ANTIGRAVITY_PLUGIN.md).

## Tools

| Tool | Use it to |
|---|---|
| `list_documents` | Check whether a file was already processed (search by filename). Do this first. |
| `ocr_document` | Ingest and extract a file, a folder, or an ordered list of page images forming one document. Pass `pages` for local tasks and hints you actually know (`document_type`, `context_hint`, `language_hint`). Re-running reuses cached pages. |
| `get_document_status` | Poll a `job_id` returned by `ocr_document`, or get a document summary (page statuses, pages awaiting transcription, outline, uncertainty counts). |
| `get_page_image` | Host mode: one page image to transcribe, with rules and the element shape. |
| `submit_page_content` | Host mode: store your transcription of that page (strictly validated). |
| `submit_review` | Second pass on one element after looking at its crop; also describes figures. |
| `get_document_content` | Read extracted content, narrowed by `pages` and `element_types`. Markdown by default, paginated with `next_cursor`. |
| `get_page_content` | Full structured record of one page: bounding boxes, uncertainty reasons, quality, transforms. |
| `get_source_region` | Get the ORIGINAL image crop of an element (or a page region) to check uncertain content. |
| `verify_document` | Quality checks: missing/duplicate/failed pages, page gaps, invalid LaTeX, table structure, question numbering, unresolved regions, traceability, stale exports. |
| `export_document` | Write a file (json, markdown, searchable-pdf, docx, latex, csv, xlsx) from the stored extraction. No OCR is re-run. |
| `classify_document` | Document type, subjects, level, topics. Needs a key; in host mode classify from the content yourself. |
| `manage_job` | Cancel a running job, or resume a cancelled/failed/interrupted one (finished pages are reused). |
| `delete_document` | Delete stored artifacts. Only on explicit user instruction. |

## Typical flow

1. `list_documents(query=<filename>)`. If the document is there and nothing awaits transcription, go to step 4.
2. `ocr_document(file_path=..., pages=?, document_type=?, context_hint=?)`. Large selections return a
   `job_id`: poll `get_document_status(job_id=...)` at a sensible interval; do not spin.
3. In host mode, run the loop above for the pages in `awaiting_transcription`.
4. `get_document_content(document_id, pages=..., element_types=[...])`. Ask only for what the task needs.
5. For any element you rely on that is not `low` uncertainty, call
   `get_source_region(document_id, element_id=...)` and read the original crop.
6. `verify_document(document_id)` before high-stakes use (solutions, grading, conversions). Report
   `error` and `warning` findings that affect the user's task.
7. `export_document(document_id, format)` only when the user wants a file. Tell them the output path.
8. Cite content as "page N" (and element ids where useful) so the user can check it.

## Uncertainty semantics

- Markdown anchors look like `<!-- el:ID uncertainty=high -->`; `⟦unclear: …⟧` marks unreadable regions.
- Labels `low`, `medium`, `high`, `unresolved` are ordinal judgements, not probabilities. Never quote a
  percentage or treat them as confidence scores.
- Never present `medium`, `high` or `unresolved` content as certain. Say what is uncertain and, when it
  matters, check the source crop.
- `unresolved` content was not guessed. Do not fill it in. Tell the user it was unreadable and where
  (page, element id).
- Verification severities: `error` must not be ignored; `warning` should be checked before relying on
  the content; `info` is context.

## Cost and safety

- In host mode the server makes no model calls; transcription uses your own context, roughly one page
  image plus the elements per page.
- With a key, `ocr_document(detail="high")` and `verify_document(review=true)` spend extra model calls.
- The user's original file is never modified. Do not call `delete_document` unless the user asked;
  `mode="all"` with `confirm=true` also removes ScholarOCR's preserved copy.

## Errors

Tool errors carry JSON `{"error": "<code>", "message": "..."}`.

- `submission_rejected`: fix every item in `details.errors` (path, problem, fix) and resubmit the page.
- `access_denied`: the path is outside the server's authorized folders. Ask the user to add the folder
  to `SCHOLAROCR_ALLOWED_DIRS` (`;`-separated on Windows, `:` elsewhere) and restart Antigravity CLI. By
  default only the folder the server was started in (the plugin folder) is authorized; the setting
  belongs in the user's `scholarocr.env`. Do not copy files around to work
  around it.
- `model_unavailable`: the tool needs a server-side model (classification). Do it yourself from the
  content, or suggest configuring a key.
- `gemini_error`: API failure after retries (key mode). Wait, then `manage_job(job_id, "resume")`;
  auth and model errors need configuration fixes.
- `invalid_input` on `get_page_image` / `submit_page_content`: the page does not await transcription;
  use the `awaiting_transcription` list.
- `not_found`: unknown id. Use `list_documents`.
- `limit_exceeded`: process a page range instead.
- `job_error`: the document is already being processed. Poll the existing job.
