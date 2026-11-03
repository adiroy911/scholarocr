"""Background runs (reading documents, answering questions) and their event streams.

A run is one assistant message. Its worker thread emits events (``stage``, ``page``, ``delta``,
``document``, ``verification``, ``usage``, ``done``...) into an in-memory list the browser follows over SSE;
the final state is written to the message in ``app.db``, so history shows what happened even after a
restart.

Reading with Antigravity: the service runs in host mode, so after ingestion every page that needs vision is
``awaiting_transcription`` with a prepared image. For each such page the worker calls ``agy -p`` with the image
folder as the only readable path, then submits the JSON reply through ``ScholarOCR.submit_page_content``:
the same strict validation, conversion, uncertainty rules and storage as an MCP submission. A rejection is
sent back to the model with every problem (at most ``max_retries`` times).
"""

from __future__ import annotations

import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..exceptions import ScholarOCRError, SubmissionRejected
from ..intelligence.gemini_client import TextPart
from ..intelligence.responses import PageExtractionResponse
from ..intelligence.schema_utils import to_gemini_schema
from ..logging_config import get_logger
from ..schemas import ProcessingOptions
from ..service import ScholarOCR
from . import prompts
from .agy import AgyCancelled, AgyError, AgyRunner, AgyTimeout
from .config import AppConfig, Prefs
from .engines import EngineStatus, ServicePool
from .store import AppStore, new_id
from .view import chat_context, document_view, verification_view

log = get_logger("webapp.runs")
TERMINAL_JOB = {"completed", "partial", "failed", "cancelled", "interrupted", "awaiting_transcription"}
CHAT_CONTEXT_CHARS = 18000  # leaves room for rules and history inside agy's command-line limit
CHAT_CONTEXT_CHARS_KEY = 120000
MAX_ROTATION_RETRIES = 2


class Cancelled(Exception):
    pass


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

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def active_for(self, conversation_id: str) -> Run | None:
        with self._lock:
            return next((r for r in self._runs.values() if r.conversation_id == conversation_id
                         and r.state == "running"), None)

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

    def _read_worker(self, run: Run, groups, names, engine: EngineStatus, model, prefs: Prefs, instructions: str):
        data: dict[str, Any] = {"engine": engine.id, "engine_label": engine.label, "model": model,
                                "instructions": instructions, "documents": [], "pages": {}, "usage": {}}
        state = "done"
        try:
            run.emit("stage", stage="starting", engine=engine.id, engine_label=engine.label, model=model)
            svc = self.pool.get(engine.id, prefs, model)
            for gi, paths in enumerate(groups):
                run.check()
                self._read_one(run, svc, paths, names[gi], engine, model, prefs, instructions, data)
            if any(d.get("state") == "partial" for d in data["documents"]):
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
                  model: str | None, prefs: Prefs, instructions: str, data: dict) -> dict[str, Any]:
        label = names[0] if len(names) == 1 else f"{names[0]} and {len(names) - 1} more"
        run.emit("stage", stage="preparing", message=f"Preparing {label}", files=names)
        options = ProcessingOptions(context_hint=instructions or None)
        result = svc.process(paths, options, wait=False)
        doc_id, job_id = result["document_id"], result["job_id"]
        self.store.attach_document(run.conversation_id, doc_id)
        entry: dict[str, Any] = {"document_id": doc_id, "files": names, "page_count": result["page_count"],
                                 "warnings": result.get("warnings", []), "state": "done"}
        data["documents"].append(entry)  # recorded now, so a stopped or failed run still lists the document
        entry["state"] = "running"
        run.emit("document_started", document_id=doc_id, page_count=result["page_count"], files=names)
        self._persist(run, data, "running")
        last = None
        while True:
            if run.cancel.is_set():
                svc.cancel_job(job_id)
            job = svc.job_status(job_id)
            snap = (job["completed_pages"], job["failed_pages"], job.get("stage"))
            if snap != last:
                run.emit("job", document_id=doc_id, completed=job["completed_pages"], failed=job["failed_pages"],
                         total=job["total_pages"], stage=(job.get("stage") or "")[:200], status=job["status"])
                last = snap
            if job["status"] in TERMINAL_JOB:
                break
            time.sleep(0.3)
        if job["status"] == "cancelled" or run.cancel.is_set():
            raise Cancelled()
        if engine.id == "antigravity":
            self._transcribe_with_agy(run, svc, doc_id, model, data)
        run.check()
        try:
            report = svc.verify(doc_id)
            entry["verification"] = verification_view(report)
            run.emit("verification", document_id=doc_id, **entry["verification"])
        except ScholarOCRError as e:
            entry["verification"] = {"error": e.message}
        view = document_view(svc, doc_id)
        entry["title"] = view["title"]
        waiting = [p["page_number"] for p in view["pages"] if p["status"] == "awaiting_transcription"]
        failed = [p["page_number"] for p in view["pages"] if p["status"] == "failed"]
        if waiting or failed:
            entry["state"] = "partial"
            entry["unread_pages"] = sorted(waiting + failed)
        else:
            entry["state"] = "done"
        run.emit("document", document_id=doc_id, view=view, state=entry["state"])
        return entry

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

    def _transcribe_with_agy(self, run: Run, svc: ScholarOCR, doc_id: str, model: str | None, data: dict) -> None:
        waiting = svc.awaiting_pages(doc_id)
        if not waiting:
            return
        runner = self.agy_factory()
        if runner is None:
            raise AgyError("Antigravity CLI (agy) is no longer on PATH")
        total = len(waiting)
        for i, n in enumerate(waiting, start=1):
            run.check()
            stats = self._agy_page(run, runner, svc, doc_id, n, model, index=i, total=total)
            data["pages"][f"{doc_id}:{n}"] = stats
            for k, v in stats.get("usage", {}).items():
                data["usage"][k] = data["usage"].get(k, 0) + v
            self._persist(run, data, "running")

    def _agy_page(self, run: Run, runner: AgyRunner, svc: ScholarOCR, doc_id: str, n: int, model: str | None, *,
                  index: int, total: int) -> dict[str, Any]:
        stats: dict[str, Any] = {"page": n, "attempts": 0, "usage": {}, "seconds": 0.0, "status": "reading"}
        run.emit("page", document_id=doc_id, page=n, index=index, total=total, status="reading", attempt=1)
        run_dir = self.cfg.runs_dir / f"{run.id}-p{n}"
        img_dir = run_dir / "page"
        img_dir.mkdir(parents=True, exist_ok=True)
        retries = rotations = 0
        errors: list[dict] | None = None
        previous: Any = None
        conversation: str | None = None
        started = time.monotonic()
        try:
            while True:
                run.check()
                img, mime, info = svc.host_page_image(doc_id, n, instructions="full")
                if img is None:
                    stats["status"] = "done"
                    return stats
                image_path = img_dir / ("page.png" if mime == "image/png" else "page.jpg")
                image_path.write_bytes(img)
                image_ref = str(image_path.resolve()).replace("\\", "/")
                if errors is None:
                    prompt = prompts.page_prompt(image_ref, info["instructions"])
                    resume = None
                elif conversation:
                    prompt = prompts.retry_prompt(errors, previous, image_path=None, brief=None)
                    resume = conversation
                else:
                    prompt = prompts.retry_prompt(errors, previous, image_path=image_ref, brief=info["instructions"])
                    resume = None
                stats["attempts"] += 1
                try:
                    res = runner.run(prompt, run_dir=run_dir, model=model, read_dirs=[img_dir],
                                     schema=self.page_schema, timeout_s=self.cfg.page_timeout_s, cancel=run.cancel,
                                     conversation_id=resume)
                except AgyCancelled:
                    raise Cancelled() from None
                except AgyError:
                    if resume is not None:  # resuming failed: retry once more with a full prompt
                        conversation = None
                        run.emit("page", document_id=doc_id, page=n, index=index, total=total, status="retrying",
                                 attempt=stats["attempts"] + 1, message="Retrying with the full page prompt")
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
                        out = svc.submit_page_content(doc_id, n, payload, transcriber=f"antigravity:{model or 'default'}")
                    except SubmissionRejected as e:
                        errors = list((e.details or {}).get("errors") or [{"path": "(root)", "problem": e.message}])
                        previous = payload
                    else:
                        if out.get("rotated"):
                            rotations += 1
                            if rotations > MAX_ROTATION_RETRIES:
                                raise AgyError("the page kept being reported as rotated")
                            errors, previous, conversation = None, None, None
                            run.emit("page", document_id=doc_id, page=n, index=index, total=total, status="rotated",
                                     attempt=stats["attempts"] + 1, message="Page re-oriented; reading it again")
                            continue
                        stats.update(status="done", elements=out.get("elements"), uncertainty=out.get("uncertainty"),
                                     warnings=(out.get("warnings") or [])[:5])
                        run.emit("page", document_id=doc_id, page=n, index=index, total=total, status="done",
                                 elements=out.get("elements"), uncertainty=out.get("uncertainty"),
                                 attempts=stats["attempts"])
                        return stats
                retries += 1
                if retries > self.cfg.max_retries:
                    stats.update(status="rejected", errors=errors[:10])
                    run.emit("page", document_id=doc_id, page=n, index=index, total=total, status="rejected",
                             errors=errors[:10], attempts=stats["attempts"],
                             message=f"Rejected {retries} times; the page stays unread")
                    return stats
                conversation = res.conversation_id
                run.emit("page", document_id=doc_id, page=n, index=index, total=total, status="retrying",
                         attempt=stats["attempts"] + 1, errors=errors[:10],
                         message=f"Validator found {len(errors)} problem(s); asking again")
        except AgyTimeout as e:
            stats.update(status="timeout", error=e.message)
            run.emit("page", document_id=doc_id, page=n, index=index, total=total, status="timeout", message=e.message)
            return stats
        except AgyError as e:
            stats.update(status="error", error=e.message)
            run.emit("page", document_id=doc_id, page=n, index=index, total=total, status="error", message=e.message)
            return stats
        finally:
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

                try:
                    res = runner.run(prompt, run_dir=run_dir, model=model, read_dirs=[], schema=None,
                                     timeout_s=self.cfg.chat_timeout_s, cancel=run.cancel, on_delta=on_delta)
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
        data["seconds_total"] = round(time.time() - run.started, 1)
        self._persist(run, data, state)
        with run.cond:
            run.state = state
        run.emit("done", state=state, data=data)
        self.store.touch(run.conversation_id)
