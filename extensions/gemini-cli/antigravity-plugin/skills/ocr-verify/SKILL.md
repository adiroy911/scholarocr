---
name: ocr-verify
description: >-
  Verify a ScholarOCR extraction and check its uncertain elements against the original page image.
  Use before relying on extracted content for solutions, grading or conversions, or when the user
  asks whether an extraction is accurate.
---

## Instructions

1. Resolve the document_id. If the user gave a file name, find it with `list_documents`; if several
   documents match, ask which one.
2. Call `verify_document` with the default scope. Do not set `review=true` unless the user asked for
   a review pass, because it spends model calls.
3. Summarize findings by severity (`error`, `warning`, `info`) with page numbers and element ids.
   Errors must not be glossed over.
4. For each element with uncertainty `high` or `unresolved` (at most 5; ask before doing more), call
   `get_source_region` and compare the original crop with the extracted content. Never present
   unresolved text as read by the engine; if you read it yourself from the crop, say so.
5. End with a one-line verdict: is the extraction safe to rely on, and what should be re-checked.

Uncertainty labels are ordinal, not probabilities. Do not turn them into percentages.
