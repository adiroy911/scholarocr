"""Example 1 — Python API: process a document, inspect uncertain items, export Markdown + searchable PDF.

    python examples/01_python_api.py path/to/document.pdf

Scanned pages need GEMINI_API_KEY; born-digital pages are extracted without it.
"""

import sys
from pathlib import Path

from scholarocr.config import Settings
from scholarocr.schemas import ProcessingOptions
from scholarocr.service import ScholarOCR

path = Path(sys.argv[1]).resolve()
settings = Settings.from_env()
settings.allowed_dirs = [path.parent]  # authorize just this folder for the example
ocr = ScholarOCR(settings)

result = ocr.process(path, ProcessingOptions(context_hint="academic document"), wait=True)
doc_id = result["document_id"]
summary = result["summary"]
print(f"{doc_id}: {summary['pages_by_status']} routes={summary['routes']} uncertainty={summary['uncertainty']}")

doc = ocr.get_document(doc_id)
for el in doc.iter_elements():
    if el.uncertainty.value != "low":
        print(f"  check p{el.page_number} {el.element_id} [{el.type.value}/{el.uncertainty.value}] "
              f"{el.content[:60]!r} reasons={el.uncertainty_reasons}")

report = ocr.verify(doc_id)
print("verification:", "passed" if report.passed else "FAILED", report.counts())
for fmt in ("markdown", "searchable-pdf"):
    if fmt == "searchable-pdf" and all(s.endswith(".docx") for s in doc.original_filenames):
        continue
    out = ocr.export(doc_id, fmt)
    print(f"{fmt}: {out['primary_file']}")
