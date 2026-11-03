"""Page-level processing orchestration.

Threading model: everything touching PyMuPDF (rendering, text-layer extraction) runs on the
calling thread, because PyMuPDF documents are not thread-safe. Only Gemini work (extraction,
review, figure description, crops, checkpoint writes) runs in a bounded worker pool, so at most
``concurrency`` page images are held in memory at once.
"""

from __future__ import annotations

import os
import threading
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable

from PIL import Image

from ..config import Settings
from ..exceptions import GeminiError, JobError, ScholarOCRError
from ..ingestion.docx_input import read_docx_blocks
from ..ingestion.loader import PageRenderer
from ..intelligence import diagrams, reviewer
from ..intelligence.extractor import VisualPageContext, convert_response, extract_page, page_doubt_note
from ..intelligence.gemini_client import GeminiClient
from ..intelligence.prompts import PROMPT_VERSION
from ..logging_config import get_logger
from ..preprocessing.cropping import detect_content_region
from ..preprocessing.enhancement import preprocess
from ..preprocessing.quality import assess
from ..schemas import (Element, ElementType, Manifest, ManifestEntry, Page, PageStatus, ProcessingOptions, Route,
                       SourceRef, TableCell, TableData)
from ..schemas.elements import MATH_TYPES, VISUAL_TYPES
from ..storage import ArtifactStore, Repository
from ..storage.artifacts import atomic_write_bytes
from ..utils import element_id, now_iso, parse_page_range
from . import host, structure
from .checkpoints import cache_key, load_page, save_page
from .routing import decide_route
from .text_layer import extract_elements

log = get_logger("pipeline")

HEARTBEAT_S = 20.0  # lock heartbeat interval while waiting on workers (stale after 120 s)
CROP_TYPES = VISUAL_TYPES | MATH_TYPES | {ElementType.TABLE, ElementType.UNCERTAIN}


@dataclass
class RunSummary:
    document_id: str
    selected_pages: list[int]
    done: int = 0
    blank: int = 0
    cached: int = 0
    failed: int = 0
    cancelled: bool = False
    routes: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    reviewed: int = 0
    awaiting: int = 0  # host mode: pages prepared and waiting for the client's model
    awaiting_pages: list[int] = field(default_factory=list)

    @property
    def finished(self) -> int:
        return self.done + self.blank + self.cached


ProgressFn = Callable[[RunSummary, str], None]


def _png(img: Image.Image) -> bytes:
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


class Pipeline:
    def __init__(self, settings: Settings, repo: Repository, store: ArtifactStore, gemini: GeminiClient, *,
                 method: str = "gemini", label: str | None = None):
        self.settings = settings
        self.repo = repo
        self.store = store
        self.gemini = gemini
        # How elements from this pipeline's vision model are labelled (extraction_method) and keyed in the
        # cache ("sampling" for MCP sampling; None = the configured key provider's model ids).
        self.method = method
        self.label = label
        self._lock = threading.Lock()

    @property
    def model(self) -> GeminiClient:
        return self.gemini

    @property
    def host_mode(self) -> bool:
        """No server-side vision model: visual pages wait for the client's model (host mode)."""
        return not self.gemini.available

    # ------------------------------------------------------------------ public
    def run(self, manifest: Manifest, options: ProcessingOptions, *, owner: str,
            should_cancel: Callable[[], bool] = lambda: False, on_progress: ProgressFn | None = None,
            model_client: GeminiClient | None = None, model_method: str = "gemini",
            model_label: str | None = None) -> RunSummary:
        """``model_client`` replaces the configured provider for this run only (MCP sampling); a separate
        pipeline instance is used so concurrent jobs never see it."""
        if model_client is not None:
            clone = Pipeline(self.settings, self.repo, self.store, model_client, method=model_method, label=model_label)
            return clone._run(manifest, options, owner=owner, should_cancel=should_cancel, on_progress=on_progress)
        return self._run(manifest, options, owner=owner, should_cancel=should_cancel, on_progress=on_progress)

    def _run(self, manifest: Manifest, options: ProcessingOptions, *, owner: str,
             should_cancel: Callable[[], bool], on_progress: ProgressFn | None) -> RunSummary:
        doc_id = manifest.document_id
        selected = parse_page_range(options.pages, len(manifest.pages))
        summary = RunSummary(document_id=doc_id, selected_pages=selected)
        lock_name = f"doc:{doc_id}"
        if not self.repo.acquire_lock(lock_name, owner):
            raise JobError(f"Document {doc_id} is already being processed by another job")
        renderer = PageRenderer(self.store, manifest)
        progress = on_progress or (lambda s, stage: None)
        workers = max(1, self.settings.concurrency)
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scholarocr") as pool:
                pending: dict[Future, int] = {}
                for n in selected:
                    if should_cancel():
                        summary.cancelled = True
                        break
                    while len(pending) >= workers:
                        # Bounded waits keep the lock heartbeat fresh during slow pages.
                        self._collect(wait(pending, timeout=HEARTBEAT_S, return_when=FIRST_COMPLETED).done, pending,
                                      summary, progress)
                        self.repo.heartbeat(lock_name, owner)
                    entry = manifest.pages[n - 1]
                    try:
                        fut = self._process_page(entry, manifest, options, renderer, pool, summary)
                    except Exception as exc:  # page-level failure must not stop the document
                        self._fail_page(doc_id, entry, getattr(exc, "partial_page", None), exc, summary)
                        fut = None
                    if fut is not None:
                        pending[fut] = n
                    else:
                        progress(summary, f"page {n}")
                    self.repo.heartbeat(lock_name, owner)
                while pending:
                    self._collect(wait(pending, timeout=HEARTBEAT_S, return_when=FIRST_COMPLETED).done, pending,
                                  summary, progress)
                    self.repo.heartbeat(lock_name, owner)
        finally:
            renderer.close()
            self.repo.release_lock(lock_name, owner)
        return summary

    # ------------------------------------------------------------------ internals
    def _collect(self, done, pending: dict, summary: RunSummary, progress: ProgressFn) -> None:
        for fut in done:
            n = pending.pop(fut)
            try:
                fut.result()
            except Exception:  # already recorded by the worker wrapper
                pass
            progress(summary, f"page {n}")

    def _process_page(self, entry: ManifestEntry, manifest: Manifest, options: ProcessingOptions,
                      renderer: PageRenderer, pool: ThreadPoolExecutor, summary: RunSummary) -> Future | None:
        doc_id = manifest.document_id
        media_type = manifest.sources[entry.source_file_index].media_type
        route, reason = decide_route(entry, options, media_type)
        label = host.HOST_LABEL if self.host_mode else self.label
        key = cache_key(entry, route, options, self.settings, self.model.available, model_label=label)
        with self._lock:
            summary.routes[route.value] = summary.routes.get(route.value, 0) + 1
        if not options.reprocess:
            existing = load_page(self.store, doc_id, entry.page_number)
            if existing and existing.cache_key == key and existing.status in (PageStatus.DONE, PageStatus.BLANK):
                self.repo.set_page(doc_id, entry.page_number, existing.status, route=route.value, cache_key=key)
                with self._lock:
                    summary.cached += 1
                return None
            if (existing and existing.cache_key == key and existing.status == PageStatus.AWAITING_TRANSCRIPTION
                    and host.load_task(self.store, doc_id, entry.page_number) is not None):
                self.repo.set_page(doc_id, entry.page_number, existing.status, route=route.value, cache_key=key)
                with self._lock:
                    summary.awaiting += 1
                    summary.awaiting_pages.append(entry.page_number)
                return None

        page = Page(page_id=entry.page_id, document_id=doc_id, page_number=entry.page_number, width_pt=entry.width_pt,
                    height_pt=entry.height_pt, route=route, cache_key=key, warnings=[f"route: {reason}"] + entry.notes)
        try:
            return self._build_page(page, entry, route, reason, options, renderer, pool, summary)
        except Exception as exc:
            exc.partial_page = page  # type: ignore[attr-defined]  # keep route/quality/images for diagnostics
            raise

    def _build_page(self, page: Page, entry: ManifestEntry, route: Route, reason: str, options: ProcessingOptions,
                    renderer: PageRenderer, pool: ThreadPoolExecutor, summary: RunSummary) -> Future | None:
        doc_id = page.document_id
        page_dir = self.store.page_dir(doc_id, entry.page_number)

        if route == Route.DOCX_TEXT:
            page.elements = self._docx_elements(entry, renderer)
            structure.annotate(page.elements)
            return self._finish(page, summary)

        original, dpi = renderer.render(entry, self.settings.render_dpi)
        orig_path = page_dir / "original.png"
        atomic_write_bytes(orig_path, _png(original))
        page.original_image = self.store.rel(doc_id, orig_path)

        if route == Route.BLANK:
            quality = assess(original, dpi)
            if quality.is_blank:
                page.status = PageStatus.BLANK
                page.quality = quality
                return self._finish(page, summary)
            # The manifest's blank flag (low-resolution check, possibly from an older ingestion) disagrees with
            # the full-resolution assessment: never skip a page that has ink.
            unblanked = entry.model_copy(deep=True)
            unblanked.profile.is_blank = False
            media_type = renderer.manifest.sources[entry.source_file_index].media_type
            route, reason = decide_route(unblanked, options, media_type)
            page.route = route
            page.warnings = [w for w in page.warnings if not w.startswith("route:") and w != "blank_page"]
            page.warnings.insert(0, f"route: {reason}")
            page.warnings.append(f"flagged blank at ingestion, but the full-resolution check found ink "
                                 f"(ink_ratio {quality.ink_ratio}); processed instead of skipped")

        if route == Route.EMBEDDED_TEXT:
            pdf_page = renderer.pdf_page(entry)
            page.elements = extract_elements(pdf_page, document_id=doc_id, page_id=entry.page_id,
                                             page_number=entry.page_number, source_file=entry.source_file,
                                             source_page_index=entry.source_page_index, page_image_rel=page.original_image)
            structure.annotate(page.elements)
            self._save_crops(page, original, dpi)
            figures = [e for e in page.elements if e.type in VISUAL_TYPES and e.bbox]
            if figures and self.model.available:
                return pool.submit(self._guard, page, summary, self._describe_figures, page, figures, original, dpi)
            return self._finish(page, summary)

        # Visual routes. Deterministic analysis always runs (and is recorded): in host mode it is all the
        # server does before handing the processed image to the client's model.
        crop_box = detect_content_region(original) if options.auto_crop else None
        if crop_box is not None:
            page.warnings.append(f"cropped to the detected document region {list(crop_box)} px; content outside it "
                                 "(app or screen chrome) is not transcribed. Disable with auto_crop=false.")
        region = original.crop(crop_box) if crop_box else original
        quality = assess(region, dpi)
        page.quality = quality
        processed, transforms, matrix = preprocess(original, quality, crop_box=crop_box)
        self._save_processed(page, processed, transforms)
        text_layer = None
        pdf_page = renderer.pdf_page(entry)
        if pdf_page is not None and entry.profile.text_chars:
            text_layer = (pdf_page.get_text("text") or "")[:6000] or None
        if self.host_mode:
            task = host.HostTask(
                route=route, reason=reason, dpi=dpi, original_size=original.size, processed_size=processed.size,
                matrix=matrix, crop_box=crop_box, text_layer=(text_layer or "")[:host.MAX_TEXT_LAYER_CHARS] or None,
                hints={"document_type": options.document_type.value if options.document_type else None,
                       "language_hint": options.language_hint, "context_hint": options.context_hint},
                review=options.review, detail=options.detail)
            host.save_task(self.store, doc_id, entry.page_number, task)
            page.status = PageStatus.AWAITING_TRANSCRIPTION
            return self._finish(page, summary)
        return pool.submit(self._guard, page, summary, self._visual_worker, page, entry, route, options, original, dpi,
                           processed, transforms, matrix, text_layer, crop_box)

    def _guard(self, page: Page, summary: RunSummary, fn, *args) -> None:
        try:
            fn(*args)
            self._finish(page, summary)
        except Exception as exc:
            entry_like = page
            self._fail_page(page.document_id, None, entry_like, exc, summary)
            raise

    def _visual_worker(self, page: Page, entry: ManifestEntry, route: Route, options: ProcessingOptions,
                       original: Image.Image, dpi: float, processed: Image.Image, transforms, matrix,
                       text_layer: str | None, crop_box: tuple[int, int, int, int] | None = None) -> None:
        doc_id = page.document_id
        hints = dict(document_type=options.document_type.value if options.document_type else None,
                     language_hint=options.language_hint, context_hint=options.context_hint, text_layer=text_layer)
        resp, info = extract_page(self.model, self.settings, route, processed, document_id=doc_id,
                                  page_number=page.page_number, **hints)
        if resp.rotation_detected in (90, 180, 270):
            # Residual rotation the deterministic check could not resolve: correct once and re-extract.
            processed, transforms, matrix = preprocess(original, page.quality, extra_rotation=resp.rotation_detected,
                                                       crop_box=crop_box)
            self._save_processed(page, processed, transforms)
            page.warnings.append(f"model reported {resp.rotation_detected} deg rotation; re-extracted after correction")
            resp, info = extract_page(self.model, self.settings, route, processed, document_id=doc_id,
                                      page_number=page.page_number, **hints)
        page.orientation = int(sum(t.params.get("degrees_ccw", 0) for t in transforms if t.step == "rotate")) % 360
        ctx = VisualPageContext(
            document_id=doc_id, page_id=page.page_id, page_number=page.page_number, source_file=entry.source_file,
            source_page_index=entry.source_page_index, page_width_pt=page.width_pt, page_height_pt=page.height_pt,
            original_dpi=dpi, original_size=original.size, processed_size=processed.size, matrix=matrix,
            page_image_rel=page.original_image, quality=page.quality,
        )
        page.elements = convert_response(resp, ctx, model=info.model, method=self.method)
        page.language = resp.page_language
        page.page_label = resp.page_label
        page.model = info.model
        page.prompt_version = PROMPT_VERSION
        if resp.notes:
            page.warnings += [f"model note: {n}" for n in resp.notes]
        page.warnings += page_doubt_note(resp)
        if info.repaired:
            page.warnings.append("extraction output required a repair request")
        structure.annotate(page.elements)
        if options.review:
            for el in reviewer.select_for_review(page.elements, detail=options.detail):
                try:
                    reviewer.review_element(self.model, self.settings, el, reviewer.crop_region(original, el, dpi),
                                            document_id=doc_id)
                except GeminiError as exc:
                    el.uncertainty_reasons.append(f"review failed: {exc.category}")
                    page.warnings.append(f"review of {el.element_id} failed ({exc.category})")
        self._save_crops(page, original, dpi)

    def _save_processed(self, page: Page, processed: Image.Image, transforms) -> None:
        proc_path = self.store.page_dir(page.document_id, page.page_number) / "processed.png"
        atomic_write_bytes(proc_path, _png(processed))
        page.processed_image = self.store.rel(page.document_id, proc_path)
        page.processed_image_size = processed.size
        page.transforms = transforms

    def _describe_figures(self, page: Page, figures: list[Element], original: Image.Image, dpi: float) -> None:
        for el in figures[: diagrams.MAX_FIGURES_PER_PAGE]:
            try:
                diagrams.describe(self.model, self.settings, el, reviewer.crop_region(original, el, dpi, pad_pt=4),
                                  document_id=page.document_id)
            except GeminiError as exc:
                el.uncertainty_reasons.append(f"figure description failed: {exc.category}")
                page.warnings.append(f"description of {el.element_id} failed ({exc.category})")
        page.model = self.settings.gemini_model
        page.prompt_version = PROMPT_VERSION

    def _save_crops(self, page: Page, original: Image.Image, dpi: float) -> None:
        regions = self.store.page_dir(page.document_id, page.page_number) / "regions"
        for el in page.elements:
            if el.type in CROP_TYPES and el.bbox is not None:
                crop = reviewer.crop_region(original, el, dpi, pad_pt=2)
                path = regions / f"{el.element_id}.png"
                atomic_write_bytes(path, _png(crop))
                el.source.region_image = self.store.rel(page.document_id, path)

    def _docx_elements(self, entry: ManifestEntry, renderer: PageRenderer) -> list[Element]:
        doc_id = renderer.manifest.document_id
        blocks = read_docx_blocks(renderer.source_path(entry))
        src = SourceRef(document_id=doc_id, page_number=entry.page_number, source_file=entry.source_file,
                        source_page_index=0)
        out: list[Element] = []
        for i, b in enumerate(blocks, 1):
            common = dict(element_id=element_id(entry.page_id, i), page_id=entry.page_id, page_number=entry.page_number,
                          reading_order=i, extraction_method="docx", source=src.model_copy())
            kind = b["kind"]
            if kind == "table":
                rows = [[TableCell(text=c, is_header=(r == 0)) for c in row] for r, row in enumerate(b["rows"])]
                out.append(Element(type=ElementType.TABLE, table=TableData(rows=rows, header_rows=1 if rows else 0),
                                   **common))
            elif kind == "title":
                out.append(Element(type=ElementType.TITLE, content=b["text"], level=0, **common))
            elif kind == "heading":
                lvl = b.get("level", 1)
                out.append(Element(type=ElementType.HEADING if lvl <= 1 else ElementType.SUBHEADING, content=b["text"],
                                   level=lvl, **common))
            elif kind == "list":
                out.append(Element(type=ElementType.LIST, content=b["text"], items=[b["text"]], **common))
            else:
                out.append(Element(type=ElementType.PARAGRAPH, content=b["text"], **common))
        return out

    def _finish(self, page: Page, summary: RunSummary) -> None:
        if page.status == PageStatus.PENDING:
            page.status = PageStatus.DONE
        page.processed_at = now_iso()
        if page.status == PageStatus.DONE and not page.elements:
            page.warnings.append("no content extracted from a non-blank page")
        save_page(self.store, page)
        self.repo.set_page(page.document_id, page.page_number, page.status, route=page.route.value if page.route else None,
                           cache_key=page.cache_key)
        with self._lock:
            if page.status == PageStatus.AWAITING_TRANSCRIPTION:
                summary.awaiting += 1
                summary.awaiting_pages.append(page.page_number)
            elif page.status == PageStatus.BLANK:
                summary.blank += 1
            else:
                summary.done += 1
            summary.reviewed += sum(1 for e in page.elements if any(r.by.startswith("review:") for r in e.revisions))
        return None

    def _fail_page(self, doc_id: str, entry: ManifestEntry | None, page: Page | None, exc: BaseException,
                   summary: RunSummary) -> None:
        n = entry.page_number if entry else page.page_number  # type: ignore[union-attr]
        msg = exc.message if isinstance(exc, ScholarOCRError) else f"{exc.__class__.__name__}: {exc}"
        if not isinstance(exc, ScholarOCRError):
            log.error("page %s/%d failed:\n%s", doc_id, n, "".join(traceback.format_exception(exc)))
        else:
            log.warning("page %s/%d failed: %s", doc_id, n, msg)
        failed = page or Page(page_id=entry.page_id, document_id=doc_id, page_number=n,  # type: ignore[union-attr]
                              width_pt=entry.width_pt, height_pt=entry.height_pt)  # type: ignore[union-attr]
        failed.status = PageStatus.FAILED
        failed.error = msg
        failed.elements = []
        failed.cache_key = None  # never reuse a failed page
        failed.processed_at = now_iso()
        save_page(self.store, failed)
        self.repo.set_page(doc_id, n, PageStatus.FAILED, route=failed.route.value if failed.route else None, error=msg)
        with self._lock:
            summary.failed += 1
            summary.errors.append(f"page {n}: {msg}")


def owner_id() -> str:
    return f"{os.getpid()}:{threading.get_ident()}"
