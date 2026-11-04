"""Background runs (reading documents, answering questions) and their event streams.

A run is one assistant message. Its worker thread emits events (``stage``, ``page``, ``delta``,
``document``, ``verification``, ``usage``, ``done``...) into an in-memory list the browser follows over SSE;
the final state is written to the message in ``app.db``, so history shows what happened even after a
restart.

Reading with Antigravity: the service runs in host mode, so ingestion prepares every page that needs vision
(render, preprocess, host task) and marks it ``awaiting_transcription``. The page reader starts as soon as the
first page is prepared (the job keeps preparing the rest in the background) and reads several pages at once,
one ``agy -p`` process each: at most ``Prefs.read_concurrency`` per run, and at most that many in the whole app
(a ``Limiter`` shared by every document being read). Each call gets the page image folder as its only readable
path; the JSON reply goes through ``ScholarOCR.submit_page_content`` (strict validation, conversion,
uncertainty rules, box refinement, figure crops and cleaned text for that page, storage) right after it
arrives. A rejection is sent back to the model with every problem (at most ``max_retries`` times). Pages are
stored by number, so finishing out of order changes nothing about the result. When agy reports throttling or
an exhausted quota (or two pages in a row fail), the reader pauses, halves its concurrency for the rest of the
run and tries those pages again. Stop kills every running agy process of the run.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..exceptions import InputError, ScholarOCRError, SubmissionRejected
from ..intelligence.gemini_client import TextPart
from ..intelligence.responses import PageExtractionResponse
from ..intelligence.schema_utils import to_gemini_schema
from ..logging_config import get_logger
from ..schemas import ProcessingOptions
from ..service import ScholarOCR
from . import prompts, review as review_mod
from .agy import AgyCancelled, AgyError, AgyRateLimited, AgyRunner, AgyTimeout, model_for_effort
from .config import DEFAULT_READ_CONCURRENCY, MAX_READ_CONCURRENCY, AppConfig, Prefs
from .engines import EngineStatus, ServicePool
from .store import AppStore, new_id
from .view import chat_context, document_view, verification_view

log = get_logger("webapp.runs")
TERMINAL_JOB = {"completed", "partial", "failed", "cancelled", "interrupted", "awaiting_transcription"}
CHAT_CONTEXT_CHARS = 18000  # leaves room for rules and history inside agy's command-line limit
CHAT_CONTEXT_CHARS_KEY = 120000
MAX_ROTATION_RETRIES = 2
POLL_S = 0.25
FAILURES_AS_THROTTLE = 2  # consecutive failed pages treated like a rate limit
_STAGE_PAGE = re.compile(r"^page (\d+)$")


class Cancelled(Exception):
    pass


class Limiter:
    """A counting semaphore whose capacity can change: the app-wide cap on agy page reads running at once."""

    def __init__(self, capacity: int = DEFAULT_READ_CONCURRENCY):
        self._cap = max(1, capacity)
        self._used = 0
        self._cond = threading.Condition()

    @property
    def capacity(self) -> int:
        return self._cap

    @property
    def in_use(self) -> int:
        return self._used

    def set_capacity(self, n: int) -> None:
        with self._cond:
            self._cap = max(1, min(int(n), MAX_READ_CONCURRENCY))
            self._cond.notify_all()

    def acquire(self, cancel: threading.Event) -> bool:
        with self._cond:
            while self._used >= self._cap:
                if cancel.is_set():
                    return False
                self._cond.wait(POLL_S)
            if cancel.is_set():
                return False
            self._used += 1
            return True

    def release(self) -> None:
        with self._cond:
            self._used = max(0, self._used - 1)
            self._cond.notify_all()


@dataclass
class ReadProgress:
    """Shared state of one document's page reader (what the progress line shows)."""

    total: int
    concurrency: int
    reading: set[int] = field(default_factory=set)
    read: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"reading": sorted(self.reading), "read": self.read, "total": self.total,
                    "concurrency": self.concurrency}


@dataclass
class Run:
    id: str
    conversation_id: str
    message_id: str
    kind: str  # "read" | "chat"
    events: list[dict[str, Any]] = field(default_factory=list)
    state: str = "running"
    cancel: threading.Event = field(default_factory=threading.Event)
    cond: threading.Condition = field(default_factory=threading.Condition)
    started: float = field(default_factory=time.time)
    prior_seconds: float = 0.0  # time of the earlier runs of this message (resuming)

    def emit(self, type_: str, **data: Any) -> dict[str, Any]:
        with self.cond:
            ev = {"seq": len(self.events) + 1, "type": type_, "t": round(time.time() - self.started, 2), **data}
            self.events.append(ev)
            self.cond.notify_all()
        return ev

    def since(self, seq: int) -> list[dict[str, Any]]:
        with self.cond:
            return self.events[seq:]

    def wait(self, seq: int, timeout: float) -> None:
        with self.cond:
            if len(self.events) <= seq and self.state == "running":
                self.cond.wait(timeout)

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()


AgyFactory = Callable[[], AgyRunner | None]


class RunManager:
    def __init__(self, cfg: AppConfig, store: AppStore, pool: ServicePool, agy_factory: AgyFactory, *,
                 page_schema: dict[str, Any] | None = None, threaded: bool = True):
        self.cfg = cfg
        self.store = store
        self.pool = pool
        self.agy_factory = agy_factory
        self.page_schema = page_schema or to_gemini_schema(PageExtractionResponse)
        self.threaded = threaded
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()
        self.limiter = Limiter()  # every document read by this app shares it
        self._reading: dict[str, str] = {}  # document id -> the run reading it: one run per document in the whole app

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def running(self) -> list[Run]:
        with self._lock:
            return [r for r in self._runs.values() if r.state == "running"]

    def active_for(self, conversation_id: str) -> Run | None:
        with self._lock:
            return next((r for r in self._runs.values() if r.conversation_id == conversation_id
                         and r.state == "running"), None)

    def reading_run(self, document_id: str) -> Run | None:
        with self._lock:
            run = self._runs.get(self._reading.get(document_id, ""))
            return run if run is not None and run.state == "running" else None

    def _claim_document(self, document_id: str, run: Run) -> bool:
        with self._lock:
            holder = self._runs.get(self._reading.get(document_id, ""))
            if holder is not None and holder.id != run.id and holder.state == "running":
                return False
            self._reading[document_id] = run.id
            return True

    def _release_documents(self, run: Run) -> None:
        with self._lock:
            for doc_id in [d for d, r in self._reading.items() if r == run.id]:
                del self._reading[doc_id]

    def cancel(self, run_id: str) -> bool:
        run = self.get(run_id)
        if run is None or run.state != "running":
            return False
        run.cancel.set()
        run.emit("stage", stage="cancelling", message="Stopping")
        return True

    def _start(self, run: Run, target: Callable[[], None]) -> Run:
        with self._lock:
            self._runs[run.id] = run
            # Keep finished runs for a while so a reconnecting page can replay them; drop the oldest.
            done = [r for r in self._runs.values() if r.state != "running"]
            for old in sorted(done, key=lambda r: r.started)[:-50]:
                self._runs.pop(old.id, None)
        if self.threaded:
            threading.Thread(target=target, name=f"run-{run.id}", daemon=True).start()
        else:
            target()
        return run

    # ================================================================== reading documents
    def start_read(self, conversation_id: str, groups: list[list[Path]], names: list[list[str]], *,
                   engine: EngineStatus, model: str | None, prefs: Prefs, instructions: str,
                   message_id: str) -> Run:
        run = Run(new_id("r"), conversation_id, message_id, "read")
        return self._start(run, lambda: self._read_worker(run, groups, names, engine, model, prefs, instructions))

    def start_resume(self, conversation_id: str, document_id: str, message: dict[str, Any], *, engine: EngineStatus,
                     model: str | None, prefs: Prefs) -> tuple[Run, bool]:
        """Read the pages of a document that are not accepted yet, in the message that first read it. Returns the
        run and whether it is new: a document being read already keeps that run, so there is never a double run."""
        with self._lock:
            holder = self._runs.get(self._reading.get(document_id, ""))
            if holder is not None and holder.state == "running":
                return holder, False
            if any(r.conversation_id == conversation_id and r.state == "running" for r in self._runs.values()):
                raise InputError("this conversation is still working; wait for it or stop it first")
            run = Run(new_id("r"), conversation_id, message["id"], "read")
            run.prior_seconds = float(message["data"].get("seconds_total") or 0.0)
            self._runs[run.id] = run  # registered and claimed under one lock: a second click sees this run
            self._reading[document_id] = run.id
        data = message["data"]
        data.pop("error", None)
        entry = next(e for e in data["documents"] if e["document_id"] == document_id)
        self._persist(run, data, "running")
        log.info("run %s resumes document %s in message %s", run.id, document_id, message["id"])
        self._start(run, lambda: self._read_worker(run, [[]], [entry["files"]], engine, model, prefs,
                                                   data.get("instructions") or "", data=data, resume=document_id))
        return run, True

    def _read_worker(self, run: Run, groups, names, engine: EngineStatus, model, prefs: Prefs, instructions: str, *,
                     data: dict[str, Any] | None = None, resume: str | None = None):
        fresh = data is None
        data = data if data is not None else {
            "engine": engine.id, "engine_label": engine.label, "model": model, "instructions": instructions,
            "files": names, "documents": [], "pages": {}, "usage": {}}
        if engine.id == "antigravity":
            data.update(effort=prefs.read_effort, concurrency=prefs.read_concurrency, review=prefs.review_flagged)
        if fresh:  # known from the start: a run stopped or killed before a later file can say it never started
            self._persist(run, data, "running")
        state = "done"
        try:
            run.emit("stage", stage="starting", engine=engine.id, engine_label=engine.label, model=model)
            if resume:
                self._mark_unread(data, "interrupted")  # documents that a killed run left as "running"
            svc = self.pool.get(engine.id, prefs, model)
            for gi, paths in enumerate(groups):
                run.check()
                self._read_one(run, svc, paths, names[gi], engine, model, prefs, instructions, data, resume=resume)
            if any(d.get("state") != "done" for d in data["documents"]):
                state = "partial"
        except Cancelled:
            state = "cancelled"
            self._mark_unread(data, "cancelled")
            run.emit("stage", stage="cancelled", message="Stopped. Pages read so far are kept.")
        except ScholarOCRError as e:
            state = "error"
            self._mark_unread(data, "error")
            data["error"] = {"code": e.code, "message": e.message}
            run.emit("error", code=e.code, message=e.message)
        except Exception as e:  # pragma: no cover - defensive
            self._mark_unread(data, "error")
            log.error("run %s crashed: %s", run.id, traceback.format_exc(limit=5))
            state = "error"
            data["error"] = {"code": "internal_error", "message": f"{e.__class__.__name__}: {e}"[:400]}
            run.emit("error", code="internal_error", message=data["error"]["message"])
        finally:
            self._finish(run, state, data)

    def _read_one(self, run: Run, svc: ScholarOCR, paths: list[Path], names: list[str], engine: EngineStatus,
                  model: str | None, prefs: Prefs, instructions: str, data: dict, *,
                  resume: str | None = None) -> dict[str, Any]:
        label = names[0] if len(names) == 1 else f"{names[0]} and {len(names) - 1} more"
        run.emit("stage", stage="preparing", message=f"Preparing {label}", files=names)
        options = ProcessingOptions(context_hint=instructions or None)
        result = svc.resume_document(resume, options) if resume else svc.process(paths, options, wait=False)
        doc_id, job_id = result["document_id"], result["job_id"]
        if not self._claim_document(doc_id, run):
            svc.cancel_job(job_id)
            raise InputError("this document is being read in another conversation; wait for it or stop it first")
        self.store.attach_document(run.conversation_id, doc_id)
        entry: dict[str, Any] = {"document_id": doc_id, "files": names, "page_count": result["page_count"],
                                 "warnings": result.get("warnings", []), "state": "done"}
        if resume:
            self._replace_entry(run, data, entry, result)
        else:
            data["documents"].append(entry)  # recorded now, so a stopped or failed run still lists the document
        entry["state"] = "running"
        run.emit("document_started", document_id=doc_id, page_count=result["page_count"], files=names,
                 resumed=bool(resume), already_read=len(result.get("accepted") or []))
        self._persist(run, data, "running")
        if engine.id == "antigravity":
            # pages are read while the job is still preparing the later ones
            self._transcribe_with_agy(run, svc, doc_id, job_id, engine, model, prefs, data,
                                      page_count=result["page_count"], start_pages=result.get("prepared") or [],
                                      already_read=len(result.get("accepted") or []))
        else:
            last = None
            while True:
                if run.cancel.is_set():
                    svc.cancel_job(job_id)
                job = svc.job_status(job_id)
                last = self._job_event(run, doc_id, job, last)
                if job["status"] in TERMINAL_JOB:
                    break
                time.sleep(0.3)
            if job["status"] == "cancelled" or run.cancel.is_set():
                raise Cancelled()
        run.check()
        try:
            report = svc.verify(doc_id)
            entry["verification"] = verification_view(report)
            run.emit("verification", document_id=doc_id, **entry["verification"])
        except ScholarOCRError as e:
            entry["verification"] = {"error": e.message}
        view = document_view(svc, doc_id, prefs.text_variant)
        entry["title"] = view["title"]
        waiting = [p["page_number"] for p in view["pages"] if p["status"] == "awaiting_transcription"]
        failed = [p["page_number"] for p in view["pages"] if p["status"] in ("failed", "pending", "skipped")]
        if waiting or failed:
            entry["state"] = "partial"
            entry["unread_pages"] = sorted(waiting + failed)
        else:
            entry["state"] = "done"
        run.emit("document", document_id=doc_id, view=view, state=entry["state"])
        return entry

    @staticmethod
    def _replace_entry(run: Run, data: dict, entry: dict[str, Any], result: dict[str, Any]) -> None:
        """A resumed document keeps its place in the message; accepted pages and their numbers stay as they are."""
        index = next(i for i, e in enumerate(data["documents"]) if e["document_id"] == entry["document_id"])
        old = data["documents"][index]
        entry["resumed"] = int(old.get("resumed") or 0) + 1
        entry["warnings"] = old.get("warnings") or []
        data["documents"][index] = entry
        log.info("run %s, document %s: %d page(s) accepted and kept %s; reading %s; preparing %s", run.id,
                 entry["document_id"], len(result["accepted"]), result["accepted"], result["prepared"],
                 result["to_prepare"])

    def _mark_unread(self, data: dict, state: str) -> None:
        """Documents still in progress when a run stops: keep them, with the pages that were not read."""
        for entry in data.get("documents", []):
            if entry.get("state") != "running":
                continue
            entry["state"] = state
            try:
                svc = self.pool.reader(Prefs())
                pages = [p for p in svc.get_document(entry["document_id"]).pages
                         if p.status.value not in ("done", "blank")]
                entry["unread_pages"] = [p.page_number for p in pages]
            except ScholarOCRError:
                pass

    @staticmethod
    def _job_event(run: Run, doc_id: str, job: dict, last: tuple | None) -> tuple:
        snap = (job["completed_pages"], job["failed_pages"], job.get("stage"))
        if snap != last:
            run.emit("job", document_id=doc_id, completed=job["completed_pages"], failed=job["failed_pages"],
                     total=job["total_pages"], stage=(job.get("stage") or "")[:200], status=job["status"])
        return snap

    def _transcribe_with_agy(self, run: Run, svc: ScholarOCR, doc_id: str, job_id: str, engine: EngineStatus,
                             model: str | None, prefs: Prefs, data: dict, *, page_count: int,
                             start_pages: list[int] | None = None, already_read: int = 0) -> None:
        """Read every page that waits for transcription, several at once, starting while the job still prepares
        the later pages. Returns when the job is terminal and no page is waiting or being read. Resuming:
        ``start_pages`` are prepared already, ``already_read`` were accepted earlier, and ``job_id`` is None when no
        page needs preparing."""
        model_arg, effort_arg = model_for_effort(model, prefs.read_effort, [m["id"] for m in engine.models])
        label = f"{model_arg}-{effort_arg}" if effort_arg else (model or "default")
        review = None
        if prefs.review_flagged:  # low effort gave a different verdict on every try (docs/WEB_APP.md): not read_effort
            review_model, review_effort = model_for_effort(model, prefs.chat_effort, [m["id"] for m in engine.models])
            review = review_mod.ReviewPlan(review_model, review_effort,
                                           f"{review_model}-{review_effort}" if review_effort else (model or "default"))
        self.limiter.set_capacity(prefs.read_concurrency)
        progress = ReadProgress(total=page_count, concurrency=prefs.read_concurrency, read=already_read)
        runner: AgyRunner | None = None
        dispatched: set[int] = set(start_pages or [])
        queue: list[int] = list(start_pages or [])  # pages to (re)start, in page order
        requeued: dict[int, int] = {}
        futures: dict[Future, int] = {}
        pause_until = 0.0
        backoffs = 0
        failed_streak: dict[int, dict[str, Any]] = {}  # failed pages not yet recorded (see FAILURES_AS_THROTTLE)
        last_job = None
        job_terminal = job_id is None
        cancel_sent = job_id is None
        pool = ThreadPoolExecutor(max_workers=MAX_READ_CONCURRENCY, thread_name_prefix=f"agy-{run.id}")
        try:
            while True:
                if run.cancel.is_set():
                    if not cancel_sent:
                        svc.cancel_job(job_id)
                        cancel_sent = True
                    if not futures:
                        raise Cancelled()
                if not job_terminal:
                    job = svc.job_status(job_id)
                    last_job = self._job_event(run, doc_id, job, last_job)
                    job_terminal = job["status"] in TERMINAL_JOB
                    if job["status"] == "cancelled" and not futures:
                        raise Cancelled()
                    m = _STAGE_PAGE.match(job.get("stage") or "")
                    prepared_upto = page_count if job_terminal else (int(m.group(1)) if m else 0)
                    rows = svc.repo.get_pages(doc_id)
                    for n in sorted(rows):
                        if (n <= prepared_upto and n not in dispatched
                                and rows[n].get("status") == "awaiting_transcription"):
                            dispatched.add(n)
                            queue.append(n)
                elif not dispatched and not queue and not futures:
                    # a terminal job with pages still waiting from an earlier run of the same document
                    for n in svc.awaiting_pages(doc_id):
                        if n not in dispatched:
                            dispatched.add(n)
                            queue.append(n)
                now = time.monotonic()
                while (queue and not run.cancel.is_set() and now >= pause_until
                       and len(futures) < progress.concurrency):
                    if runner is None:
                        runner = self.agy_factory()
                        if runner is None:
                            raise AgyError("Antigravity CLI (agy) is no longer on PATH")
                    queue.sort()
                    n = queue.pop(0)
                    fut = pool.submit(self._agy_page, run, runner, svc, doc_id, n, model_arg, effort=effort_arg,
                                      label=label, progress=progress, review=review)
                    futures[fut] = n
                if job_terminal and not queue and not futures:
                    break
                if not futures:
                    time.sleep(POLL_S)
                    continue
                done, _ = wait(list(futures), timeout=POLL_S, return_when=FIRST_COMPLETED)
                for fut in done:
                    n = futures.pop(fut)
                    try:
                        stats = fut.result()
                    except Cancelled:  # agy was stopped: stop the whole run (and every other page's agy)
                        run.cancel.set()
                        continue
                    if stats.get("review", {}).get("state") == "rate_limited":
                        # the page stays accepted; the reader slows down as for a throttled page read
                        delay = self.cfg.rate_limit_backoff_s * (2 ** min(backoffs, 4))
                        backoffs += 1
                        pause_until = max(pause_until, time.monotonic() + delay)
                        with progress.lock:
                            progress.concurrency = max(1, progress.concurrency // 2)
                        run.emit("stage", stage="throttled", document_id=doc_id, concurrency=progress.concurrency,
                                 pause_seconds=round(delay), message=(
                                     f"Antigravity reported rate limiting or quota during a review; reviews are off "
                                     f"for this document, waiting {round(delay)} s, then reading "
                                     f"{progress.concurrency} page{'s' if progress.concurrency > 1 else ''} at a time"))
                    status = stats.get("status")
                    throttled = status == "rate_limited"
                    retry_pages = [n]
                    if status == "error" and requeued.get(n, 0) == 0:
                        # a failure on its own is final, unless the next page fails too: then both are retried
                        failed_streak[n] = stats
                        if len(failed_streak) < FAILURES_AS_THROTTLE:
                            continue
                        throttled, retry_pages = True, sorted(failed_streak)
                        failed_streak.clear()
                    elif status != "rate_limited":
                        self._record_pages(run, data, doc_id, failed_streak)
                        failed_streak.clear()
                    if throttled and requeued.get(n, 0) < self.cfg.rate_limit_retries and not run.cancel.is_set():
                        for p in retry_pages:
                            requeued[p] = requeued.get(p, 0) + 1
                            queue.append(p)
                        self._add_usage(data, stats)  # the tokens of the attempt that is repeated still count
                        delay = self.cfg.rate_limit_backoff_s * (2 ** min(backoffs, 4))
                        backoffs += 1
                        pause_until = max(pause_until, time.monotonic() + delay)
                        with progress.lock:
                            progress.concurrency = max(1, progress.concurrency // 2)
                        reason = "rate limiting or quota" if status == "rate_limited" else "repeated failures"
                        run.emit("stage", stage="throttled", document_id=doc_id, concurrency=progress.concurrency,
                                 pause_seconds=round(delay), message=(
                                     f"Antigravity reported {reason}; waiting {round(delay)} s, then reading "
                                     f"{progress.concurrency} page{'s' if progress.concurrency > 1 else ''} at a time"))
                        for p in retry_pages:
                            run.emit("page", document_id=doc_id, page=p, status="waiting", message=stats.get("error"),
                                     **progress.snapshot())
                        continue
                    if status == "rate_limited":  # out of retries: report it like any other failure
                        stats["status"] = "error"
                        run.emit("page", document_id=doc_id, page=n, status="error", message=stats.get("error"),
                                 **progress.snapshot())
                    self._record_pages(run, data, doc_id, {n: stats})
                if job_terminal and not queue and not futures and failed_streak:
                    self._record_pages(run, data, doc_id, failed_streak)
                    failed_streak.clear()
        finally:
            if futures:  # an error ended the reader: every agy process still running sees run.cancel and is killed
                run.cancel.set()
                wait(list(futures), timeout=60)
            pool.shutdown(wait=False, cancel_futures=True)
            # page results in page order, whatever order they finished in
            data["pages"] = dict(sorted(data["pages"].items(), key=lambda kv: (kv[0].rsplit(":", 1)[0],
                                                                                int(kv[0].rsplit(":", 1)[1]))))

    @staticmethod
    def _add_usage(data: dict, stats: dict) -> None:
        for k, v in stats.get("usage", {}).items():
            data["usage"][k] = data["usage"].get(k, 0) + v

    def _record_pages(self, run: Run, data: dict, doc_id: str, results: dict[int, dict[str, Any]]) -> None:
        for n, stats in results.items():
            data["pages"][f"{doc_id}:{n}"] = stats
            self._add_usage(data, stats)
        if results:
            self._persist(run, data, "running")

    def _review_page(self, run: Run, runner: AgyRunner, svc: ScholarOCR, doc_id: str, n: int, suggested: list[dict],
                     plan: review_mod.ReviewPlan, stats: dict[str, Any], emit: Callable[..., None]) -> None:
        """The accepted page's second look, in its worker: it keeps the page's slot under the shared limit, so
        there is still one agy process per slot. Whatever happens, the page stays accepted."""
        ids = [s["element_id"] for s in suggested]
        emit("reviewing", flagged=len(ids))

        def on_usage(usage: dict[str, int], **kw: Any) -> None:
            run.emit("usage", document_id=doc_id, page=n, usage=usage, review=True, **kw)

        result = review_mod.review_page(runner, svc, doc_id, n, ids, plan, run_dir=self.cfg.runs_dir / f"{run.id}-p{n}-review",
                                        timeout_s=self.cfg.review_timeout_s, cancel=run.cancel, on_usage=on_usage)
        stats["review"] = result
        for k, v in result["usage"].items():
            stats["usage"][k] = stats["usage"].get(k, 0) + v
        emit("reviewed", review={k: result[k] for k in ("state", "flagged", "confirmed", "corrected", "unreadable",
                                                        "rejected", "not_reviewed", "seconds")})

    def _agy_page(self, run: Run, runner: AgyRunner, svc: ScholarOCR, doc_id: str, n: int, model: str | None, *,
                  effort: str | None = None, label: str | None = None, progress: ReadProgress | None = None,
                  review: review_mod.ReviewPlan | None = None) -> dict[str, Any]:
        progress = progress or ReadProgress(total=1, concurrency=1)
        stats: dict[str, Any] = {"page": n, "attempts": 0, "usage": {}, "seconds": 0.0, "status": "reading"}
        if not self.limiter.acquire(run.cancel):
            raise Cancelled()
        log.info("run %s reads page %d of document %s", run.id, n, doc_id)
        with progress.lock:
            progress.reading.add(n)
        index = n

        def emit(status: str, **kw: Any) -> None:
            run.emit("page", document_id=doc_id, page=n, index=index, status=status, **progress.snapshot(), **kw)

        emit("reading", attempt=1)
        run_dir = self.cfg.runs_dir / f"{run.id}-p{n}"
        img_dir = run_dir / "page"
        img_dir.mkdir(parents=True, exist_ok=True)
        retries = rotations = 0
        errors: list[dict] | None = None
        previous: Any = None
        conversation: str | None = None
        started = time.monotonic()
        transcriber = f"antigravity:{label or model or 'default'}"
        stats["model"] = label or model
        try:
            while True:
                run.check()
                img, mime, info = svc.host_page_image(doc_id, n, instructions="none")
                if img is None:
                    stats["status"] = "done"
                    return stats
                brief = prompts.page_brief(info["task"])
                image_path = img_dir / ("page.png" if mime == "image/png" else "page.jpg")
                image_path.write_bytes(img)
                image_ref = str(image_path.resolve()).replace("\\", "/")
                if errors is None:
                    prompt = prompts.page_prompt(image_ref, brief)
                    resume = None
                elif conversation:
                    prompt = prompts.retry_prompt(errors, previous, image_path=None, brief=None)
                    resume = conversation
                else:
                    prompt = prompts.retry_prompt(errors, previous, image_path=image_ref, brief=brief)
                    resume = None
                stats["attempts"] += 1
                try:
                    res = runner.run(prompt, run_dir=run_dir, model=model, read_dirs=[img_dir],
                                     schema=self.page_schema, timeout_s=self.cfg.page_timeout_s, cancel=run.cancel,
                                     conversation_id=resume, effort=effort)
                except AgyCancelled:
                    raise Cancelled() from None
                except AgyRateLimited as e:
                    stats.update(status="rate_limited", error=e.message)
                    return stats
                except AgyError:
                    if resume is not None:  # resuming failed: retry once more with a full prompt
                        conversation = None
                        emit("retrying", attempt=stats["attempts"] + 1, message="Retrying with the full page prompt")
                        continue
                    raise
                for k, v in res.usage.items():
                    stats["usage"][k] = stats["usage"].get(k, 0) + v
                stats.setdefault("calls", []).append({"usage": res.usage, "agy_seconds": res.duration_s,
                                                      "wall_seconds": res.wall_s})
                run.emit("usage", document_id=doc_id, page=n, usage=res.usage, agy_seconds=res.duration_s,
                         wall_seconds=res.wall_s)
                payload = res.structured if isinstance(res.structured, dict) else None
                if payload is None:
                    errors = [{"path": "(root)", "problem": "the reply was not a JSON object",
                               "fix": "reply with the page JSON object only"}]
                    previous = res.response[:6000]
                else:
                    try:
                        out = svc.submit_page_content(doc_id, n, payload, transcriber=transcriber)
                    except SubmissionRejected as e:
                        errors = list((e.details or {}).get("errors") or [{"path": "(root)", "problem": e.message}])
                        previous = payload
                    else:
                        if out.get("rotated"):
                            rotations += 1
                            if rotations > MAX_ROTATION_RETRIES:
                                raise AgyError("the page kept being reported as rotated")
                            errors, previous, conversation = None, None, None
                            emit("rotated", attempt=stats["attempts"] + 1, message="Page re-oriented; reading it again")
                            continue
                        log.info("run %s: page %d of document %s accepted", run.id, n, doc_id)
                        stats.update(status="done", elements=out.get("elements"), uncertainty=out.get("uncertainty"),
                                     warnings=(out.get("warnings") or [])[:5])
                        with progress.lock:
                            progress.reading.discard(n)
                            progress.read += 1
                        emit("done", elements=out.get("elements"), uncertainty=out.get("uncertainty"),
                             attempts=stats["attempts"])
                        if review is not None and out.get("review_suggested"):
                            self._review_page(run, runner, svc, doc_id, n, out["review_suggested"], review, stats, emit)
                        return stats
                retries += 1
                if retries > self.cfg.max_retries:
                    stats.update(status="rejected", errors=errors[:10])
                    with progress.lock:
                        progress.reading.discard(n)
                    emit("rejected", errors=errors[:10], attempts=stats["attempts"],
                         message=f"Rejected {retries} times; the page stays unread")
                    return stats
                conversation = res.conversation_id
                emit("retrying", attempt=stats["attempts"] + 1, errors=errors[:10],
                     message=f"Validator found {len(errors)} problem(s); asking again")
        except AgyTimeout as e:
            stats.update(status="timeout", error=e.message)
            with progress.lock:
                progress.reading.discard(n)
            emit("timeout", message=e.message)
            return stats
        except AgyError as e:
            stats.update(status="error", error=e.message)
            with progress.lock:
                progress.reading.discard(n)
            emit("error", message=e.message)
            return stats
        finally:
            with progress.lock:
                progress.reading.discard(n)
            self.limiter.release()
            stats["seconds"] = round(time.monotonic() - started, 1)
            shutil.rmtree(run_dir, ignore_errors=True)

    # ================================================================== chat
    def start_chat(self, conversation_id: str, question: str, *, engine: EngineStatus, model: str | None,
                   prefs: Prefs, message_id: str) -> Run:
        run = Run(new_id("r"), conversation_id, message_id, "chat")
        return self._start(run, lambda: self._chat_worker(run, question, engine, model, prefs))

    def _history(self, conversation_id: str, exclude: str) -> list[dict[str, str]]:
        out = []
        for m in self.store.messages(conversation_id):
            if m["id"] == exclude or m["kind"] not in ("text", "chat") or not m["text"]:
                continue
            out.append({"role": "user" if m["role"] == "user" else "assistant", "text": m["text"][:1500]})
        return out[-6:-1] if out else []  # the last user message is the question itself

    def _chat_worker(self, run: Run, question: str, engine: EngineStatus, model: str | None, prefs: Prefs):
        data: dict[str, Any] = {"engine": engine.id, "engine_label": engine.label, "model": model, "usage": {}}
        text = ""
        state = "done"
        try:
            run.emit("stage", stage="thinking", engine=engine.id, engine_label=engine.label, model=model)
            docs = self.store.documents(run.conversation_id)
            reader = self.pool.reader(prefs)
            budget = CHAT_CONTEXT_CHARS if engine.id == "antigravity" else CHAT_CONTEXT_CHARS_KEY
            context, truncated, _ = chat_context(reader, docs, budget)
            history = self._history(run.conversation_id, run.message_id)
            data["grounded_in"] = docs[:5]
            data["truncated"] = truncated
            if engine.id == "antigravity":
                runner = self.agy_factory()
                if runner is None:
                    raise AgyError("Antigravity CLI (agy) is no longer on PATH")
                prompt = prompts.chat_prompt(question, context, history, truncated=truncated,
                                             instructions=prefs.saved_instructions)
                if len(prompt) > 28000:
                    context = context[: max(0, len(context) - (len(prompt) - 27500))] + "\n[... truncated ...]"
                    data["truncated"] = True
                    prompt = prompts.chat_prompt(question, context, history, truncated=True,
                                                 instructions=prefs.saved_instructions)
                run_dir = self.cfg.runs_dir / f"{run.id}-chat"
                chunks: list[str] = []

                def on_delta(s: str) -> None:
                    chunks.append(s)
                    run.emit("delta", text=s)

                model_arg, effort_arg = model_for_effort(model, prefs.chat_effort, [m["id"] for m in engine.models])
                try:
                    res = runner.run(prompt, run_dir=run_dir, model=model_arg, read_dirs=[], schema=None,
                                     timeout_s=self.cfg.chat_timeout_s, cancel=run.cancel, on_delta=on_delta,
                                     effort=effort_arg)
                except AgyCancelled:
                    text = "".join(chunks)
                    raise Cancelled() from None
                finally:
                    shutil.rmtree(run_dir, ignore_errors=True)
                text = res.response or "".join(chunks)
                data["usage"] = res.usage
                data["seconds"] = res.wall_s
                run.emit("usage", usage=res.usage, wall_seconds=res.wall_s, agy_seconds=res.duration_s)
            else:
                svc = self.pool.get(engine.id, prefs, model)
                parts = [TextPart(prompts.chat_prompt(question, context, history, truncated=truncated,
                                                      instructions=prefs.saved_instructions))]
                started = time.monotonic()
                answer, info = svc.gemini.generate_structured(
                    purpose="app_chat", model=model or svc.settings.gemini_model, system=prompts.CHAT_SYSTEM,
                    parts=parts, schema=prompts.ChatAnswer, prompt_version=prompts.APP_CHAT_PROMPT_VERSION,
                    document_id=docs[0] if docs else None, media_resolution=None, temperature=0.2)
                run.check()
                text = answer.answer
                data["cited_pages"] = answer.cited_pages
                data["usage"] = dict(info.usage)
                data["seconds"] = round(time.monotonic() - started, 2)
                run.emit("delta", text=text)
                run.emit("usage", usage=info.usage, wall_seconds=data["seconds"])
        except Cancelled:
            state = "cancelled"
            run.emit("stage", stage="cancelled", message="Stopped")
        except ScholarOCRError as e:
            state = "error"
            data["error"] = {"code": e.code, "message": e.message}
            run.emit("error", code=e.code, message=e.message)
        except Exception as e:  # pragma: no cover - defensive
            log.error("chat run %s crashed: %s", run.id, traceback.format_exc(limit=5))
            state = "error"
            data["error"] = {"code": "internal_error", "message": f"{e.__class__.__name__}: {e}"[:400]}
            run.emit("error", code="internal_error", message=data["error"]["message"])
        finally:
            self.store.update_message(run.message_id, text=text)
            self._finish(run, state, data)

    # ================================================================== bookkeeping
    def _persist(self, run: Run, data: dict, state: str) -> None:
        try:
            self.store.update_message(run.message_id, data=data, state=state)
        except Exception:  # pragma: no cover
            log.exception("could not save run state")

    def _finish(self, run: Run, state: str, data: dict) -> None:
        data["state"] = state
        data["seconds_total"] = round(time.time() - run.started + run.prior_seconds, 1)
        self._persist(run, data, state)
        self._release_documents(run)
        with run.cond:
            run.state = state
        run.emit("done", state=state, data=data)
        self.store.touch(run.conversation_id)
