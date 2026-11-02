# ScholarOCR extension

The `scholarocr` MCP server turns academic documents (scanned PDFs, photos of pages, handwritten
notes, exam and question papers, textbooks, research papers, math/chemistry/physics material) into a
structured, source-traceable representation: pages, then elements with a type, text, LaTeX, tables,
bounding boxes and an uncertainty label.

Do not OCR documents yourself and do not guess unreadable content. Call the server, then reason over
what it returns.

## When to use it

Use ScholarOCR when an answer depends on what a document says and any of these hold:

- the file is a scan or a photo, or a PDF without a reliable text layer;
- it contains equations, chemical formulas, tables, diagrams or handwriting;
- the user wants an editable or searchable version (Markdown, searchable PDF, DOCX, LaTeX, CSV, XLSX);
- you would otherwise read page images yourself.

Born-digital PDFs of plain prose are cheap too: pages with a reliable text layer need no model calls,
and you still get page and element references to cite.

## Tools

| Tool | Use it to |
|---|---|
| `list_documents` | Check whether a file was already processed (search by filename). Do this first. |
| `ocr_document` | Ingest and extract a file, a folder, or an ordered list of page images forming one document. Pass `pages` for local tasks and hints you actually know (`document_type`, `context_hint`, `language_hint`). Re-running reuses cached pages. |
| `get_document_status` | Poll a `job_id` returned by `ocr_document`, or get a document summary (page statuses, outline, uncertainty counts). |
| `get_document_content` | Read extracted content, narrowed by `pages` and `element_types`. Markdown by default, paginated with `next_cursor`. |
| `get_page_content` | Full structured record of one page: bounding boxes, uncertainty reasons, quality, transforms. |
| `get_source_region` | Get the ORIGINAL image crop of an element (or a page region) to check uncertain content yourself. |
| `verify_document` | Quality checks: missing/duplicate/failed pages, page gaps, invalid LaTeX, table structure, question numbering, unresolved regions, traceability, stale exports. |
| `export_document` | Write a file (json, markdown, searchable-pdf, docx, latex, csv, xlsx) from the stored extraction. No OCR is re-run. |
| `classify_document` | Document type, subjects, level, topics. |
| `manage_job` | Cancel a running job, or resume a cancelled/failed/interrupted one (finished pages are reused). |
| `delete_document` | Delete stored artifacts. Only on explicit user instruction. |

## Typical flow

1. `list_documents(query=<filename>)`. If the document is there, go to step 3.
2. `ocr_document(file_path=..., pages=?, document_type=?, context_hint=?)`. Large selections return a
   `job_id`: poll `get_document_status(job_id=...)` at a sensible interval; do not spin.
3. `get_document_content(document_id, pages=..., element_types=[...])`. Ask only for what the task needs.
4. For any element you rely on that is not `low` uncertainty, call
   `get_source_region(document_id, element_id=...)` and read the original crop.
5. `verify_document(document_id)` before high-stakes use (solutions, grading, conversions). Report
   `error` and `warning` findings that affect the user's task.
6. `export_document(document_id, format)` only when the user wants a file. Tell them the output path.
7. Cite content as "page N" (and element ids where useful) so the user can check it.

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

- `ocr_document(detail="high")` and `verify_document(review=true)` spend extra model calls. Use them for
  difficult math or handwriting, or when the user asks for maximum fidelity.
- The user's original file is never modified. Do not call `delete_document` unless the user asked;
  `mode="all"` with `confirm=true` also removes ScholarOCR's preserved copy.

## Errors

Tool errors carry JSON `{"error": "<code>", "message": "..."}`.

- `access_denied`: the path is outside the server's authorized folders. Ask the user to add the folder
  to `SCHOLAROCR_ALLOWED_DIRS` (`;`-separated on Windows, `:` elsewhere) and restart Gemini CLI. By
  default only the folder Gemini CLI was started in is authorized. Do not copy files around to work
  around it.
- `gemini_unavailable`: no API key reached the server. Text-layer pages still work; scanned pages fail.
  Tell the user to run `gemini extensions config scholarocr` or add the key to a `.env` file in the
  workspace.
- `gemini_error`: API failure after retries (rate limit, auth, unknown model). Wait, then
  `manage_job(job_id, "resume")`; auth and model errors need configuration fixes.
- `not_found`: unknown id. Use `list_documents`.
- `limit_exceeded`: process a page range instead.
- `job_error`: the document is already being processed. Poll the existing job.
