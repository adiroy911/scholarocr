---
name: scholarocr
description: Read, extract and verify content from scanned or complex academic documents (scanned PDFs, photos of pages, handwritten notes, exam/question papers, textbooks, research papers, math/chemistry/physics material) through the ScholarOCR MCP server or CLI. Use whenever a task depends on the contents of such a document, when a PDF has no reliable text layer, when equations/tables/diagrams must be read accurately, or when the user wants a scanned document converted to Markdown, searchable PDF, DOCX, LaTeX or spreadsheets.
---

# ScholarOCR

ScholarOCR is a shared OCR/intelligence backend. It turns academic documents into a structured,
source-traceable representation (pages → elements with type, text, LaTeX, tables, bounding boxes,
uncertainty). **Never re-implement OCR yourself and never guess unreadable content**: call the
engine, then reason over what it returns.

## When to use it

Use it when the answer depends on what a document says and any of these hold: the file is a scan
or photo; it contains equations, chemical formulas, tables, diagrams or handwriting; the user asks
for an editable/searchable version; or you would otherwise read page images yourself. For a plain
born-digital PDF of ordinary prose, ScholarOCR is still cheap (no model calls for pages with a
reliable text layer) and gives you page/element references.

## Core workflow (MCP tools: `mcp__scholarocr__*`)

1. **Reuse first**: `list_documents(query=<filename>)`. If it is already processed, skip to step 3.
   Calling `ocr_document` again is safe — unchanged pages come from cache — but costs a round trip.
2. **Extract**: `ocr_document(file_path=..., document_type=?, context_hint=?, pages=?)`.
   - Pass hints you actually know ("Class 11 chemistry exam", `document_type="handwritten_notes"`).
   - Large documents return a `job_id`; poll `get_document_status(job_id=...)` every so often
     (don't spin). Process only the pages you need (`pages="40-55"`) when the task is local.
3. **Retrieve narrowly**: `get_document_content(document_id, pages=..., element_types=[...])`
   (markdown by default, paginated via `next_cursor`). Use `get_page_content` for the full IR of a
   page (bounding boxes, uncertainty reasons, quality, transforms).
4. **Check uncertain content against the source** before relying on it: anchors look like
   `<!-- el:ID uncertainty=high -->`; `⟦unclear: …⟧` marks unreadable regions. Call
   `get_source_region(document_id, element_id=ID)` to see the original crop and read it yourself.
5. **Verify** with `verify_document(document_id)` before high-stakes use (solutions, conversions,
   grading). Report ERROR/WARNING findings that affect the user's task.
6. **Export only on request**: `export_document(document_id, format)`; no OCR is re-run.
7. **Cite pages**: refer to content as "page N" (and element ids when useful) so the user can check.

## Rules

- Uncertainty labels (low/medium/high/unresolved) are ordinal, **not probabilities**. Never present
  medium/high/unresolved content as certain; say what is uncertain and show the source when it matters.
- Never fill in `unresolved` text. Tell the user it was unreadable and where (page, element).
- Don't delete anything unless the user explicitly asks. `delete_document(mode="all", confirm=true)`
  removes ScholarOCR's preserved copy; the user's original file is never touched.
- `verify_document(review=true)` and `ocr_document(detail="high")` spend extra model calls — use
  them for difficult math/handwriting or when the user needs maximum fidelity.
- File access is limited to the server's authorized directories; on `access_denied`, tell the user
  to add the folder to `SCHOLAROCR_ALLOWED_DIRS` rather than copying files around.

## CLI fallback (no MCP available)

`scholarocr --json process FILE [--pages 1-10] [--type exam-paper] [--context "..."]`, then
`scholarocr content DOC_ID [--pages ..] [--types equation,table]`, `scholarocr region DOC_ID
--element ID --out crop.png` (then view the PNG), `scholarocr verify DOC_ID`,
`scholarocr export DOC_ID --format docx`. Exit code 6 = Gemini key missing/failed, 7 = finished
with problems (partial pages / verification errors).

## References (read when needed)

- `references/mcp-tools.md` — every tool, parameters and return shapes.
- `references/workflows.md` — recipes: solve a textbook exercise, digitize question papers,
  handwritten notes, research papers, batches, large documents.
- `references/output-formats.md` — choosing and configuring export formats.
- `references/verification-and-errors.md` — verification findings, uncertainty rules, source
  traceability, error codes and recovery.
