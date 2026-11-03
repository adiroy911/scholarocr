---
name: ocr-export
description: >-
  Export a document ScholarOCR has already processed to json, markdown, searchable-pdf, docx, latex,
  csv or xlsx. Use when the user wants a file from an extraction.
---

## Instructions

1. Resolve the document_id (use `list_documents` for a file name; ask if several match).
2. If the user did not name a format, ask. Suggest markdown for reading, docx for editing,
   searchable-pdf for a scan they want to search, latex for math-heavy material, csv or xlsx for
   tables.
3. Call `export_document` with that format. No OCR is re-run.
4. Report the output path(s), warnings, and the post-generation checks it returns (for example
   searchable-PDF text recall or LaTeX compile status). For docx, native Word equations need pandoc on
   the server's PATH; without it equations are inserted as LaTeX source and inline math as readable text,
   and the export warns about it.
5. If the document has `unresolved` or `high` uncertainty elements, say they are carried into the
   export as-is and suggest the ocr-verify skill first.
