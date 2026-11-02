#!/usr/bin/env bash
# Example 2 — CLI workflow (Linux/macOS; see docs/CLI_REFERENCE.md for PowerShell).
#   ./examples/02_cli_workflow.sh path/to/paper.pdf
set -euo pipefail
FILE="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
export SCHOLAROCR_ALLOWED_DIRS="$(dirname "$FILE")"
DOC=$(scholarocr --json process "$FILE" --type exam-paper | python -c 'import json,sys; print(json.load(sys.stdin)["document_id"])')
echo "document: $DOC"
scholarocr content "$DOC" --pages 1 | head -40
scholarocr verify "$DOC" || echo "(verification reported problems; see above)"
scholarocr export "$DOC" --format docx
scholarocr export "$DOC" --format json
