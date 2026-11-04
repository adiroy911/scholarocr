"""Ingestion: validate inputs, preserve sources, build the page manifest.

A *document* is an ordered collection of one or more source files (PDF, images, DOCX).
Its ID is derived from the ordered content hashes of its sources, so re-ingesting the same
files yields the same ID (which is what makes caching and resumption work). Page order is
exactly the order given by the caller (directories are expanded in natural sort order) and
is never changed afterwards.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from ..exceptions import InputError, LimitExceededError
from ..logging_config import get_logger
from ..schemas import Manifest, ManifestEntry, PageProfile, SourceFile
from ..security import PathPolicy
from ..storage.artifacts import ArtifactStore
from ..utils import natural_key, page_id, sha256_bytes, sha256_file
from . import images as img_mod
from .pdf import profile_page, render_page
from .validation import DOCX_TYPE, IMAGE_TYPES, SUPPORTED_EXTENSIONS, detect_media_type, open_pdf_checked

log = get_logger("ingestion")

HASH_DPI = 36
# Blank detection is deliberately conservative: a page wrongly classed as blank is silently skipped, while a
# blank page sent to OCR only costs one empty transcription. "Ink" is measured relative to the paper level
# (90th percentile), not against a fixed grey value: downscaling a clean digital page anti-aliases thin
# strokes to light grey, so a fixed "< 128" count can see almost no ink on a full page of text. A page is
# blank only when the cheap low-resolution check AND a higher-resolution check both find no ink.
BLANK_INK_RATIO = 0.0004  # fraction of pixels darker than paper by BLANK_INK_DELTA
BLANK_INK_DELTA = 40  # grey levels below the paper level that count as ink
BLANK_MIN_CONTRAST = 30.0  # paper minus darkest ink below this: no visible marks at all
BLANK_CONFIRM_DPI = 100  # PDF pages that look blank at HASH_DPI are re-checked at this resolution


@dataclass
class ResolvedInput:
    path: Path
    media_type: str


def expand_inputs(raw_paths: list[str | Path], policy: PathPolicy) -> list[ResolvedInput]:
    out: list[ResolvedInput] = []
    for raw in raw_paths:
        p = policy.check_file(raw)
        if p.is_dir():
            children = sorted(
                (c for c in p.iterdir() if c.is_file() and c.suffix.lower() in SUPPORTED_EXTENSIONS),
                key=lambda c: natural_key(c.name),
            )
            if not children:
                raise InputError(f"Directory {p} contains no supported files")
            for c in children:
                c = policy.check_file(c)
                out.append(ResolvedInput(c, detect_media_type(c)))
        else:
            out.append(ResolvedInput(p, detect_media_type(p)))
    if not out:
        raise InputError("No input files")
    return out


def compute_document_id(source_hashes: list[str]) -> str:
    h = hashlib.sha256("|".join(source_hashes).encode()).hexdigest()
    return f"doc_{h[:16]}"


def ink_measure(gray: np.ndarray) -> tuple[bool, float]:
    """(looks blank, ink ratio) for a greyscale array. Ink = pixels darker than the paper level by
    ``BLANK_INK_DELTA``; light-on-dark pages (median below 110) are measured inverted."""
    if gray.size == 0:
        return True, 0.0
    if float(np.median(gray)) < 110:
        gray = 255 - gray
    paper = float(np.percentile(gray, 90))
    dark = gray < paper - BLANK_INK_DELTA
    ink = float(dark.mean())
    darkest = float(np.percentile(gray[dark], 10)) if dark.any() else float(gray.min())
    return (ink < BLANK_INK_RATIO or paper - darkest < BLANK_MIN_CONTRAST), ink


def is_blank_image(img: Image.Image) -> tuple[bool, float]:
    """Full-resolution blank check (the confirmation step of ``image_signature``)."""
    return ink_measure(np.asarray(img.convert("L"), dtype=np.uint8))


def image_signature(img: Image.Image, *, confirm_full: bool = True) -> tuple[str, str, bool, float]:
    """(content hash, dhash hex, is_blank, ink_ratio).

    Hashes come from a 400 px greyscale thumbnail. ``is_blank`` needs the thumbnail AND (with
    ``confirm_full``) the image at its own resolution to agree that there is no ink; ``ink_ratio`` is from
    the higher-resolution measurement when it ran."""
    g = img.convert("L")
    small = g.copy()
    small.thumbnail((400, 400))
    arr = np.asarray(small, dtype=np.uint8)
    content_hash = sha256_bytes(arr.tobytes() + str(arr.shape).encode())
    d = np.asarray(g.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    bits = (d[:, 1:] > d[:, :-1]).flatten()
    dhash = f"{int(''.join('1' if b else '0' for b in bits), 2):016x}"
    blank, ink = ink_measure(arr)
    if blank and confirm_full and g.size != small.size:
        blank, ink = ink_measure(np.asarray(g, dtype=np.uint8))
    return content_hash, dhash, blank, ink


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def build_manifest(raw_paths: list[str | Path], *, policy: PathPolicy, store: ArtifactStore, max_pages: int,
                   rebuild: bool = False) -> Manifest:
    """Ingest ``raw_paths`` as one document. An existing manifest is reused unless ``rebuild`` is set
    (``reprocess=True``): then page profiles (blank flags, duplicates) are measured again. Page ids and
    content hashes do not change, so the document id and cached pages stay valid."""
    inputs = expand_inputs(raw_paths, policy)
    hashes = [sha256_file(i.path) for i in inputs]
    document_id = compute_document_id(hashes)
    manifest_path = store.doc_dir(document_id) / "manifest.json"
    if manifest_path.exists() and not rebuild:
        log.info("document %s already ingested; reusing manifest", document_id)
        manifest = Manifest.model_validate(store.read_json(manifest_path))
        by_hash = dict(zip(hashes, inputs))
        for src in manifest.sources:  # the stored copy may be gone: this is the same file, so put it back
            if src.sha256 in by_hash:
                store.restore_source(document_id, src.stored_path, by_hash[src.sha256].path)
        return manifest

    sources: list[SourceFile] = []
    entries: list[ManifestEntry] = []
    warnings: list[str] = []
    page_no = 0
    for idx, (inp, sha) in enumerate(zip(inputs, hashes)):
        stored = store.preserve_source(document_id, idx, inp.path)
        rel_stored = store.rel(document_id, stored)
        meta: dict = {}
        if inp.media_type == "application/pdf":
            doc = open_pdf_checked(inp.path)
            try:
                meta = {k: v for k, v in (doc.metadata or {}).items() if v}
                if getattr(doc, "is_repaired", False):
                    warnings.append(f"{inp.path.name}: PDF structure was damaged and repaired on open")
                count = doc.page_count
                if page_no + count > max_pages:
                    raise LimitExceededError(f"Document exceeds the page limit ({max_pages}, SCHOLAROCR_MAX_PAGES)")
                for i in range(count):
                    page_no += 1
                    page = doc[i]
                    prof = profile_page(page)
                    small = render_page(page, HASH_DPI)
                    chash, dh, blank_img, _ = image_signature(small, confirm_full=False)
                    text = (page.get_text("text") or "").strip()
                    if blank_img and prof.text_chars == 0:
                        # Confirm at a resolution where thin strokes survive before skipping the page.
                        blank_img, _ = is_blank_image(render_page(page, BLANK_CONFIRM_DPI))
                    prof.is_blank = prof.is_blank or (blank_img and prof.text_chars == 0)
                    entries.append(ManifestEntry(
                        page_number=page_no, page_id=page_id(document_id, page_no), source_file=inp.path.name,
                        source_file_index=idx, source_page_index=i, width_pt=page.rect.width,
                        height_pt=page.rect.height, rotation=page.rotation, content_hash=chash, dhash=dh,
                        text_hash=sha256_bytes(" ".join(text.split()).encode()) if text else None, profile=prof,
                    ))
            finally:
                doc.close()
        elif inp.media_type in IMAGE_TYPES:
            count = img_mod.image_frame_count(inp.path)
            if page_no + count > max_pages:
                raise LimitExceededError(f"Document exceeds the page limit ({max_pages}, SCHOLAROCR_MAX_PAGES)")
            for i in range(count):
                page_no += 1
                im, dpi, exif_rot = img_mod.load_frame(inp.path, i)
                eff_dpi = dpi or img_mod.DEFAULT_DPI
                chash, dh, blank_img, _ = image_signature(im)
                notes = ["exif_orientation_applied"] if exif_rot else []
                if dpi is None:
                    notes.append(f"no_dpi_metadata_assumed_{int(img_mod.DEFAULT_DPI)}")
                entries.append(ManifestEntry(
                    page_number=page_no, page_id=page_id(document_id, page_no), source_file=inp.path.name,
                    source_file_index=idx, source_page_index=i, width_pt=im.width * 72.0 / eff_dpi,
                    height_pt=im.height * 72.0 / eff_dpi, image_dpi=eff_dpi, content_hash=chash, dhash=dh,
                    profile=PageProfile(is_scanned=True, image_coverage=1.0, is_blank=blank_img), notes=notes,
                ))
        elif inp.media_type == DOCX_TYPE:
            count = 1
            page_no += 1
            entries.append(ManifestEntry(
                page_number=page_no, page_id=page_id(document_id, page_no), source_file=inp.path.name,
                source_file_index=idx, source_page_index=0, width_pt=612, height_pt=792, content_hash=sha,
                profile=PageProfile(has_text_layer=True), notes=["docx_no_page_geometry"],
            ))
            warnings.append(f"{inp.path.name}: DOCX has no fixed pages; extracted as one logical page")
        else:  # pragma: no cover - detect_media_type already rejects others
            raise InputError(f"Unsupported media type {inp.media_type}")
        sources.append(SourceFile(
            index=idx, original_path=str(inp.path), filename=inp.path.name, stored_path=rel_stored, sha256=sha,
            size_bytes=inp.path.stat().st_size, media_type=inp.media_type, page_count=count, metadata=meta,
        ))

    _flag_duplicates(entries, warnings)
    manifest = Manifest(document_id=document_id, sources=sources, pages=entries, warnings=warnings)
    store.write_json(manifest_path, manifest)
    log.info("ingested %s: %d source(s), %d page(s)", document_id, len(sources), len(entries))
    return manifest


def _flag_duplicates(entries: list[ManifestEntry], warnings: list[str]) -> None:
    seen: list[ManifestEntry] = []
    for e in entries:
        if e.profile.is_blank:
            e.notes.append("blank_page")
            continue
        for prev in seen:
            exact = prev.content_hash == e.content_hash
            near = (
                prev.dhash and e.dhash and hamming(prev.dhash, e.dhash) <= 2
                and (prev.text_hash == e.text_hash)
                and abs(prev.width_pt - e.width_pt) < 1 and abs(prev.height_pt - e.height_pt) < 1
            )
            if exact or near:
                e.duplicate_of = prev.page_number
                e.notes.append("duplicate_page" if exact else "possible_duplicate_page")
                warnings.append(
                    f"page {e.page_number} {'duplicates' if exact else 'may duplicate'} page {prev.page_number}"
                )
                break
        seen.append(e)


class PageRenderer:
    """Renders manifest pages from the preserved source copies, keeping PDFs open per instance."""

    def __init__(self, store: ArtifactStore, manifest: Manifest):
        self.store = store
        self.manifest = manifest
        self._pdfs: dict[int, object] = {}

    def render(self, entry: ManifestEntry, dpi: int) -> tuple[Image.Image, float]:
        """Return (RGB image, dpi of that image)."""
        src = self.manifest.sources[entry.source_file_index]
        path = self.store.resolve(self.manifest.document_id, src.stored_path)
        if src.media_type == "application/pdf":
            doc = self._pdfs.get(src.index)
            if doc is None:
                doc = open_pdf_checked(path)
                self._pdfs[src.index] = doc
            return render_page(doc[entry.source_page_index], dpi), float(dpi)  # type: ignore[index]
        if src.media_type in IMAGE_TYPES:
            im, _, _ = img_mod.load_frame(path, entry.source_page_index)
            return im, entry.image_dpi or img_mod.DEFAULT_DPI
        raise InputError(f"Page {entry.page_number} ({src.media_type}) has no renderable image")

    def pdf_page(self, entry: ManifestEntry):
        src = self.manifest.sources[entry.source_file_index]
        if src.media_type != "application/pdf":
            return None
        doc = self._pdfs.get(src.index)
        if doc is None:
            doc = open_pdf_checked(self.store.resolve(self.manifest.document_id, src.stored_path))
            self._pdfs[src.index] = doc
        return doc[entry.source_page_index]  # type: ignore[index]

    def source_path(self, entry: ManifestEntry) -> Path:
        src = self.manifest.sources[entry.source_file_index]
        return self.store.resolve(self.manifest.document_id, src.stored_path)

    def close(self) -> None:
        for d in self._pdfs.values():
            try:
                d.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        self._pdfs.clear()
