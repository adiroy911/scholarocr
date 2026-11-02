"""On-disk layout for a document. All stored references are relative to the document dir.

documents/<document_id>/
  source/<NN>_<original filename>     preserved copy of each source (never auto-deleted)
  manifest.json
  pages/<NNNN>/original.png            render of the original page
  pages/<NNNN>/processed.png           preprocessed image actually sent to Gemini
  pages/<NNNN>/page.json               page IR (checkpoint)
  pages/<NNNN>/regions/<element>.png   cropped source regions
  classification.json, verification.json
  exports/<format>/...
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from pydantic import BaseModel

from ..exceptions import NotFoundError


class ArtifactStore:
    def __init__(self, documents_dir: Path):
        self.root = documents_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def doc_dir(self, document_id: str) -> Path:
        if not document_id or any(c in document_id for c in "/\\.:") or document_id != os.path.basename(document_id):
            raise NotFoundError(f"Invalid document id: {document_id!r}")
        return self.root / document_id

    def page_dir(self, document_id: str, page_number: int) -> Path:
        return self.doc_dir(document_id) / "pages" / f"{page_number:04d}"

    def rel(self, document_id: str, path: Path) -> str:
        return path.relative_to(self.doc_dir(document_id)).as_posix()

    def resolve(self, document_id: str, rel: str) -> Path:
        base = self.doc_dir(document_id).resolve()
        p = (base / rel).resolve()
        if not p.is_relative_to(base):
            raise NotFoundError("Artifact reference escapes document directory")
        return p

    def write_json(self, path: Path, data: BaseModel | dict | list) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = data.model_dump_json(indent=2) if isinstance(data, BaseModel) else json.dumps(data, indent=2, ensure_ascii=False)
        atomic_write_text(path, text)

    def read_json(self, path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    def preserve_source(self, document_id: str, index: int, src: Path) -> Path:
        dest_dir = self.doc_dir(document_id) / "source"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{index:02d}_{src.name}"
        if not dest.exists():
            shutil.copy2(src, dest)
            try:
                os.chmod(dest, 0o444)  # read-only: originals are immutable
            except OSError:
                pass
        return dest

    def delete_generated(self, document_id: str) -> list[str]:
        """Delete everything except the preserved sources and manifest."""
        d = self.doc_dir(document_id)
        removed = []
        for name in ("pages", "exports", "classification.json", "verification.json"):
            p = d / name
            if p.is_dir():
                shutil.rmtree(p)
                removed.append(name)
            elif p.exists():
                p.unlink()
                removed.append(name)
        return removed

    def delete_all(self, document_id: str) -> None:
        d = self.doc_dir(document_id)
        if d.exists():
            for p in d.rglob("*"):
                try:
                    os.chmod(p, 0o644 if p.is_file() else 0o755)
                except OSError:
                    pass
            shutil.rmtree(d)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
