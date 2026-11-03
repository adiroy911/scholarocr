# Verification, uncertainty, traceability, errors

## Uncertainty labels (ordinal, not probabilities)
- Start: extractor's own label (deterministic text layer = low unless characters are garbled).
- Escalated by evidence: invalid LaTeX → high; table structure problems → medium; poor (not fair) page
  quality for math/tables/handwriting → medium; `uncertain` elements → unresolved. The page-level `page_doubt`
  (old name `legibility`) is only a page warning; it does not raise element labels.
- Host mode: each element's label is your doubt about it (`low` = you would bet it is exact, `medium` =
  a specific character or word could differ, `high` = a guess among alternatives, `unresolved` = cannot
  read). Label every element on its own.
- Targeted review (second model on the crop): confirmed → one step lower (and not re-raised by the rule
  that flagged it); corrected → content replaced (old value in `revisions`), at least medium; unreadable →
  unresolved.

## Verification findings (`verify_document`)
Severity `error` = must not be ignored (failed pages, empty extraction, broken traceability);
`warning` = check before relying on it (missing/duplicate pages, page-number gaps, invalid LaTeX,
ragged tables, question numbering gaps, unresolved/high-uncertainty elements, stale exports);
`info` = context (blank pages, uncertainty distribution).

## Source traceability
Every element carries `source.page_number`, `source_file`, `source_page_index`, `page_image` and,
for figures/tables/equations/uncertain regions, `region_image`. `get_source_region(element_id)` returns
the original crop; boxes from a model (Gemini, OpenRouter, or you in host mode) are approximate (`bbox_source="model_estimate"`), so the crop
has padding.

## Error codes
| code | meaning | what to do |
|---|---|---|
| `access_denied` | path outside authorized dirs | ask the user to APPEND the folder to `SCHOLAROCR_ALLOWED_DIRS` in the settings file the message names (`;` on Windows, `:` elsewhere; keep the existing entries) and restart the server |
| `invalid_input` / `unsupported_format` | bad arguments or file type | fix arguments; supported: PDF, PNG, JPEG, TIFF, WebP, BMP, GIF, DOCX |
| `encrypted_document` | password-protected PDF | ask the user for a decrypted copy |
| `corrupt_document` | damaged file | ask for another copy |
| `limit_exceeded` | file size / page limit | process a page range or raise the limit |
| `not_found` | unknown document/job/element id | `list_documents` |
| `submission_rejected` | your `submit_page_content`/`submit_review` failed validation | fix every `details.errors[]` item (path, problem, fix) and resubmit the whole page; nothing was stored |
| `model_unavailable` / `gemini_unavailable` | a tool needs a server-side model and none is configured (host mode) | do the task yourself from the content (e.g. classification), or the user configures `GEMINI_API_KEY` / `OPENROUTER_API_KEY` |
| `gemini_error` | API failure after retries (rate limit, auth, model not found) | wait and `manage_job(resume)`; auth/model errors need configuration fixes |
| `response_validation_failed` | model output invalid twice | the page is marked failed (never silently accepted); resume later or try `force_visual`/`detail` changes |
| `job_error` | job conflict (document already processing) | poll the existing job |

A page that fails shows up in `get_document_status(document_id).failed_pages` and in verification;
the rest of the document remains usable.
