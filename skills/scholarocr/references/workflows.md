# Workflows

## Solve an exercise from a scanned textbook
1. `list_documents(query="textbook name")` → reuse if present.
2. Find the exercise: `get_document_status(document_id)` → `outline` lists headings and questions
   with page numbers. If not processed yet, process just the relevant pages: `ocr_document(path, pages="112-114")`.
3. `get_document_content(document_id, pages="112-114")`.
4. For each equation the solution depends on, if its anchor shows `uncertainty=medium|high`, call
   `get_source_region(element_id=…)` and read the crop. Proceed only once the statement is unambiguous;
   otherwise tell the user what is ambiguous.
5. Solve, citing "Exercise 5.3, page 113".

## Digitize previous-year question papers (batch)
- One paper = one `ocr_document` call (a folder of page photos is one document; files are ordered
  naturally: page1, page2, …, page10). For 15 papers, start each with `wait=false` and poll statuses.
- Hints: `document_type="previous_year_paper"`, `context_hint="Class 12 Physics, CBSE 2023"`.
- After extraction: `verify_document` → check `structure` findings (question numbering gaps, duplicate
  numbers), `duplicates`/`page_order` (missing or repeated pages) and `math` (invalid LaTeX).
- Use `get_document_content(format="json")` and the `question` fields (`number`, `marks`) to build
  question banks; `classify_document` for subject/topic when the user wants classification.
- Export `docx` (editable), `markdown`, or `searchable-pdf` as requested.

## Handwritten notes
- `ocr_document(path, document_type="handwritten_notes")` (handwriting profile; review on by default).
- Expect more `medium/high/unresolved`. Never complete words; show source regions for unclear parts.
- Consider `detail="high"` for formula-heavy notes.

## Research papers
- Usually born-digital → deterministic text extraction (fast, exact boxes); math-font pages and
  figures are read visually automatically.
- Use roles `abstract`, `authors`, headings for sections, `bibliography_entry` for references.

## Convert a scanned paper into an editable document
1. `ocr_document` → wait/poll. 2. `verify_document`; if errors or many unresolved items, consider
`verify_document(review=true)`. 3. `export_document(format="docx")`. 4. Report the file path and
every unresolved region / warning (page numbers) so the user can fix them.

## Large documents (hundreds of pages)
- Start with `wait=false`; poll `get_document_status(job_id)` at a relaxed pace.
- Failures on individual pages don't stop the job (`status=partial`); `manage_job(job_id, "resume")`
  re-runs only failed/unfinished pages.
- Retrieve content page-range by page-range with `next_cursor`; don't pull the whole book at once.
