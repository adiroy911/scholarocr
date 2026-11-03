"""ScholarOCR service facade: the one API used by the CLI, the MCP server and Python callers.

Agent-agnostic by design: nothing here knows about Claude, Antigravity or MCP.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from . import EXTRACTION_VERSION, SCHEMA_VERSION, __version__
from .config import Settings
from .exceptions import ExportError, InputError, ModelUnavailableError, NotFoundError
from .ingestion.loader import PageRenderer, build_manifest
from .intelligence import classifier, reviewer
from .intelligence.gemini_client import GeminiClient, Transport
from .logging_config import configure_logging, get_logger
from .pipeline import host, structure
from .pipeline.checkpoints import load_page, save_page
from .pipeline.job_manager import JobManager
from .pipeline.orchestrator import Pipeline, RunSummary
from .pipeline.verification import verify as run_verification
from .schemas import (Document, DocumentType, Element, ElementType, FigureData, Job, Manifest, Page, PageStatus,
                      ProcessingOptions, Revision, VerificationReport)
from .intelligence import prompts as host_prompts
from .intelligence.extractor import VisualPageContext, convert_response, page_doubt_note
from .schemas.elements import MATH_TYPES as MATH_TYPES_SET
from .schemas.elements import MODEL_METHODS, VISUAL_TYPES
from .security import PathPolicy
from .storage import ArtifactStore, Repository
from .utils import now_iso, parse_page_range

log = get_logger("service")


class ScholarOCR:
    def __init__(self, settings: Settings | None = None, *, transport: Transport | None = None):
        self.settings = settings or Settings.from_env()
        configure_logging(self.settings.log_level)
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.repo = Repository(self.settings.db_path)
        self.store = ArtifactStore(self.settings.documents_dir)
        self.policy = PathPolicy(self.settings.allowed_dirs, self.settings.max_file_mb, env_file=self.settings.env_file)
        self.gemini = GeminiClient(self.settings, transport=transport, usage_sink=self.repo.record_call)
        self.pipeline = Pipeline(self.settings, self.repo, self.store, self.gemini)
        # Per-document model override for one synchronous run (MCP sampling): doc id -> (client, method, label).
        self._overrides: dict[str, tuple[GeminiClient, str, str]] = {}
        self.jobs = JobManager(self.repo, self._run_job)
        self.jobs.recover_stale()
        if self.settings.retention_days is not None:
            self.apply_retention()

    # ------------------------------------------------------------------ mode
    @property
    def mode(self) -> str:
        """``host`` (the MCP client's model transcribes), or the key provider in use."""
        if not self.gemini.available:
            return "host"
        p = self.settings.effective_provider
        return p if p != "host" else "gemini"  # an injected transport (tests) behaves like a key provider

    def mode_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"mode": self.mode, "provider_requested": self.settings.provider}
        if self.mode == "host":
            info["meaning"] = ("No model API key is used. Pages with a reliable text layer are extracted "
                               "deterministically; pages that need vision are prepared (rendered, cropped, "
                               "deskewed) and wait for YOUR model: get_page_image -> submit_page_content.")
        else:
            info["meaning"] = f"Server-side visual OCR with the {self.mode} provider ({self.settings.gemini_model})."
        if self.settings.provider_note:
            info["note"] = self.settings.provider_note
        return info

    # ------------------------------------------------------------------ processing
    def process(self, paths: str | Path | list[str | Path], options: ProcessingOptions | None = None, *,
                wait: bool | None = None, model_override: tuple[GeminiClient, str, str] | None = None
                ) -> dict[str, Any]:
        """Ingest file(s) as one document and extract it. Returns immediately with a job id for
        large selections unless ``wait=True``; small selections run synchronously by default.

        ``model_override`` = (client, extraction_method, cache label) runs this call's visual pages through
        another model client (MCP sampling); it forces a synchronous run."""
        options = options or ProcessingOptions()
        path_list = [paths] if isinstance(paths, (str, Path)) else list(paths)
        manifest = build_manifest(path_list, policy=self.policy, store=self.store, max_pages=self.settings.max_pages,
                                  rebuild=options.reprocess)
        selected = parse_page_range(options.pages, len(manifest.pages))
        doc_id = manifest.document_id
        existing = self.repo.get_document(doc_id)
        self.repo.upsert_document(
            doc_id, title=None, filenames=[s.filename for s in manifest.sources],
            document_type=options.document_type.value if options.document_type else None,
            page_count=len(manifest.pages), status=existing["status"] if existing else "ingested",
            data=(existing or {}).get("data") or {},
        )
        if model_override is not None:
            wait = True  # the override (an MCP sampling session) only lives for this call
            self._overrides[doc_id] = model_override
        if wait is None:
            wait = len(selected) <= self.settings.sync_page_limit
        try:
            job = self.jobs.start(doc_id, options, len(selected), background=not wait)
        finally:
            self._overrides.pop(doc_id, None)
        mode = model_override[1] if model_override is not None else self.mode
        result = {
            "document_id": doc_id,
            "job_id": job.job_id,
            "status": job.status.value,
            "mode": mode,
            "page_count": len(manifest.pages),
            "selected_pages": len(selected),
            "synchronous": bool(wait),
            "warnings": manifest.warnings,
        }
        if self.settings.provider_note and mode == "host":
            result["warnings"] = list(result["warnings"]) + [self.settings.provider_note]
        blank = self._blank_pages(manifest, selected, finished=bool(wait), force_visual=options.force_visual)
        if blank:
            result["blank_pages"] = blank
            result["warnings"] = list(result["warnings"]) + [
                f"page(s) {', '.join(map(str, blank))} skipped as blank (no ink found); nothing was extracted from "
                "them. If a skipped page has content, call ocr_document again with force_visual=true and "
                "reprocess=true, and tell the user."]
        if wait:
            result["summary"] = self.document_summary(doc_id)
            awaiting = result["summary"].get("awaiting_transcription") or []
            if awaiting:
                result["awaiting_transcription"] = awaiting
                result["next_step"] = (
                    f"Host mode: {len(awaiting)} page(s) need YOUR vision. For each, call get_page_image("
                    f"document_id='{doc_id}') (it returns the next waiting page), transcribe the image, and call "
                    "submit_page_content with the elements. Pages with a text layer are already done.")
        elif mode == "host":
            result["next_step"] = ("Poll get_document_status(job_id=...) until it is terminal. Pages that need "
                                   "vision then wait for you: get_page_image -> submit_page_content.")
        return result

    def _blank_pages(self, manifest: Manifest, selected: list[int], *, finished: bool, force_visual: bool) -> list[int]:
        """Selected pages skipped as blank: stored status after a finished run, else the manifest's flag."""
        if finished:
            out = []
            for n in selected:
                p = load_page(self.store, manifest.document_id, n)
                if p is not None and p.status == PageStatus.BLANK:
                    out.append(n)
            return out
        return [] if force_visual else [n for n in selected if manifest.pages[n - 1].profile.is_blank]

    def _run_job(self, document_id: str, options: ProcessingOptions, job_id: str, should_cancel, progress) -> RunSummary:
        manifest = self.load_manifest(document_id)
        self.repo.update_document(document_id, status="processing")
        override = self._overrides.get(document_id)
        try:
            if override is not None:
                summary = self.pipeline.run(manifest, options, owner=job_id, should_cancel=should_cancel,
                                            on_progress=progress, model_client=override[0], model_method=override[1],
                                            model_label=override[2])
            else:
                summary = self.pipeline.run(manifest, options, owner=job_id, should_cancel=should_cancel,
                                            on_progress=progress)
        except Exception:
            self.repo.update_document(document_id, status="failed")
            raise
        self._refresh_document_record(document_id, options)
        return summary

    def _refresh_document_record(self, document_id: str, options: ProcessingOptions | None = None) -> None:
        doc = self.get_document(document_id)
        statuses = [p.status for p in doc.pages]
        if all(s in (PageStatus.DONE, PageStatus.BLANK) for s in statuses):
            status = "processed"
        elif any(s == PageStatus.FAILED for s in statuses):
            status = "partial" if any(s in (PageStatus.DONE, PageStatus.BLANK) for s in statuses) else "failed"
        elif any(s == PageStatus.AWAITING_TRANSCRIPTION for s in statuses):
            status = "awaiting_transcription"
        elif any(s in (PageStatus.DONE, PageStatus.BLANK) for s in statuses):
            status = "partially_processed"
        else:
            status = "ingested"
        row = self.repo.get_document(document_id) or {}
        data = row.get("data") or {}
        data.update({"languages": doc.languages, "last_options": options.model_dump(mode="json") if options else
                     data.get("last_options")})
        self.repo.update_document(document_id, status=status, title=doc.title, data=data)

    # ------------------------------------------------------------------ jobs
    def job_status(self, job_id: str) -> dict[str, Any]:
        self.jobs.recover_stale()
        job = self.jobs.get(job_id)
        return self._job_dict(job)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._job_dict(self.jobs.cancel(job_id))

    def resume_job(self, job_id: str, *, wait: bool = False) -> dict[str, Any]:
        return self._job_dict(self.jobs.resume(job_id, background=not wait))

    def wait_for_job(self, job_id: str, timeout: float | None = None) -> dict[str, Any]:
        return self._job_dict(self.jobs.wait(job_id, timeout))

    @staticmethod
    def _job_dict(job: Job) -> dict[str, Any]:
        d = job.model_dump(mode="json")
        d["progress"] = f"{job.completed_pages}/{job.total_pages}"
        d.pop("pid", None)
        return d

    # ------------------------------------------------------------------ retrieval
    def load_manifest(self, document_id: str) -> Manifest:
        path = self.store.doc_dir(document_id) / "manifest.json"
        if not path.exists():
            raise NotFoundError(f"No document with id {document_id}")
        return Manifest.model_validate_json(path.read_text(encoding="utf-8"))

    def get_document(self, document_id: str, *, page_numbers: Iterable[int] | None = None) -> Document:
        manifest = self.load_manifest(document_id)
        row = self.repo.get_document(document_id) or {}
        wanted = set(page_numbers) if page_numbers is not None else None
        pages: list[Page] = []
        for e in manifest.pages:
            if wanted is not None and e.page_number not in wanted:
                continue
            p = load_page(self.store, document_id, e.page_number)
            if p is None:
                p = Page(page_id=e.page_id, document_id=document_id, page_number=e.page_number, width_pt=e.width_pt,
                         height_pt=e.height_pt, status=PageStatus.PENDING)
            pages.append(p)
        title = next((el.content for p in pages for el in p.elements if el.type == ElementType.TITLE), None)
        langs = sorted({p.language for p in pages if p.language} | {el.language for p in pages for el in p.elements
                                                                    if el.language})
        dtype = row.get("document_type")
        cls_path = self.store.doc_dir(document_id) / "classification.json"
        return Document(
            document_id=document_id, title=title or row.get("title"),
            original_filenames=[s.filename for s in manifest.sources],
            document_type=DocumentType(dtype) if dtype else DocumentType.UNKNOWN,
            languages=langs, page_count=len(manifest.pages),
            source_metadata={s.filename: {**s.metadata, "sha256": s.sha256, "media_type": s.media_type,
                                          "pages": s.page_count} for s in manifest.sources},
            processing={"last_options": (row.get("data") or {}).get("last_options"),
                        "classification": json.loads(cls_path.read_text()) if cls_path.exists() else None,
                        "models": sorted({p.model for p in pages if p.model}),
                        "prompt_versions": sorted({p.prompt_version for p in pages if p.prompt_version})},
            status=row.get("status", "ingested"), created_at=row.get("created_at", now_iso()),
            updated_at=row.get("updated_at"), extraction_version=EXTRACTION_VERSION, schema_version=SCHEMA_VERSION,
            pages=pages,
        )

    def document_summary(self, document_id: str) -> dict[str, Any]:
        doc = self.get_document(document_id)
        counts: dict[str, int] = {}
        for p in doc.pages:
            counts[p.status.value] = counts.get(p.status.value, 0) + 1
        elements = [el for p in doc.pages for el in p.elements]
        unc: dict[str, int] = {}
        for el in elements:
            unc[el.uncertainty.value] = unc.get(el.uncertainty.value, 0) + 1
        jobs = self.repo.jobs_for_document(document_id)
        awaiting = [p.page_number for p in doc.pages if p.status == PageStatus.AWAITING_TRANSCRIPTION]
        out_awaiting = {"awaiting_transcription": awaiting,
                        "next_step": "get_page_image -> transcribe -> submit_page_content, one page at a time"
                        } if awaiting else {}
        return {
            "document_id": document_id,
            "title": doc.title,
            "status": doc.status,
            "mode": self.mode,
            "page_count": doc.page_count,
            "pages_by_status": counts,
            **out_awaiting,
            "routes": {r: sum(1 for p in doc.pages if p.route and p.route.value == r)
                       for r in sorted({p.route.value for p in doc.pages if p.route})},
            "elements": len(elements),
            "uncertainty": unc,
            "failed_pages": [{"page": p.page_number, "error": p.error} for p in doc.pages if p.status == PageStatus.FAILED],
            "outline": structure.outline(elements)[:60],
            "latest_job": self._job_dict(jobs[0]) if jobs else None,
        }

    def get_content(self, document_id: str, *, pages: str | None = None, element_types: list[str] | None = None,
                    fmt: str = "markdown", max_chars: int = 30000, cursor: int | None = None,
                    include_ids: bool = True) -> dict[str, Any]:
        """Paginated content. ``cursor`` is the page number to continue from (from ``next_cursor``)."""
        if fmt not in ("markdown", "json", "text"):
            raise InputError("format must be markdown, json or text")
        manifest = self.load_manifest(document_id)
        selected = parse_page_range(pages, len(manifest.pages))
        if cursor is not None:
            selected = [n for n in selected if n >= int(cursor)]
        types = {ElementType(t) for t in element_types} if element_types else None
        chunks: list[Any] = []
        used = 0
        next_cursor = None
        pending_pages = []
        awaiting_pages = []
        for n in selected:
            page = load_page(self.store, document_id, n)
            if page is None:
                pending_pages.append(n)
                continue
            if page.status == PageStatus.AWAITING_TRANSCRIPTION:
                awaiting_pages.append(n)
                continue
            rendered = self._render_page(document_id, page, fmt, types, include_ids)
            size = len(rendered) if isinstance(rendered, str) else len(json.dumps(rendered))
            if chunks and used + size > max_chars:
                next_cursor = n
                break
            chunks.append(rendered)
            used += size
        out: dict[str, Any] = {"document_id": document_id, "format": fmt, "next_cursor": next_cursor,
                               "unprocessed_pages": pending_pages}
        if awaiting_pages:
            out["awaiting_transcription"] = awaiting_pages
            out["note_awaiting"] = ("these pages have no content yet: transcribe them with get_page_image -> "
                                    "submit_page_content (host mode)")
        if fmt == "json":
            out["pages"] = chunks
        else:
            out["content"] = "\n\n".join(chunks)
        if used > max_chars:
            out["note"] = "first page alone exceeds max_chars; returned in full"
        return out

    def _render_page(self, document_id: str, page: Page, fmt: str, types, include_ids: bool):
        if fmt == "json":
            d = page.model_dump(mode="json", exclude_none=True)
            if types:
                d["elements"] = [e for e in d["elements"] if e["type"] in {t.value for t in types}]
            return d
        if page.status == PageStatus.FAILED:
            return f"<!-- page {page.page_number}: extraction FAILED: {page.error} -->"
        doc_stub = Document(document_id=document_id, original_filenames=[], page_count=1, created_at=now_iso(),
                            extraction_version=EXTRACTION_VERSION, schema_version=SCHEMA_VERSION, pages=[page])
        from .export import document_to_markdown

        md = document_to_markdown(doc_stub, include_ids=include_ids and fmt == "markdown", element_types=types)
        if fmt == "text":
            return "\n".join(el.text_for_search() for el in sorted(page.elements, key=lambda e: e.reading_order)
                             if not types or el.type in types)
        return md

    COMPACT_FIELDS = ("type", "role", "content", "latex", "uncertainty", "uncertainty_reasons", "question",
                      "parent_id", "handwritten")

    def get_page(self, document_id: str, page_number: int, *, compact: bool = False,
                 fields: list[str] | None = None) -> dict[str, Any]:
        """One page's IR. ``compact``: per element only id, type, role, content, LaTeX, uncertainty (+reasons),
        question, parent and a rounded bbox; page-level images, transforms, sources and revisions are left
        out. ``fields`` keeps exactly these element fields (``element_id`` is always kept)."""
        manifest = self.load_manifest(document_id)
        if not 1 <= page_number <= len(manifest.pages):
            raise InputError(f"page must be between 1 and {len(manifest.pages)}")
        page = load_page(self.store, document_id, page_number)
        if page is None:
            return {"document_id": document_id, "page_number": page_number, "status": "pending",
                    "message": "Page not processed yet. Run ocr_document with this page range."}
        if compact or fields:
            keep = list(fields) if fields else [*self.COMPACT_FIELDS, "bbox"]
            unknown = sorted(set(keep) - set(Element.model_fields))
            if unknown:
                raise InputError(f"unknown element field(s) {unknown}; e.g. {list(self.COMPACT_FIELDS)}")
            els = []
            for el in page.elements:
                e = el.model_dump(mode="json", exclude_none=True, include={"element_id", *keep})
                if "bbox" in e:
                    e["bbox"] = [round(v, 1) for v in (el.bbox.x0, el.bbox.y0, el.bbox.x1, el.bbox.y1)]
                if not e.get("uncertainty_reasons", True):
                    e.pop("uncertainty_reasons")
                if e.get("role") == "none":
                    e.pop("role")
                if e.get("handwritten") is False:
                    e.pop("handwritten")
                els.append(e)
            d = {"document_id": document_id, "page_number": page_number, "status": page.status.value,
                 "route": page.route.value if page.route else None, "page_label": page.page_label,
                 "language": page.language, "quality_grade": page.quality.grade, "warnings": page.warnings,
                 "elements": els}
            if page.status == PageStatus.AWAITING_TRANSCRIPTION:
                d["message"] = "Awaiting transcription by the client's model: get_page_image -> submit_page_content."
            return d
        d = page.model_dump(mode="json", exclude_none=True)
        if page.status == PageStatus.AWAITING_TRANSCRIPTION:
            d["message"] = "Awaiting transcription by the client's model: get_page_image -> submit_page_content."
        return d

    def find_element(self, document_id: str, element_id: str) -> tuple[Page, Any]:
        try:
            page_number = int(element_id.rsplit("-p", 1)[1].split("-e", 1)[0])
        except (IndexError, ValueError):
            raise InputError(f"Malformed element id {element_id!r}") from None
        page = load_page(self.store, document_id, page_number)
        if page is None:
            raise NotFoundError(f"Page {page_number} of {document_id} is not processed")
        for el in page.elements:
            if el.element_id == element_id:
                return page, el
        raise NotFoundError(f"No element {element_id} in {document_id}")

    def get_source_region(self, document_id: str, *, element_id: str | None = None, page_number: int | None = None,
                          bbox: list[float] | None = None, padding_pt: float = 6.0,
                          max_side: int = 1600) -> tuple[bytes, dict[str, Any]]:
        """Return (PNG bytes, metadata) for an element's source region, a page region, or a full page."""
        el = None
        if element_id:
            page, el = self.find_element(document_id, element_id)
        elif page_number is not None:
            manifest = self.load_manifest(document_id)
            if not 1 <= page_number <= len(manifest.pages):
                raise InputError(f"page must be between 1 and {len(manifest.pages)}")
            page = load_page(self.store, document_id, page_number) or Page(
                page_id=manifest.pages[page_number - 1].page_id, document_id=document_id, page_number=page_number,
                width_pt=manifest.pages[page_number - 1].width_pt, height_pt=manifest.pages[page_number - 1].height_pt)
        else:
            raise InputError("Provide element_id, or page_number (optionally with bbox)")
        img, dpi = self._page_image(document_id, page)
        meta: dict[str, Any] = {"document_id": document_id, "page_number": page.page_number,
                                "page_size_pt": [page.width_pt, page.height_pt]}
        region = None
        if el is not None:
            meta.update({"element_id": el.element_id, "type": el.type.value, "uncertainty": el.uncertainty.value,
                         "bbox_source": el.bbox_source, "source_file": el.source.source_file,
                         "source_page_index": el.source.source_page_index, "content": el.content[:500],
                         "latex": el.latex})
            if el.bbox is not None:
                region = [el.bbox.x0, el.bbox.y0, el.bbox.x1, el.bbox.y1]
            else:
                meta["note"] = "element has no bounding box; returning the full page"
        elif bbox:
            if len(bbox) != 4:
                raise InputError("bbox must be [x0, y0, x1, y1] in PDF points")
            region = [float(v) for v in bbox]
        if region:
            k = dpi / 72.0
            x0 = max(0, int((region[0] - padding_pt) * k))
            y0 = max(0, int((region[1] - padding_pt) * k))
            x1 = min(img.width, int((region[2] + padding_pt) * k) + 1)
            y1 = min(img.height, int((region[3] + padding_pt) * k) + 1)
            if x1 <= x0 or y1 <= y0:
                raise InputError("region is outside the page")
            img = img.crop((x0, y0, x1, y1))
            meta["bbox_pt"] = region
        img.thumbnail((max_side, max_side))
        meta["image_size_px"] = list(img.size)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), meta

    def _page_image(self, document_id: str, page: Page) -> tuple[Image.Image, float]:
        if page.original_image:
            path = self.store.resolve(document_id, page.original_image)
            if path.exists():
                img = Image.open(path).convert("RGB")
                return img, img.width * 72.0 / page.width_pt
        manifest = self.load_manifest(document_id)
        renderer = PageRenderer(self.store, manifest)
        try:
            return renderer.render(manifest.pages[page.page_number - 1], self.settings.render_dpi)
        finally:
            renderer.close()

    # ------------------------------------------------------------------ verification
    def verify(self, document_id: str, *, scope: list[str] | None = None, review: bool = False) -> VerificationReport:
        reviewed: list[str] = []
        candidates: list[dict[str, Any]] = []
        if review and self.gemini.available:
            reviewed = self._review_flagged(document_id)
        elif review:
            candidates = self.host_review_candidates(document_id)
        doc = self.get_document(document_id)
        report = run_verification(doc, self.load_manifest(document_id), self.store, scope=scope,
                                  exports=self.repo.list_exports(document_id))
        report.reviewed_elements = reviewed
        if review and not self.gemini.available:
            report.review_candidates = candidates
            report.notes.append(
                f"Host mode: {len(candidates)} element(s) to re-check. For each: get_source_region(element_id) to "
                "see the original crop, then submit_review(document_id, element_id, verdict, ...)." if candidates
                else "Host mode: no element needs a review pass.")
        self.store.write_json(self.store.doc_dir(document_id) / "verification.json", report)
        self.repo.add_verification(document_id, report.passed, {"counts": report.counts(), "scope": report.scope})
        return report

    def _review_flagged(self, document_id: str) -> list[str]:
        reviewed = []
        manifest = self.load_manifest(document_id)
        for e in manifest.pages:
            page = load_page(self.store, document_id, e.page_number)
            if page is None or page.status != PageStatus.DONE:
                continue
            todo = [el for el in reviewer.select_for_review(page.elements, detail="standard")
                    if not any(r.by.startswith("review:") for r in el.revisions)]
            if not todo:
                continue
            img, dpi = self._page_image(document_id, page)
            for el in todo:
                reviewer.review_element(self.gemini, self.settings, el, reviewer.crop_region(img, el, dpi),
                                        document_id=document_id)
                reviewed.append(el.element_id)
            save_page(self.store, page)
        if reviewed:
            self.repo.update_document(document_id)
        return reviewed

    # ------------------------------------------------------------------ host mode
    def awaiting_pages(self, document_id: str) -> list[int]:
        manifest = self.load_manifest(document_id)
        out = []
        for e in manifest.pages:
            p = load_page(self.store, document_id, e.page_number)
            if p is not None and p.status == PageStatus.AWAITING_TRANSCRIPTION:
                out.append(e.page_number)
        return out

    def _host_page(self, document_id: str, page_number: int) -> tuple[Page, host.HostTask]:
        manifest = self.load_manifest(document_id)
        if not 1 <= page_number <= len(manifest.pages):
            raise InputError(f"page_number must be between 1 and {len(manifest.pages)}")
        page = load_page(self.store, document_id, page_number)
        task = host.load_task(self.store, document_id, page_number)
        resubmittable = (page is not None and page.status == PageStatus.DONE and (page.model or "").startswith("host"))
        if page is None or task is None or not (page.status == PageStatus.AWAITING_TRANSCRIPTION or resubmittable):
            status = page.status.value if page else "not processed"
            route = page.route.value if page and page.route else None
            raise InputError(f"page {page_number} does not need transcription (status={status}, route={route}). "
                             "Only pages listed in awaiting_transcription take host submissions.",
                             details={"awaiting_transcription": self.awaiting_pages(document_id)[:50]})
        return page, task

    def host_page_image(self, document_id: str, page_number: int | None = None, *, instructions: str = "auto",
                        max_side: int | None = None) -> tuple[bytes | None, str | None, dict[str, Any]]:
        """The processed image of a page awaiting transcription, plus transcription instructions.

        ``page_number=None`` picks the first waiting page. Returns ``(None, None, info)`` when nothing waits.
        ``instructions``: "full" (rules + element examples), "short", or "auto" (full until the first page of
        this document has been submitted)."""
        waiting = self.awaiting_pages(document_id)
        if page_number is None:
            if not waiting:
                return None, None, {"document_id": document_id, "status": "nothing_to_transcribe",
                                    "message": "No page is waiting for transcription. Read the result with "
                                               "get_document_content, or verify_document."}
            page_number = waiting[0]
        page, task = self._host_page(document_id, page_number)
        img = Image.open(self.store.resolve(document_id, page.processed_image)).convert("RGB")
        data, mime, size = host.encode_for_host(img, max_side or self.settings.host_image_max_side)
        if instructions == "auto":
            any_done = any((p.model or "").startswith("host") for p in self.get_document(document_id).pages
                           if p.status == PageStatus.DONE)
            instructions = "short" if any_done else "full"
        brief = host_prompts.host_page_brief(task.route, full=instructions == "full", text_layer=task.text_layer,
                                             **task.hints)
        rest = [n for n in waiting if n != page_number]
        info = {
            "document_id": document_id, "page_number": page_number, "route": task.route.value,
            "image": {"width_px": size[0], "height_px": size[1], "mime_type": mime,
                      "coordinates": "box_2d = [ymin, xmin, ymax, xmax], integers 0-1000 of this image"},
            "status": page.status.value,
            "pages_waiting_after_this": len(rest), "next_waiting_page": rest[0] if rest else None,
            "instructions": brief,
        }
        if page.status == PageStatus.DONE:
            info["note"] = "This page was already transcribed; submitting again replaces its content."
        return data, mime, info

    def submit_page_content(self, document_id: str, page_number: int, payload: dict[str, Any], *,
                            transcriber: str | None = None) -> dict[str, Any]:
        """Validate and store a host-model transcription of one page (host mode).

        ``payload`` has the PageExtractionResponse shape (elements, page_language, page_label, page_doubt,
        rotation_detected, notes). Raises ``SubmissionRejected`` with every problem; nothing is stored then."""
        page, task = self._host_page(document_id, page_number)
        resp, warnings = host.validate_submission(dict(payload))
        original = Image.open(self.store.resolve(document_id, page.original_image)).convert("RGB")
        label = f"{host.HOST_LABEL}:{transcriber}" if transcriber else host.HOST_LABEL
        if resp.rotation_detected in (90, 180, 270) and task.rotations < host.MAX_ROTATIONS:
            from .preprocessing.enhancement import preprocess

            processed, transforms, matrix = preprocess(original, page.quality, extra_rotation=resp.rotation_detected,
                                                       crop_box=task.crop_box)
            self.pipeline._save_processed(page, processed, transforms)
            task.matrix, task.processed_size, task.rotations = matrix, processed.size, task.rotations + 1
            host.save_task(self.store, document_id, page_number, task)
            page.warnings.append(f"client model reported {resp.rotation_detected} deg rotation; image re-oriented")
            save_page(self.store, page)
            return {"accepted": False, "rotated": True, "document_id": document_id, "page_number": page_number,
                    "message": f"The page image was rotated by {resp.rotation_detected} degrees. Nothing was stored. "
                               "Call get_page_image for this page again and transcribe the corrected image."}
        if resp.rotation_detected:
            warnings.append("rotation_detected ignored: the page was already re-oriented twice")
        manifest = self.load_manifest(document_id)
        entry = manifest.pages[page_number - 1]
        ctx = VisualPageContext(
            document_id=document_id, page_id=page.page_id, page_number=page_number, source_file=entry.source_file,
            source_page_index=entry.source_page_index, page_width_pt=page.width_pt, page_height_pt=page.height_pt,
            original_dpi=task.dpi, original_size=tuple(task.original_size), processed_size=tuple(task.processed_size),
            matrix=task.matrix, page_image_rel=page.original_image, quality=page.quality)
        page.elements = convert_response(resp, ctx, model=label, method=host.HOST_METHOD)
        page.language, page.page_label = resp.page_language, resp.page_label
        page.model, page.prompt_version = label, host_prompts.HOST_PROMPT_VERSION
        page.orientation = int(sum(t.params.get("degrees_ccw", 0) for t in page.transforms if t.step == "rotate")) % 360
        page.warnings = [w for w in page.warnings if not w.startswith("model note:")]
        page.warnings += [f"model note: {n}" for n in resp.notes] + page_doubt_note(resp)
        structure.annotate(page.elements)
        self.pipeline._save_crops(page, original, task.dpi)
        page.status = PageStatus.DONE
        page.error = None
        page.processed_at = now_iso()
        if not page.elements:
            page.warnings.append("no content extracted from a non-blank page")
        save_page(self.store, page)
        self.repo.set_page(document_id, page_number, page.status, route=task.route.value, cache_key=page.cache_key)
        self._refresh_document_record(document_id)
        self.repo.update_document(document_id)
        unc: dict[str, int] = {}
        for el in page.elements:
            unc[el.uncertainty.value] = unc.get(el.uncertainty.value, 0) + 1
        review = []
        if task.review:
            review = [{"element_id": el.element_id, "type": el.type.value, "uncertainty": el.uncertainty.value,
                       "reasons": el.uncertainty_reasons[:3]}
                      for el in reviewer.select_for_review(page.elements, detail=task.detail)]
        remaining = self.awaiting_pages(document_id)
        out: dict[str, Any] = {"accepted": True, "document_id": document_id, "page_number": page_number,
                               "elements": len(page.elements), "uncertainty": unc, "warnings": warnings,
                               "pages_waiting": len(remaining), "next_waiting_page": remaining[0] if remaining else None}
        if review:
            out["review_suggested"] = review
        if remaining:
            out["next_step"] = f"get_page_image(document_id='{document_id}') for page {remaining[0]}"
        else:
            out["next_step"] = ("All pages are transcribed. " + ("Optionally re-check review_suggested elements: "
                                "get_source_region(element_id) then submit_review. " if review else "")
                                + "Then read with get_document_content or run verify_document.")
        return out

    def host_review_candidates(self, document_id: str, limit: int = 40) -> list[dict[str, Any]]:
        """Elements a host model should re-check: flagged model-read elements not yet reviewed, and figures
        on text-layer pages that have no description."""
        out = []
        for p in self.get_document(document_id).pages:
            if p.status != PageStatus.DONE:
                continue
            for el in reviewer.select_for_review(p.elements, detail="standard"):
                if not any(r.by.startswith("review:") for r in el.revisions):
                    out.append({"element_id": el.element_id, "page": p.page_number, "type": el.type.value,
                                "uncertainty": el.uncertainty.value, "task": "re-check"})
            for el in p.elements:
                if (el.type in VISUAL_TYPES and el.bbox is not None and el.extraction_method not in MODEL_METHODS
                        and not (el.figure and el.figure.description)):
                    out.append({"element_id": el.element_id, "page": p.page_number, "type": el.type.value,
                                "uncertainty": el.uncertainty.value, "task": "describe figure"})
        return out[:limit]

    def submit_review(self, document_id: str, element_id: str, payload: dict[str, Any], *,
                      transcriber: str | None = None) -> dict[str, Any]:
        """Host mode second pass: apply the client model's verdict on one element (after it looked at the crop
        from get_source_region). Previous values are kept in ``revisions``."""
        page, el = self.find_element(document_id, element_id)
        resp, figure = host.validate_review(dict(payload))
        by = f"review:{host.HOST_LABEL}" + (f":{transcriber}" if transcriber else "")
        if figure is not None and el.type in VISUAL_TYPES and resp.verdict != "unreadable":
            previous = el.figure.model_dump() if el.figure else None
            labels = list(dict.fromkeys((el.figure.labels if el.figure else []) + figure.labels))
            el.figure = FigureData(description=figure.description, labels=labels, relationships=figure.relationships,
                                   figure_label=(el.figure.figure_label if el.figure else None) or figure.figure_label,
                                   kind=figure.kind)
            el.revisions.append(Revision(at=now_iso(), by=by.replace("review:", "describe:"), field="figure",
                                         previous=previous))
            el.uncertainty_reasons = [r for r in el.uncertainty_reasons if "not described" not in r]
            el.uncertainty_reasons.append("figure description written by the client model from the crop")
        else:
            reviewer.apply_review(el, resp, by=by)
            # Only a correction can introduce new LaTeX/table problems. Re-running the rules after "confirmed"
            # would re-raise the label for the very issue the reviewer just checked against the source.
            if resp.verdict == "corrected" and (el.type in MATH_TYPES_SET or el.type == ElementType.TABLE):
                from .intelligence.uncertainty import apply_rules

                apply_rules(el, quality=page.quality)  # re-check corrected LaTeX / table structure
        save_page(self.store, page)
        self.repo.update_document(document_id)
        return {"document_id": document_id, "element_id": element_id, "verdict": resp.verdict,
                "uncertainty": el.uncertainty.value, "content": el.content[:500], "latex": el.latex,
                "revisions": len(el.revisions)}

    # ------------------------------------------------------------------ export
    def export(self, document_id: str, fmt: str, *, options: dict | None = None,
               output_dir: str | Path | None = None) -> dict[str, Any]:
        from .export import FORMATS, ExportContext, export_document

        fmt = fmt.lower().replace("_", "-")
        aliases = {"md": "markdown", "pdf": "searchable-pdf", "tex": "latex", "searchable_pdf": "searchable-pdf"}
        fmt = aliases.get(fmt, fmt)
        if fmt not in FORMATS:
            raise ExportError(f"Unknown format {fmt!r}; supported: {', '.join(FORMATS)}")
        doc = self.get_document(document_id)
        not_ready = [p.page_number for p in doc.pages if p.status not in (PageStatus.DONE, PageStatus.BLANK)]
        warnings = []
        if len(not_ready) == len(doc.pages):
            raise ExportError("No processed pages to export. Run ocr_document first.")
        if not_ready:
            warnings.append(f"pages not processed or failed (omitted or image-only): {not_ready}")
        out_dir = (self.policy.check_output_dir(output_dir, self.settings.data_dir) if output_dir
                   else self.store.doc_dir(document_id) / "exports" / fmt)
        ctx = ExportContext(document=doc, manifest=self.load_manifest(document_id),
                            doc_dir=self.store.doc_dir(document_id), out_dir=Path(out_dir), options=options or {},
                            text_layer_font=self.settings.text_layer_font)
        result = export_document(ctx, fmt)
        result.warnings = warnings + list(result.warnings)
        self.repo.add_export(document_id, fmt, result.primary_file,
                             {"files": result.files, "verification": result.verification})
        return result.model_dump(mode="json")

    # ------------------------------------------------------------------ classification
    def classify(self, document_id: str, *, context_hint: str | None = None, pages: str | None = None,
                 client: GeminiClient | None = None) -> dict[str, Any]:
        """``client`` overrides the configured provider (MCP sampling)."""
        client = client or self.gemini
        if not client.available:
            raise ModelUnavailableError(
                "classify_document needs a server-side model (a Gemini/OpenRouter key, or a client with MCP "
                "sampling). In host mode, read get_document_content and classify the document yourself.")
        manifest = self.load_manifest(document_id)
        selected = parse_page_range(pages, len(manifest.pages))[:10]
        doc = self.get_document(document_id, page_numbers=selected)
        text = "\n".join(el.text_for_search() for el in doc.iter_elements())
        images = []
        if len(text.strip()) < 200:
            renderer = PageRenderer(self.store, manifest)
            try:
                for n in selected[: classifier.MAX_IMAGES]:
                    entry = manifest.pages[n - 1]
                    if not manifest.sources[entry.source_file_index].media_type.endswith("document"):
                        images.append(renderer.render(entry, 100)[0])
            finally:
                renderer.close()
        resp = classifier.classify(client, self.settings, document_id=document_id, page_count=len(manifest.pages),
                                   text_sample=text or None, page_images=images, context_hint=context_hint)
        result = {"document_id": document_id, **resp.model_dump(), "pages_considered": selected,
                  "evidence": "text" if len(text.strip()) >= 200 else "page_images", "context_hint": context_hint,
                  "classified_at": now_iso()}
        self.store.write_json(self.store.doc_dir(document_id) / "classification.json", result)
        self.repo.update_document(document_id, document_type=resp.document_type)
        return result

    # ------------------------------------------------------------------ catalogue & deletion
    def list_documents(self, *, status: str | None = None, document_type: str | None = None, query: str | None = None,
                       limit: int = 20, offset: int = 0) -> dict[str, Any]:
        rows, total = self.repo.list_documents(status=status, document_type=document_type, query=query,
                                               limit=max(1, min(limit, 200)), offset=max(0, offset))
        docs = [{k: r[k] for k in ("document_id", "title", "filenames", "document_type", "page_count", "status",
                                   "created_at", "updated_at")} for r in rows]
        nxt = offset + len(docs)
        return {"documents": docs, "total": total, "next_offset": nxt if nxt < total else None}

    def delete(self, document_id: str, *, mode: str = "generated", confirm: bool = False) -> dict[str, Any]:
        """mode="generated": delete extractions, crops and exports; keep the preserved source copy,
        manifest and record (re-processable). mode="all": also delete the preserved source copy and
        the record; requires confirm=True. The caller's own original file is never touched."""
        self.load_manifest(document_id)
        if any(j.status.value in ("running", "queued") for j in self.repo.jobs_for_document(document_id)):
            self.jobs.recover_stale()
            if any(j.status.value in ("running", "queued") for j in self.repo.jobs_for_document(document_id)):
                raise InputError("Document has a running job; cancel it first")
        if mode == "generated":
            removed = self.store.delete_generated(document_id)
            self.repo.delete_document(document_id, keep_record=True)
            self.repo.update_document(document_id, status="ingested")
            return {"document_id": document_id, "deleted": removed, "kept": ["source copy", "manifest", "record"]}
        if mode == "all":
            if not confirm:
                raise InputError("mode='all' deletes the preserved source copy too; pass confirm=true to proceed")
            self.store.delete_all(document_id)
            self.repo.delete_document(document_id, keep_record=False)
            return {"document_id": document_id, "deleted": ["everything stored by ScholarOCR"],
                    "note": "the original file at its own location was not touched"}
        raise InputError("mode must be 'generated' or 'all'")

    def apply_retention(self, *, days: float | None = None, dry_run: bool = False) -> dict[str, Any]:
        """Delete generated artifacts (extractions, crops, exports) of documents not updated for
        ``days`` (default: SCHOLAROCR_RETENTION_DAYS). Preserved source copies and manifests are kept,
        so documents remain re-processable. Documents with running jobs are skipped."""
        from datetime import datetime, timedelta, timezone

        days = self.settings.retention_days if days is None else days
        if days is None:
            raise InputError("No retention period configured (SCHOLAROCR_RETENTION_DAYS) or given")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=float(days))).isoformat(timespec="seconds")
        rows, _ = self.repo.list_documents(limit=1_000_000)
        purged, skipped = [], []
        for r in rows:
            if r["updated_at"] > cutoff or r["status"] == "ingested":
                continue
            if any(j.status.value in ("running", "queued") for j in self.repo.jobs_for_document(r["document_id"])):
                skipped.append(r["document_id"])
                continue
            if not dry_run:
                self.delete(r["document_id"], mode="generated")
            purged.append(r["document_id"])
        if purged and not dry_run:
            log.info("retention: purged generated artifacts of %d document(s)", len(purged))
        return {"cutoff": cutoff, "purged": purged, "skipped_running": skipped, "dry_run": dry_run}

    # ------------------------------------------------------------------ diagnostics
    def usage(self, document_id: str | None = None) -> dict[str, Any]:
        rows = self.repo.usage(document_id)
        out: dict[str, Any] = {"by_model_purpose": rows,
                               "totals": {k: sum(r[k] or 0 for r in rows) for k in
                                          ("calls", "attempts", "prompt_tokens", "output_tokens", "total_tokens")}}
        pricing = self._load_pricing()
        if pricing:
            cost = 0.0
            missing = set()
            for r in rows:
                price = pricing.get(r["model"])
                if not price:
                    missing.add(r["model"])
                    continue
                cost += r["prompt_tokens"] / 1e6 * price.get("input_per_million", 0)
                cost += (r["output_tokens"] + r["thought_tokens"]) / 1e6 * price.get("output_per_million", 0)
            out["estimated_cost"] = {"amount": round(cost, 4), "currency": pricing.get("_currency", "USD"),
                                     "models_without_pricing": sorted(missing),
                                     "source": str(self.settings.pricing_file)}
        else:
            out["estimated_cost"] = None
            out["note"] = "Set SCHOLAROCR_PRICING_FILE to get cost estimates (no prices are hardcoded)."
        return out

    def _load_pricing(self) -> dict | None:
        p = self.settings.pricing_file
        if not p or not Path(p).is_file():
            return None
        try:
            return json.loads(Path(p).read_text(encoding="utf-8"))
        except ValueError:
            log.warning("pricing file %s is not valid JSON", p)
            return None

    def info(self) -> dict[str, Any]:
        return {"version": __version__, "extraction_version": EXTRACTION_VERSION, "schema_version": SCHEMA_VERSION,
                "gemini_available": self.gemini.available, **self.mode_info(), "settings": self.settings.redacted()}
