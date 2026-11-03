# ScholarOCR MCP tools

All tools return JSON (structured content). Errors come back as tool errors whose text is JSON:
`{"error": "<code>", "message": "..."}`.

| Tool | Purpose | Key inputs | Returns |
|---|---|---|---|
| `ocr_document` | Ingest + extract (cached per page) | `file_path` (path, folder, or ordered list = one document), `document_type`, `language_hint`, `context_hint`, `pages`, `detail` (`standard`/`high`), `force_visual`, `auto_crop` (default true: screenshots/photos cropped to the page), `review` (default true), `reprocess`, `wait` | `document_id`, `job_id`, `status`, `mode` (`host`/`gemini`/`openrouter`), `page_count`, `selected_pages`, `synchronous`, `warnings`, `summary` when synchronous; host mode adds `awaiting_transcription` and `next_step` |
| `get_document_status` | Job progress or document summary | `job_id` **or** `document_id` | job: `status` (queued/running/completed/partial/failed/cancelled/interrupted/awaiting_transcription), `progress` "done/total", `stage`, `errors`, `cached_pages`; document: `pages_by_status`, `routes`, `uncertainty` counts, `outline`, `failed_pages`, `awaiting_transcription`, `mode`, `latest_job` |
| `get_document_content` | Content, paginated by page | `document_id`, `pages`, `element_types`, `format` (`markdown`/`json`/`text`), `max_chars`, `cursor` | `content` (or `pages` for json), `next_cursor`, `unprocessed_pages`, `awaiting_transcription` |
| `get_page_content` | IR of one page | `document_id`, `page_number`, `compact` (id/type/content/latex/uncertainty/question/bbox only), `fields` | page object: `elements[]`, `quality`, `transforms`, `route`, `warnings`, `status`, `error` |
| `get_source_region` | Original image of an element/page region | `document_id` + `element_id`, or `page_number` (+ `bbox` [x0,y0,x1,y1] in PDF points) | PNG image + metadata (`bbox_pt`, `bbox_source`, `uncertainty`, `content`, `latex`, `source_file`, `source_page_index`) |
| `get_page_image` | Host mode: one page to transcribe | `document_id`, `page_number` (default: next waiting), `instructions` (`auto`/`full`/`short`) | image + rules + element shape; `page_number`, `image` size, `next_waiting_page`; or `nothing_to_transcribe` |
| `submit_page_content` | Host mode: store your transcription of a page | `document_id`, `page_number`, `elements[]` (same shape as the engine's model output: `type`, `text`, `latex`, `table`, `figure`, `question`, `role`, `box_2d` [ymin,xmin,ymax,xmax] 0-1000 of the image, `uncertainty`, `caption_of`, `parent_index`, ...), `page_language`, `page_label`, `page_doubt` (overall doubt; old name `legibility`), `rotation_detected`, `notes` | `accepted`, `elements`, `uncertainty` counts, `warnings`, `review_suggested`, `next_waiting_page`; rejection: error `submission_rejected` with `details.errors[]` (`path`, `problem`, `fix`) and nothing stored; `rotated: true` when the image was re-oriented and must be fetched again |
| `submit_review` | Second pass on one element (host mode, after `get_source_region`) | `document_id`, `element_id`, `verdict` (`confirmed`/`corrected`/`unreadable`), `text`, `latex`, `table`, `figure`, `uncertainty`, `notes` | new `uncertainty`, `content`, `revisions` count |
| `verify_document` | Quality checks | `document_id`, `scope[]`, `review` | `passed`, `counts`, `findings[]` (check, severity, message, page_number, element_id), `stats`, `reviewed_elements`; host mode with `review=true`: `review_candidates`, `notes` |
| `export_document` | File from stored IR | `document_id`, `format`, `options` | `primary_file`, `files`, `warnings`, `verification` |
| `list_documents` | Catalogue | `query`, `status`, `document_type`, `limit`, `offset` | `documents[]`, `total`, `next_offset` |
| `classify_document` | Document type/subject | `document_id`, `context_hint`, `pages` | `document_type`, `subjects`, `level`, `chapters_or_topics`, `certainty`, `hint_consistent`, `rationale`; host mode: error `model_unavailable` |
| `delete_document` | Delete stored artifacts | `document_id`, `mode` (`generated`/`all`), `confirm` | what was deleted/kept |
| `manage_job` | Cancel or resume | `job_id`, `action` (`cancel`/`resume`) | job status |

## Element fields (IR)

`element_id` (`doc_…-p0003-e012`), `type` (title, heading, subheading, paragraph, text, handwriting,
equation, chemical_equation, table, figure, diagram, image, caption, footnote, header, footer,
page_number, list, code, bibliography_entry, reference, uncertain), `role` (question, subquestion,
definition, theorem, example, proof, solution, answer, abstract, …), `content`, `latex`, `table`
(`rows` of cells with spans, `header_rows`, `caption`), `figure` (`description`, `labels`,
`relationships`), `question` (`number`, `marks`, `parent_number`), `parent_id` (caption→figure,
subquestion→question), `bbox` (PDF points, top-left origin) + `bbox_source` (`pdf_text_layer` exact /
`model_estimate` approximate), `uncertainty` + `uncertainty_reasons`, `source` (`page_number`,
`source_file`, `source_page_index`, `page_image`, `region_image`), `revisions` (review history).

## Routes (how a page was read)

`embedded_text` (deterministic, no model call), `visual_general`, `visual_math`,
`visual_handwriting`, `blank`, `docx_text`. Visual routes are read by the server's model when a key is
configured; in host mode they wait (`awaiting_transcription`) for the client's model.
`extraction_method` says who read an element: `embedded_text`, `docx`, `gemini` (a server key provider), or
`host_model` (submitted through `submit_page_content`).
