"""Searchable PDF: original page appearance plus an invisible (render mode 3) text layer.

PDF source pages are copied unchanged; image source pages are rebuilt from the preserved
image at the manifest size. Invisible text is added only for elements that were read by
the model (``extraction_method == "gemini"``); text-layer pages are already searchable. When a
PDF page already carries a visible text layer (e.g. it was re-read visually because of figures or
math fonts), elements whose words are already in that layer are skipped to avoid duplicates.
"""

from __future__ import annotations

import io
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from PIL import Image

from ..exceptions import ExportError
from ..ingestion.images import load_frame
from ..ingestion.validation import DOCX_TYPE, IMAGE_TYPES
from ..schemas import Element, ElementType, ManifestEntry, Page
from .base import ExportContext, ExportResult, ordered_elements

FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/usr/local/share/fonts/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/seguisym.ttf",
    "C:/Windows/Fonts/Nirmala.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
)
FONT_SEARCH_DIRS = ("/usr/share/fonts", "/usr/local/share/fonts")
FONT_SEARCH_NAMES = ("DejaVuSans.ttf", "NotoSans-Regular.ttf")

LAYER_FONT_NAME = "SOCRTL"
MIN_FONTSIZE = 0.5
UNPLACED_FONTSIZE = 2.0
SIZE_TOLERANCE_PT = 1.0
RECALL_WARN = 0.9
COMPARE_DPI = 36

_TOKEN = re.compile(r"\w+")


@dataclass
class LayerFont:
    fontname: str
    fontfile: str | None
    font: pymupdf.Font

    @property
    def line_factor(self) -> float:
        return max(1.0, self.font.ascender - self.font.descender)

    def covers(self, ch: str) -> bool:
        if self.fontfile is None:
            return ord(ch) < 256
        return bool(self.font.has_glyph(ord(ch)))


def find_text_layer_font(explicit: Path | None, warnings: list[str]) -> Path | None:
    """First usable Unicode TTF: the configured one, well-known paths, then a font-dir search."""
    if explicit is not None:
        if Path(explicit).is_file():
            return Path(explicit)
        warnings.append(f"Configured text-layer font not found: {explicit}; searching system fonts")
    for cand in FONT_CANDIDATES:
        if Path(cand).is_file():
            return Path(cand)
    for d in FONT_SEARCH_DIRS:
        root = Path(d)
        if root.is_dir():
            for name in FONT_SEARCH_NAMES:
                hit = next(root.rglob(name), None)
                if hit is not None:
                    return hit
    return None


def load_layer_font(explicit: Path | None, warnings: list[str]) -> LayerFont:
    path = find_text_layer_font(explicit, warnings)
    if path is not None:
        try:
            return LayerFont(LAYER_FONT_NAME, str(path), pymupdf.Font(fontfile=str(path)))
        except (pymupdf.mupdf.FzErrorBase, RuntimeError, ValueError) as e:  # unreadable/unsupported font file
            warnings.append(f"Cannot load text-layer font {path} ({e.__class__.__name__})")
    warnings.append("No Unicode TrueType font found; using built-in Helvetica. Characters outside "
                    "Latin-1 may be lost from the text layer (set a text-layer font to fix this).")
    return LayerFont("helv", None, pymupdf.Font("helv"))


# ------------------------------------------------------------------ text fitting
def _word_widths(font: pymupdf.Font, paragraphs: list[list[str]]) -> tuple[list[list[float]], float]:
    return [[font.text_length(w, fontsize=1) for w in p] for p in paragraphs], font.text_length(" ", fontsize=1)


def _wrap(paragraphs: list[list[str]], widths: list[list[float]], space: float, max_units: float,
          strict: bool) -> list[str] | None:
    """Greedy wrap at unit size; ``max_units`` = box width / fontsize. None if a word cannot fit."""
    lines: list[str] = []
    for words, ws in zip(paragraphs, widths):
        cur: list[str] = []
        cur_w = 0.0
        for word, w in zip(words, ws):
            if strict and w > max_units:
                return None
            add = w if not cur else cur_w + space + w
            if cur and add > max_units:
                lines.append(" ".join(cur))
                cur, cur_w = [word], w
            else:
                cur.append(word)
                cur_w = add
        if cur:
            lines.append(" ".join(cur))
    return lines


def fit_text(text: str, lf: LayerFont, width: float, height: float) -> tuple[list[str], float]:
    """Wrap ``text`` into lines that fit ``width`` and pick the largest fontsize filling ``height``."""
    paragraphs = [p.split() for p in text.split("\n")]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        return [], 0.0
    widths, space = _word_widths(lf.font, paragraphs)

    def attempt(size: float) -> list[str] | None:
        lines = _wrap(paragraphs, widths, space, width / size, strict=True)
        if lines is None or len(lines) * size * lf.line_factor > height:
            return None
        return lines

    lo, hi = MIN_FONTSIZE, max(MIN_FONTSIZE, min(height / lf.line_factor, 144.0))
    best = attempt(lo)
    if best is None:  # does not fit even at the minimum size: keep it searchable anyway
        return _wrap(paragraphs, widths, space, width / lo, strict=False) or [], lo
    if (top := attempt(hi)) is not None:
        return top, hi
    for _ in range(24):
        mid = (lo + hi) / 2
        lines = attempt(mid)
        if lines is None:
            hi = mid
        else:
            lo, best = mid, lines
    return best, lo


def layer_text(el: Element) -> str:
    """Searchable text of an element: content, LaTeX for math, table cells row by row, list items."""
    return el.text_for_search()


def _insert_lines(page: pymupdf.Page, lf: LayerFont, lines: list[str], size: float, x0: float, y0: float,
                  height: float) -> None:
    if not lines:
        return
    pitch = height / len(lines)
    offset = max(0.0, (pitch - size * lf.line_factor) / 2) + size * lf.font.ascender
    for i, line in enumerate(lines):
        pt = pymupdf.Point(x0, y0 + i * pitch + offset) * page.derotation_matrix
        page.insert_text(pt, line, fontsize=size, fontname=lf.fontname, render_mode=3, rotate=page.rotation)


def add_text_layer(page: pymupdf.Page, elements: list[Element], lf: LayerFont) -> tuple[list[str], int]:
    """Insert invisible text for ``elements``. Returns (inserted texts, count without position)."""
    if lf.fontfile is not None:
        page.insert_font(fontname=lf.fontname, fontfile=lf.fontfile)
    rect = page.rect
    inserted: list[str] = []
    unplaced: list[str] = []
    for el in elements:
        text = layer_text(el)
        if not text.strip():
            continue
        box = el.bbox.clamp(rect.width, rect.height) if el.bbox is not None else None
        if box is None or box.width < 1 or box.height < 1:
            unplaced.append(text)
            continue
        lines, size = fit_text(text, lf, box.width, box.height)
        _insert_lines(page, lf, lines, size, box.x0, box.y0, box.height)
        inserted.append(text)
    if unplaced:
        joined = "\n".join(unplaced)
        width = max(10.0, rect.width - 4)
        paragraphs = [p.split() for p in joined.split("\n") if p.split()]
        widths, space = _word_widths(lf.font, paragraphs)
        lines = _wrap(paragraphs, widths, space, width / UNPLACED_FONTSIZE, strict=False) or []
        height = len(lines) * UNPLACED_FONTSIZE * lf.line_factor
        _insert_lines(page, lf, lines, UNPLACED_FONTSIZE, 2.0, 2.0, height)
        inserted.extend(unplaced)
    return inserted, len(unplaced)


# ------------------------------------------------------------------ verification helpers
def tokens(text: str) -> Counter[str]:
    return Counter(_TOKEN.findall(unicodedata.normalize("NFKC", text).casefold()))


def token_recall(expected: Counter[str], found: Counter[str]) -> tuple[int, int]:
    total = sum(expected.values())
    matched = sum((expected & found).values())
    return matched, total


def _pixels_equal(a: pymupdf.Page, b: pymupdf.Page) -> bool:
    pa = a.get_pixmap(dpi=COMPARE_DPI, alpha=False)
    pb = b.get_pixmap(dpi=COMPARE_DPI, alpha=False)
    return (pa.width, pa.height) == (pb.width, pb.height) and pa.samples == pb.samples


# ------------------------------------------------------------------ export
def _image_stream(path: Path, media_type: str, index: int, frames: int) -> tuple[bytes, tuple[int, int]]:
    """Image bytes for the page: the original file when it can be used as-is, else a re-encode."""
    img, _, exif_rotated = load_frame(path, index)
    with Image.open(path) as raw:
        plain_mode = raw.mode in ("RGB", "L")  # CMYK/palette/alpha files are re-encoded as RGB
    if frames == 1 and not exif_rotated and plain_mode and media_type in ("image/png", "image/jpeg"):
        return path.read_bytes(), img.size
    buf = io.BytesIO()
    if media_type == "image/jpeg":
        img.save(buf, format="JPEG", quality=95)
    else:
        img.save(buf, format="PNG")
    return buf.getvalue(), img.size


def export_searchable_pdf(ctx: ExportContext) -> ExportResult:
    manifest = ctx.manifest
    warnings: list[str] = []
    entries = sorted(manifest.pages, key=lambda e: e.page_number)
    kinds = {e.page_number: manifest.sources[e.source_file_index].media_type for e in entries}
    if entries and all(k == DOCX_TYPE for k in kinds.values()):
        raise ExportError("Searchable PDF is not applicable: every page comes from a DOCX source (no page images)")
    ir_pages: dict[int, Page] = {p.page_number: p for p in ctx.document.pages}
    lf = load_layer_font(ctx.text_layer_font, warnings)

    out = pymupdf.open()
    sources: dict[int, pymupdf.Document] = {}
    included: list[tuple[ManifestEntry, int]] = []  # (entry, output page index)
    expected_text: dict[int, str] = {}
    image_px: dict[int, tuple[int, int]] = {}
    unplaced_total = 0
    missing_glyphs: set[str] = set()
    try:
        for entry in entries:
            src = manifest.sources[entry.source_file_index]
            if src.media_type == DOCX_TYPE:
                warnings.append(f"Page {entry.page_number} comes from DOCX {src.filename}; not included in the PDF")
                continue
            path = ctx.resolve(src.stored_path)
            if not path.is_file():
                raise ExportError(f"Preserved source missing for page {entry.page_number}: {src.stored_path}")
            if src.media_type == "application/pdf":
                if src.index not in sources:
                    sources[src.index] = pymupdf.open(path, filetype="pdf")
                out.insert_pdf(sources[src.index], from_page=entry.source_page_index, to_page=entry.source_page_index)
                page = out[-1]
            elif src.media_type in IMAGE_TYPES:
                stream, px = _image_stream(path, src.media_type, entry.source_page_index, src.page_count)
                page = out.new_page(width=entry.width_pt, height=entry.height_pt)
                page.insert_image(page.rect, stream=stream, keep_proportion=False)
                image_px[entry.page_number] = px
            else:
                raise ExportError(f"Page {entry.page_number}: unsupported source type {src.media_type}")
            included.append((entry, out.page_count - 1))

            ir_page = ir_pages.get(entry.page_number)
            elements = [
                e for e in (ordered_elements(ir_page) if ir_page else [])
                if e.extraction_method == "gemini" and e.type != ElementType.UNCERTAIN
            ]
            if elements and src.media_type == "application/pdf":
                existing = tokens(sources[src.index][entry.source_page_index].get_text("text"))
                if existing:
                    kept = []
                    for e in elements:
                        exp = tokens(layer_text(e))
                        m, tot = token_recall(exp, existing)
                        if not tot or m / tot < 0.8:
                            kept.append(e)
                    skipped = len(elements) - len(kept)
                    if skipped:
                        warnings.append(f"Page {entry.page_number}: {skipped} element(s) already in the PDF text "
                                        "layer; not duplicated")
                    elements = kept
            if elements:
                texts, unplaced = add_text_layer(page, elements, lf)
                unplaced_total += unplaced
                joined = "\n".join(texts)
                expected_text[entry.page_number] = joined
                missing_glyphs.update(ch for ch in joined if not ch.isspace() and not lf.covers(ch))
        if not included:
            raise ExportError("No pages could be included in the searchable PDF")
        if lf.fontfile is not None:
            out.subset_fonts()
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        out_path = ctx.out_dir / "document.searchable.pdf"
        out.save(out_path, garbage=3, deflate=True)
    finally:
        out.close()

    if unplaced_total:
        warnings.append(f"{unplaced_total} elements without position: their text was added at the page's "
                        "top-left in tiny invisible type so it stays searchable")
    if missing_glyphs:
        sample = "".join(sorted(missing_glyphs)[:20])
        warnings.append(f"Text-layer font {Path(lf.fontfile).name if lf.fontfile else 'Helvetica'} lacks glyphs "
                        f"for {len(missing_glyphs)} character(s) ({sample}); they may not be searchable")

    try:
        verification = _verify(ctx, out_path, included, sources, expected_text, image_px, warnings)
    finally:
        for d in sources.values():
            d.close()
    verification["text_layer_font"] = lf.fontfile or "helv"
    verification["elements_without_position"] = unplaced_total
    return ExportResult(format="searchable-pdf", primary_file=str(out_path), files=[str(out_path)],
                        warnings=warnings, verification=verification)


def _verify(ctx: ExportContext, out_path: Path, included: list[tuple[ManifestEntry, int]],
            sources: dict[int, pymupdf.Document], expected_text: dict[int, str],
            image_px: dict[int, tuple[int, int]], warnings: list[str]) -> dict:
    ir_pages = {p.page_number: p for p in ctx.document.pages}
    per_page: list[dict] = []
    matched_all = total_all = 0
    sizes_ok = appearance_ok = extractable = True
    with pymupdf.open(out_path) as doc:
        count_ok = doc.page_count == len(included)
        if not count_ok:
            warnings.append(f"Output has {doc.page_count} pages, expected {len(included)}")
        for entry, idx in included:
            if idx >= doc.page_count:
                break
            page = doc[idx]
            size_ok = (abs(page.rect.width - entry.width_pt) <= SIZE_TOLERANCE_PT
                       and abs(page.rect.height - entry.height_pt) <= SIZE_TOLERANCE_PT)
            sizes_ok &= size_ok
            src = ctx.manifest.sources[entry.source_file_index]
            if src.media_type == "application/pdf":
                appearance = "identical" if _pixels_equal(sources[src.index][entry.source_page_index], page) else "changed"
            else:
                imgs = page.get_images(full=True)
                px = image_px.get(entry.page_number)
                appearance = "image_embedded" if any((i[2], i[3]) == px for i in imgs) else "image_missing"
            appearance_ok &= appearance in ("identical", "image_embedded")
            text = page.get_text("text")
            row: dict = {"page_number": entry.page_number, "size_ok": size_ok, "appearance": appearance,
                         "text_chars": len(text.strip())}
            ir_page = ir_pages.get(entry.page_number)
            has_ir_text = bool(ir_page and any(e.text_for_search().strip() for e in ir_page.elements
                                               if e.type != ElementType.UNCERTAIN))
            if has_ir_text and not text.strip():
                extractable = False
            if entry.page_number in expected_text:
                matched, total = token_recall(tokens(expected_text[entry.page_number]), tokens(text))
                matched_all += matched
                total_all += total
                row["text_layer_tokens"] = total
                row["recall"] = round(matched / total, 4) if total else None
            per_page.append(row)
    overall = round(matched_all / total_all, 4) if total_all else None
    if overall is not None and overall < RECALL_WARN:
        warnings.append(f"Text-layer recall is {overall:.2f} (< {RECALL_WARN}); some text may not be searchable")
    if not sizes_ok:
        warnings.append("Some output page sizes differ from the source by more than 1pt")
    if not appearance_ok:
        warnings.append("Some output pages do not preserve the original appearance")
    return {
        "page_count": len(included),
        "page_count_ok": count_ok,
        "page_sizes_match": sizes_ok,
        "appearance_preserved": appearance_ok,
        "text_extractable": extractable and (overall is None or overall > 0),
        "overall_recall": overall,
        "pages": per_page,
    }
