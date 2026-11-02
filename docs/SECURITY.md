# Security

ScholarOCR runs locally. Documents leave the machine only as page images/crops sent to the Gemini
API for visual OCR, review, description and classification.

## File access
- Sources can be read only from `SCHOLAROCR_ALLOWED_DIRS` (default: the working directory).
- Paths are resolved with symlinks followed (`Path.resolve(strict=True)`) and must lie inside an
  allowed directory, so `..` traversal and symlinks pointing elsewhere are rejected.
- For paths outside the allowed area the error does not reveal whether the file exists.
- Files are type-checked by magic bytes. Size (`SCHOLAROCR_MAX_FILE_MB`) and page count
  (`SCHOLAROCR_MAX_PAGES`) are limited, and Pillow's decompression-bomb guard stays on (300 MP cap).
- Export output defaults to the data directory. An explicit output directory must be inside an
  allowed directory or the data directory.
- Document IDs and artifact references are validated, so stored relative paths cannot escape the
  document directory.

## Secrets and logging
- The API key is read from the environment only. It is never logged, never written to the database,
  and shown as `set`/`not set` by `scholarocr info`. The settings `repr` hides it.
- Logs go to stderr and contain identifiers, counts, timings and error categories. They contain no
  document text. Validation errors from model output are counted, not logged, because they can quote
  content. A test asserts that extracted text does not appear in the logs.
- The usage table stores token counts and metadata only, never prompts or outputs.

## Originals and deletion
- Inputs are copied into the store as read-only preserved copies. The caller's original file is
  never modified or deleted.
- `delete_document(mode="generated")` removes extractions, crops and exports, and keeps the preserved
  copy. `mode="all"` requires `confirm=true`. The skill tells agents to use it only on an explicit
  user request.
- Retention (`SCHOLAROCR_RETENTION_DAYS`) purges generated artifacts only, never preserved sources.

## Processes and network
- The MCP server uses stdio by default, so it has no network listener. In streamable-HTTP mode it
  binds to 127.0.0.1 by default, prints a warning for other hosts, and relies on the SDK's automatic
  DNS-rebinding protection for localhost. It has no authentication, so do not expose it beyond
  localhost.
- XeLaTeX runs with `-no-shell-escape`, restricted file I/O (`openin_any=p`, `openout_any=p`) and a
  timeout. pandoc runs on a temporary Markdown file of equations only.
- SQL uses parameter binding throughout.

## Known gaps
- Documents sent to Gemini are subject to Google's API data terms. Do not process material you are
  not permitted to send to a cloud API.
- The data directory is not encrypted. Use OS-level disk encryption for sensitive material.
