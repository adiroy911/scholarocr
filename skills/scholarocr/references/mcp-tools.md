# ScholarOCR MCP tools

All tools return JSON (structured content). Errors come back as tool errors whose text is JSON:
`{"error": "<code>", "message": "..."}`.

| Tool | Purpose | Key inputs | Returns |
|---|---|---|---|
| `ocr_document` | Ingest + extract (cached per page) | `file_path` (path, folder, or ordered list = one document), `document_type`, `language_hint`, `context_hint`, `pages`, `detail` (`standard`/`high`), `force_visual`, `auto_crop` (default true: screenshots/photos cropped to the page), `review` (default true), `reprocess`, `wait` | `document_id`, `job_id`, `status`, `page_count`, `selected_pages`, `synchronous`, `warnings`, and `summary` when synchronous |
| `get_document_status` | Job progress or document summary | `job_id` **or** `document_id` | job: `status` (queued/running/completed/partial/failed/cancelled/interrupted), `progress` "done/total", `stage`, `errors`, `cached_pages`; document: `pages_by_status`, `routes`, `uncertainty` counts, `outline`, `failed_pages`, `latest_job` |
| `get_document_content` | Content, paginated by page | `document_id`, `pages`, `element_types`, `format` (`markdown`/`json`/`text`), `max_chars`, `cursor` | `content` (or `pages` for json), `next_cursor`, `unprocessed_pages` |
| `get_page_content` | Full IR of one page | `document_id`, `page_number` | page object: `elements[]`, `quality`, `transforms`, `route`, `warnings`, `status`, `error` |
| `get_source_region` | Original image of an element/page region | `document_id` + `element_id`, or `page_number` (+ `bbox` [x0,y0,x1,y1] in PDF points) | PNG image + metadata (`bbox_pt`, `bbox_source`, `uncertainty`, `content`, `latex`, `source_file`, `source_page_index`) |
| `verify_document` | Quality checks | `document_id`, `scope[]`, `review` | `passed`, `counts`, `findings[]` (check, severity, message, page_number, element_id), `stats`, `reviewed_elements` |
| `export_document` | File from stored IR | `document_id`, `format`, `options` | `primary_file`, `files`, `warnings`, `verification` |
| `list_documents` | Catalogue | `query`, `status`, `document_type`, `limit`, `offset` | `documents[]`, `total`, `next_offset` |
| `classify_document` | Document type/subject | `document_id`, `context_hint`, `pages` | `document_type`, `subjects`, `level`, `chapters_or_topics`, `certainty`, `hint_consistent`, `rationale` |
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
`visual_handwriting`, `blank`, `docx_text`. Visual routes need `GEMINI_API_KEY` on the server.
