"""DOCX input: deterministic text/table extraction. DOCX has no fixed pages, so the whole
file becomes one logical page without page geometry (documented limitation)."""

from __future__ import annotations

from pathlib import Path

from ..exceptions import CorruptDocumentError


def read_docx_blocks(path: Path) -> list[dict]:
    """Return blocks in body order: {"kind": "heading"|"paragraph"|"list"|"table", ...}."""
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as e:  # pragma: no cover
        raise CorruptDocumentError(f"python-docx unavailable: {e}") from None
    try:
        d = docx.Document(str(path))
    except Exception as e:
        raise CorruptDocumentError(f"{path.name}: cannot read DOCX ({e.__class__.__name__})") from None
    blocks: list[dict] = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            if style.lower().startswith("title"):
                blocks.append({"kind": "title", "text": text})
            elif style.lower().startswith("heading"):
                try:
                    level = int(style.split()[-1])
                except ValueError:
                    level = 1
                blocks.append({"kind": "heading", "text": text, "level": level})
            elif "list" in style.lower():
                blocks.append({"kind": "list", "text": text})
            else:
                blocks.append({"kind": "paragraph", "text": text,
                               "bold": all(r.bold for r in p.runs if r.text.strip()) if p.runs else False})
        elif tag == "tbl":
            t = Table(child, d)
            rows = [[c.text.strip() for c in r.cells] for r in t.rows]
            blocks.append({"kind": "table", "rows": rows})
    return blocks
