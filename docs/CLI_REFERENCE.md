# CLI reference

`scholarocr [--json] [--data-dir DIR] [--allowed-dir DIR ...] [--log-level LEVEL] <command> ...`

`--json` prints machine-readable JSON on stdout; progress and diagnostics go to stderr.
`--allowed-dir` (repeatable) overrides `SCHOLAROCR_ALLOWED_DIRS` for this invocation.

| Command | Purpose |
|---|---|
| `process PATH... [--pages R] [--type T] [--lang L] [--context TEXT] [--detail standard\|high] [--force-visual] [--no-crop] [--no-review] [--reprocess]` | Ingest file(s)/folder as ONE document and extract it, showing progress. Ctrl+C cancels (resumable). |
| `status ID` | Job status (`job_…`) or document summary (`doc_…`). |
| `content DOC [--pages R] [--types a,b] [--format markdown\|json\|text] [--max-chars N] [--cursor P]` | Print extracted content. |
| `page DOC N` | Full IR of page N (JSON). |
| `region DOC (--element ID \| --page N [--bbox x0,y0,x1,y1]) --out FILE.png` | Save the original source image region. |
| `verify DOC [--scope a,b] [--review]` | Quality checks; exit 7 if errors. |
| `export DOC --format F [--option k=v ...] [--out-dir DIR]` | `json`, `markdown`, `searchable-pdf`, `docx`, `latex`, `csv`, `xlsx` (aliases: md, pdf, tex). |
| `list [--query Q] [--status S] [--type T] [--limit N] [--offset N]` | List documents. |
| `classify DOC [--context TEXT] [--pages R]` | Classify (needs Gemini). |
| `delete DOC [--all --yes]` | Delete generated artifacts; `--all --yes` also the preserved copy and record. |
| `job cancel\|resume JOB` | Cancel or resume (resume runs in the foreground with progress). |
| `usage [DOC]` | Gemini calls/tokens (+ cost with a pricing file). |
| `purge [--days N] [--dry-run]` | Apply the retention policy (generated artifacts only). |
| `evaluate GT_PATH [--report FILE]` | Score against ground-truth JSON (see QUALITY_ASSURANCE.md). |
| `info` | Version and configuration (secrets redacted). |

Document types use dashes or underscores: `exam-paper`, `previous-year-paper`, `handwritten-notes`,
`research-paper`, `textbook`, `lecture-notes`, `lab-record`, ...

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | other error |
| 2 | command-line usage error |
| 3 | not found (document/job/element) |
| 4 | access denied (path outside authorized directories) |
| 5 | invalid input (bad range, corrupt/encrypted/unsupported file, limits) |
| 6 | model unavailable or failed: in host mode (no key) pages that need vision are left `awaiting_transcription`, which only an MCP client's model or a key can finish |
| 7 | finished with problems (partial job, or verification found errors) |
| 130 | interrupted; job cancelled and resumable |

## Examples (PowerShell)

```powershell
$env:SCHOLAROCR_ALLOWED_DIRS = "C:\Users\me\Study"
scholarocr process "C:\Users\me\Study\physics_2023.pdf" --type previous-year-paper --context "Class 12 Physics"
scholarocr content doc_1234abcd5678ef90 --pages 1-2
scholarocr region doc_1234abcd5678ef90 --element doc_1234abcd5678ef90-p0002-e007 --out q7.png
scholarocr verify doc_1234abcd5678ef90
scholarocr export doc_1234abcd5678ef90 --format docx
scholarocr export doc_1234abcd5678ef90 --format latex --option main_font="DejaVu Serif"
scholarocr --json list
```

Background jobs are a feature of the long-lived MCP server; the CLI runs each job in the foreground
(interrupting cancels it safely, and `scholarocr job resume JOB` continues it).
