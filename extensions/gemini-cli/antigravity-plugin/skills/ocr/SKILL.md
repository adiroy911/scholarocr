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
3. Report the document_id, status, page count and uncertainty counts.
4. Show a short preview with `get_document_content` (first page or the requested pages).
5. List elements labelled `high` or `unresolved`, with page numbers and element ids. Do not guess
   unresolved text. Offer to check them against the source with `get_source_region`.

On `access_denied`, tell the user which folder to add to `SCHOLAROCR_ALLOWED_DIRS`. Do not copy the
file elsewhere.
