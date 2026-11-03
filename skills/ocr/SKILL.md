---
name: ocr
description: >-
  Extract a scanned PDF, page photos, handwritten notes, an exam paper or a textbook with the
  ScholarOCR MCP server. Use when the user asks to OCR, read, digitize or extract a document, or
  types /ocr followed by a path.
---

## Instructions

The user's message names the file, folder, or list of page images. It may also give a page range
("1-5,8"), a document type (exam_paper, handwritten_notes, research_paper, ...) or a context hint.
Treat hints as hints, not facts.

1. Call `list_documents` with the file name. If the document is already processed and the pages are
   present, reuse it instead of re-extracting.
2. Otherwise call `ocr_document` with the path and any hints. If it returns a `job_id`, poll
   `get_document_status` at a sensible interval until the job finishes.
3. If the result says `mode: "host"` and lists `awaiting_transcription`, you are the vision model.
   One page per call: `get_page_image(document_id)` -> transcribe that image exactly, following the
   rules it returns -> `submit_page_content(document_id, page_number, elements=[...])`. If a
   submission is rejected, fix every listed problem and resubmit the page. Continue with
   `next_waiting_page` until none is left (for more than 10 pages, do 10 and ask before continuing).
   Each element's `uncertainty` is your doubt about it: `low` = you would bet it is exact, `medium` =
   a specific character or word could differ, `high` = a guess among alternatives, `unresolved` =
   cannot read. Label every element on its own; a clean print is mostly `low`.
   Never run shell commands or read the plugin's or server's files to learn the format: if unsure,
   call `get_page_image(instructions="full")`, pick the closest element type and explain in `notes`.
   If `ocr_document` warns that a page was skipped as blank but it has content, tell the user and
   re-run with `force_visual=true, reprocess=true`.
4. Report the document_id, mode, status, page count and uncertainty counts.
5. Show a short preview with `get_document_content` (first page or the requested pages).
6. List elements labelled `high` or `unresolved`, with page numbers and element ids. Do not guess
   unresolved text. Offer to check them against the source with `get_source_region`.

On `access_denied`, tell the user which folder to append to `SCHOLAROCR_ALLOWED_DIRS` in the settings
file the error names (`;`-separated on Windows; keep the existing entries). Do not copy the file
elsewhere.
