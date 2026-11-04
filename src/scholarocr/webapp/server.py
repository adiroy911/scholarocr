"""``scholarocr-app``: the local web app (Starlette, JSON + SSE API, static frontend).

Security model (single user, this machine only):

* binds to 127.0.0.1, and every request's ``Host`` must be 127.0.0.1/localhost on the app's port (blocks DNS
  rebinding);
* every ``/api`` call needs the per-process token that is embedded in the page (header
  ``X-ScholarOCR-Token``, or ``?t=`` for GET-only links such as downloads and the event stream);
* state-changing requests with an ``Origin`` header must come from the app's own origin;
* uploads are written only inside the app's data directory, with type sniffing and size/count limits;
* responses carry a strict Content-Security-Policy (scripts from the app only); the page builds all model
  output with DOM text nodes and KaTeX (trust off), never as HTML.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import secrets
import threading
import webbrowser
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from ..exceptions import InputError, LimitExceededError, NotFoundError, ScholarOCRError
from ..intelligence.gemini_client import Transport
from ..logging_config import get_logger
from ..remote.sandbox import safe_filename
from .agy import AgyRunner
from .config import MAX_MESSAGE_CHARS, AppConfig, PrefsStore
from .engines import EngineRegistry, NoEngineError, ServicePool
from . import redraw as redraw_mod
from . import review as review_mod
from .keys import KeyStore
from .runs import RunManager
from .store import AppStore
from .uploads import Uploads
from .view import document_view

log = get_logger("webapp.server")
TOKEN_HEADER = "x-scholarocr-token"
EXPORT_FORMATS = ("markdown", "docx", "latex", "searchable-pdf", "layout-pdf", "json", "csv", "xlsx")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; "
       "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")
SECURITY_HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer", "Cross-Origin-Opener-Policy": "same-origin",
                    "Cross-Origin-Resource-Policy": "same-origin"}


def static_dir() -> Path:
    return Path(str(resources.files("scholarocr.webapp") / "static"))


class App:
    """Everything the routes need, built once per process (or per test)."""

    def __init__(self, cfg: AppConfig, *, keys: KeyStore | None = None, registry: EngineRegistry | None = None,
                 transport_factory: Callable[[str, str], Transport | None] | None = None,
                 agy_factory: Callable[[], AgyRunner | None] | None = None, token: str | None = None,
                 threaded_runs: bool = True):
        cfg.ensure_dirs()
        self.cfg = cfg
        self.token = token or secrets.token_urlsafe(32)
        self.prefs = PrefsStore(cfg.prefs_path)
        self.keys = keys or KeyStore()
        self.registry = registry or EngineRegistry(cfg, self.keys)
        self.store = AppStore(cfg.db_path)
        self.uploads = Uploads(cfg.uploads_dir)
        self.uploads.sweep(cfg.upload_ttl_s)
        self.pool = ServicePool(cfg, self.keys, transport_factory=transport_factory)

        def default_agy() -> AgyRunner | None:
            path = self.registry.agy_path()
            return AgyRunner(path, cfg.runs_dir, use_reader_agent=cfg.use_reader_agent) if path else None

        self.runs = RunManager(cfg, self.store, self.pool, agy_factory or default_agy, threaded=threaded_runs)
        self.allowed_hosts = {f"127.0.0.1:{cfg.port}", f"localhost:{cfg.port}", f"[::1]:{cfg.port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}


# ---------------------------------------------------------------------- helpers
def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


def _error(e: Exception) -> JSONResponse:
    if isinstance(e, NotFoundError):
        return _json(e.to_dict(), 404)
    if isinstance(e, LimitExceededError):
        return _json(e.to_dict(), 413)
    if isinstance(e, NoEngineError):
        return _json(e.to_dict(), 409)
    if isinstance(e, ScholarOCRError):
        return _json(e.to_dict(), 400)
    log.exception("unhandled error")
    return _json({"error": "internal_error", "message": "Something failed inside the app; see the terminal log."},
                 500)


async def _body_json(request: Request, limit: int = 256_000) -> dict[str, Any]:
    body = await request.body()
    if len(body) > limit:
        raise LimitExceededError("request too large")
    if not body:
        return {}
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith("application/json"):
        raise InputError("expected application/json")
    try:
        data = json.loads(body)
    except ValueError:
        raise InputError("body is not valid JSON") from None
    if not isinstance(data, dict):
        raise InputError("body must be a JSON object")
    return data


def _owned(app: App, document_id: str) -> None:
    if not app.store.owns_document(document_id):
        raise NotFoundError(f"No document {document_id}")


def _conversation_view(app: App, cid: str) -> dict[str, Any]:
    conv = app.store.get_conversation(cid)
    run = app.runs.active_for(cid)
    return {"conversation": conv, "messages": app.store.messages(cid), "documents": app.store.documents(cid),
            "active_run": {"id": run.id, "message_id": run.message_id, "kind": run.kind} if run else None}


def _state(app: App, refresh: bool = False) -> dict[str, Any]:
    prefs = app.prefs.load()
    engines = [s.to_dict() for s in app.registry.statuses(prefs, refresh=refresh)]
    return {"version": __version__, "prefs": prefs.model_dump(), "engines": engines, "keys": app.keys.status(),
            "key_backend": app.keys.backend, "setup_needed": not any(e["available"] for e in engines),
            "limits": {"max_file_mb": prefs.max_file_mb, "max_files": prefs.max_files,
                       "max_message_chars": MAX_MESSAGE_CHARS}}


def _title_from(names: list[str], text: str) -> str:
    if names:
        base = names[0].rsplit(".", 1)[0]
        return base if len(names) == 1 else f"{base} (+{len(names) - 1})"
    return " ".join(text.split())[:60] or "New document"


# ---------------------------------------------------------------------- app factory
def create_app(cfg: AppConfig | None = None, **kwargs: Any) -> Starlette:
    cfg = cfg or AppConfig()
    app = App(cfg, **kwargs)

    def asset_version() -> str:
        """Changes whenever app.js or app.css change, so browsers never run a stale script."""
        stamp = "".join(str((static_dir() / n).stat().st_mtime_ns) for n in ("app.js", "app.css"))
        return hashlib.sha256((__version__ + stamp).encode()).hexdigest()[:12]

    async def index(request: Request) -> Response:
        index_template = (static_dir() / "index.html").read_text(encoding="utf-8")
        html = (index_template.replace("__SCHOLAROCR_TOKEN__", app.token)
                .replace("__SCHOLAROCR_VERSION__", __version__).replace("__ASSET_V__", asset_version()))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ state & settings
    async def get_state(request: Request) -> Response:
        refresh = request.query_params.get("refresh") == "1"
        return _json(await run_in_threadpool(_state, app, refresh))

    async def put_prefs(request: Request) -> Response:
        data = await _body_json(request)
        prefs = app.prefs.update(data)
        return _json({"prefs": prefs.model_dump()})

    async def put_key(request: Request) -> Response:
        provider = request.path_params["provider"]
        data = await _body_json(request, limit=4096)
        backend = app.keys.set(provider, str(data.get("key") or ""))
        app.pool.drop_engine(provider)
        return _json({"provider": provider, "set": True, "backend": backend})

    async def delete_key(request: Request) -> Response:
        provider = request.path_params["provider"]
        app.keys.delete(provider)
        app.pool.drop_engine(provider)
        return _json({"provider": provider, "set": False})

    # ------------------------------------------------------------------ conversations
    async def list_conversations(request: Request) -> Response:
        return _json({"conversations": app.store.list_conversations()})

    async def create_conversation(request: Request) -> Response:
        return _json(app.store.create_conversation(), 201)

    async def get_conversation(request: Request) -> Response:
        return _json(_conversation_view(app, request.path_params["cid"]))

    async def patch_conversation(request: Request) -> Response:
        data = await _body_json(request)
        return _json(app.store.rename(request.path_params["cid"], str(data.get("title") or "")))

    async def delete_conversation(request: Request) -> Response:
        cid = request.path_params["cid"]
        run = app.runs.active_for(cid)
        if run:
            app.runs.cancel(run.id)
        app.store.delete_conversation(cid)
        return _json({"deleted": cid})

    # ------------------------------------------------------------------ uploads
    async def upload(request: Request) -> Response:
        prefs = app.prefs.load()
        limit = int(prefs.max_file_mb * 1048576)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise LimitExceededError(f"the file is larger than the {prefs.max_file_mb:g} MB limit (change it in Settings)")
        buf = bytearray()
        async for chunk in request.stream():
            buf.extend(chunk)
            if len(buf) > limit:
                raise LimitExceededError(f"the file is larger than the {prefs.max_file_mb:g} MB limit (change it in Settings)")
        name = request.query_params.get("name") or "upload"
        meta = await run_in_threadpool(app.uploads.save, bytes(buf), name, max_mb=prefs.max_file_mb)
        return _json(meta, 201)

    async def delete_upload(request: Request) -> Response:
        app.uploads.delete(request.path_params["uid"])
        return _json({"deleted": True})

    # ------------------------------------------------------------------ messages & runs
    async def post_message(request: Request) -> Response:
        cid = request.path_params["cid"]
        app.store.get_conversation(cid)
        data = await _body_json(request)
        text = str(data.get("text") or "").replace("\x00", "").strip()
        if len(text) > MAX_MESSAGE_CHARS:
            raise InputError(f"messages are limited to {MAX_MESSAGE_CHARS} characters")
        upload_ids = data.get("uploads") or []
        if not isinstance(upload_ids, list) or not all(isinstance(u, str) for u in upload_ids):
            raise InputError("uploads must be a list of upload ids")
        prefs = app.prefs.load()
        if len(upload_ids) > prefs.max_files:
            raise LimitExceededError(f"at most {prefs.max_files} files per message (change it in Settings)")
        if app.runs.active_for(cid):
            raise InputError("this conversation is still working; wait for it or stop it first")
        if not upload_ids and not text:
            raise InputError("type a message or attach a file")
        if not upload_ids and not app.store.documents(cid):
            raise InputError("attach a document first; questions are answered from its extracted content")
        engine = await run_in_threadpool(app.registry.resolve, data.get("engine"), prefs)
        model = str(data.get("model") or "") or engine.default_model
        if model and model not in {m["id"] for m in engine.models}:
            raise InputError(f"model {model!r} is not offered by {engine.label}")
        files = [app.uploads.get(u) for u in upload_ids]
        conv = app.store.get_conversation(cid)
        if conv["title"] == "New document" and not app.store.messages(cid):
            app.store.rename(cid, _title_from([m["name"] for _, m in files], text))
        attachments = [{"name": m["name"], "type": m["type"], "bytes": m["bytes"], "kind": m["kind"]} for _, m in files]
        user_msg = app.store.add_message(cid, "user", "upload" if files else "text", text,
                                         {"attachments": attachments})
        if files:
            # Page photos together form one document (in the order attached); each PDF or DOCX is its own.
            images = [(p, m) for p, m in files if m["kind"] == "image"]
            others = [(p, m) for p, m in files if m["kind"] != "image"]
            groups = ([[p for p, _ in images]] if images else []) + [[p] for p, _ in others]
            names = ([[m["name"] for _, m in images]] if images else []) + [[m["name"]] for _, m in others]
            instructions = "\n".join(s for s in (prefs.saved_instructions, text) if s)
            asst = app.store.add_message(cid, "assistant", "read", "", {"engine": engine.id, "model": model},
                                         state="running")
            run = app.runs.start_read(cid, groups, names, engine=engine, model=model, prefs=prefs,
                                      instructions=instructions, message_id=asst["id"])
            threading.Thread(target=_discard_uploads, args=(app, run, upload_ids), daemon=True).start()
        else:
            asst = app.store.add_message(cid, "assistant", "chat", "", {"engine": engine.id, "model": model},
                                         state="running")
            run = app.runs.start_chat(cid, text, engine=engine, model=model, prefs=prefs, message_id=asst["id"])
        return _json({"run_id": run.id, "user_message": user_msg, "assistant_message": asst}, 202)

    async def run_events(request: Request) -> Response:
        run = app.runs.get(request.path_params["rid"])
        if run is None:
            raise NotFoundError("run not found (it may have finished before the app restarted)")
        try:
            start = int(request.headers.get("last-event-id") or request.query_params.get("from") or 0)
        except ValueError:
            start = 0

        async def stream():
            seq = start
            idle = 0.0
            while True:
                if await request.is_disconnected():
                    return
                events = run.since(seq)
                for ev in events:
                    seq = ev["seq"]
                    yield f"id: {seq}\nevent: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    if ev["type"] == "done":
                        return
                if events:
                    idle = 0.0
                    continue
                await run_in_threadpool(run.wait, seq, 0.5)
                idle += 0.5
                if idle >= 15:
                    idle = 0.0
                    yield ": keep-alive\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def cancel_run(request: Request) -> Response:
        ok = app.runs.cancel(request.path_params["rid"])
        return _json({"cancelling": ok})

    # ------------------------------------------------------------------ documents
    async def get_document(request: Request) -> Response:
        doc_id = request.path_params["did"]
        _owned(app, doc_id)
        prefs = app.prefs.load()
        svc = app.pool.reader(prefs)
        return _json(await run_in_threadpool(document_view, svc, doc_id, prefs.text_variant))

    async def figure_image(request: Request) -> Response:
        doc_id, eid, variant = request.path_params["did"], request.path_params["eid"], request.path_params["variant"]
        _owned(app, doc_id)
        if variant not in ("clean", "original", "svg"):
            raise InputError("figure image variant must be clean, original or svg")
        svc = app.pool.reader(app.prefs.load())
        data, media = await run_in_threadpool(svc.figure_crop, doc_id, eid, variant)
        headers = {"Cache-Control": "no-store"}
        if media == "image/svg+xml":  # sanitised on submission; opened directly it still cannot run anything
            headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        return Response(data, media_type=media, headers=headers)

    async def redraw_figure(request: Request) -> Response:
        doc_id, eid = request.path_params["did"], request.path_params["eid"]
        _owned(app, doc_id)
        data = await _body_json(request)
        prefs = app.prefs.load()
        engine = await run_in_threadpool(app.registry.resolve, data.get("engine"), prefs)
        model = str(data.get("model") or "") or engine.default_model
        if model and model not in {m["id"] for m in engine.models}:
            raise InputError(f"model {model!r} is not offered by {engine.label}")
        svc = app.pool.reader(prefs)
        key_service = None if engine.id == "antigravity" else (lambda: app.pool.get(engine.id, prefs, model))
        out = await run_in_threadpool(lambda: redraw_mod.redraw(
            svc, doc_id, eid, engine=engine, model=model, key_service=key_service, agy_factory=app.runs.agy_factory,
            runs_dir=cfg.runs_dir, timeout_s=cfg.redraw_timeout_s, effort=prefs.chat_effort))
        fid = out.get("fidelity") or {}
        return _json({"element_id": eid, "svg": bool(out.get("svg")), "faithful": bool(out.get("faithful")),
                      "fidelity": {k: fid.get(k) for k in ("score", "recall", "precision", "passed", "labels_missing",
                                                           "missing_regions", "extra_regions")},
                      "feedback": out.get("feedback", []), "attempts": out.get("attempts", 1),
                      "labels": out.get("labels", []), "removed": out.get("removed", []),
                      "usage": out.get("usage", {}), "engine": engine.id, "model": model,
                      "history": out.get("history", []), "retry_error": out.get("retry_error")})

    async def refine_document(request: Request) -> Response:
        """Update layout: box refinement, figure crops and rule-cleaned text for a document read before they
        existed (no model call, nothing is read again)."""
        doc_id = request.path_params["did"]
        _owned(app, doc_id)
        if any(doc_id in app.store.documents(r.conversation_id) for r in app.runs.running()):
            raise InputError("this document is still being read; update its layout when reading has finished")
        prefs = app.prefs.load()
        svc = app.pool.reader(prefs)
        result = await run_in_threadpool(svc.refine_document, doc_id)
        view = await run_in_threadpool(document_view, svc, doc_id, prefs.text_variant)
        return _json({"result": {"totals": result["totals"], "pages": len(result["pages"])}, "view": view})

    async def resume_document(request: Request) -> Response:
        """Read the pages of a document that are not accepted yet, with the engine and model of the read that left
        them, in that read's message. A document that is being read keeps its run: answers 200 with that run."""
        doc_id = request.path_params["did"]
        _owned(app, doc_id)
        data = await _body_json(request)
        message = app.store.get_message(str(data.get("message_id") or ""))
        stored = message["data"]
        if message["kind"] != "read" or not any(d.get("document_id") == doc_id for d in stored.get("documents") or []):
            raise NotFoundError(f"Document {doc_id} was not read in that message")

        def answer(run, started: bool) -> Response:
            return _json({"run_id": run.id, "message_id": run.message_id, "conversation_id": run.conversation_id,
                          "already_running": not started}, 202 if started else 200)

        current = app.runs.reading_run(doc_id)
        if current is not None:
            return answer(current, False)
        prefs = app.prefs.load()
        engine = await run_in_threadpool(app.registry.resolve, stored.get("engine"), prefs)
        model = stored.get("model") or engine.default_model
        if model and model not in {m["id"] for m in engine.models}:
            raise InputError(f"model {model!r} is not offered by {engine.label} any more")
        if stored.get("effort"):  # the same reader as the first read; the number of pages at once stays the setting
            prefs = prefs.model_copy(update={"read_effort": stored["effort"]})
        plan = await run_in_threadpool(lambda: app.pool.get(engine.id, prefs, model).resume_plan(doc_id))
        if not plan["unread"]:
            raise InputError("every page of this document has been read")
        if plan["source_missing"]:
            raise InputError(f"The stored copy of {', '.join(plan['source_missing'])} is gone, so its unread pages "
                             "cannot be prepared. Attach the file again.",
                             details={"source_missing": plan["source_missing"]})
        run, started = await run_in_threadpool(app.runs.start_resume, message["conversation_id"], doc_id, message,
                                               engine=engine, model=model, prefs=prefs)
        return answer(run, started)

    async def undo_review(request: Request) -> Response:
        """Put back the reading from before the review pass changed it."""
        doc_id, eid = request.path_params["did"], request.path_params["eid"]
        _owned(app, doc_id)
        if any(doc_id in app.store.documents(r.conversation_id) for r in app.runs.running()):
            raise InputError("this document is still being read; undo the change when reading has finished")
        prefs = app.prefs.load()
        svc = app.pool.reader(prefs)
        await run_in_threadpool(review_mod.undo_verdict, svc, doc_id, eid)
        view = await run_in_threadpool(document_view, svc, doc_id, prefs.text_variant)
        return _json({"element_id": eid, "view": view})

    async def element_source(request: Request) -> Response:
        doc_id, eid = request.path_params["did"], request.path_params["eid"]
        _owned(app, doc_id)
        svc = app.pool.reader(app.prefs.load())
        png, meta = await run_in_threadpool(lambda: svc.get_source_region(doc_id, element_id=eid, max_side=1400))
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})

    async def page_source(request: Request) -> Response:
        doc_id = request.path_params["did"]
        _owned(app, doc_id)
        try:
            n = int(request.path_params["n"])
        except ValueError:
            raise InputError("page must be a number") from None
        svc = app.pool.reader(app.prefs.load())
        png, _ = await run_in_threadpool(lambda: svc.get_source_region(doc_id, page_number=n, max_side=1600))
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})

    async def export(request: Request) -> Response:
        doc_id, fmt = request.path_params["did"], request.path_params["fmt"]
        _owned(app, doc_id)
        if fmt not in EXPORT_FORMATS:
            raise InputError(f"unknown format {fmt!r}")
        prefs = app.prefs.load()
        svc = app.pool.reader(prefs)
        # Cleaned / As written and figure images: from the link (the result view's switch), else from Settings.
        text = request.query_params.get("text") or prefs.text_variant
        figures = request.query_params.get("figures") or prefs.figure_variant
        if text not in ("auto", "clean", "faithful") or figures not in ("clean", "original", "redrawn"):
            raise InputError("text must be auto, clean or faithful; figures must be clean, original or redrawn")
        result = await run_in_threadpool(lambda: svc.export(doc_id, fmt, options={"text_variant": text,
                                                                                 "figure_variant": figures}))
        path = Path(result["primary_file"]).resolve()
        if not path.is_relative_to(cfg.engine_data_dir.resolve()) or not path.is_file():
            raise NotFoundError("export file not found")
        # Name the download after the source file: "class11_test_scan.searchable.pdf" instead of "document...".
        names = svc.load_manifest(doc_id).sources
        stem = safe_filename(names[0].filename.rsplit(".", 1)[0] if names else "document", fallback="document")
        suffix = path.name[len("document"):] if path.name.startswith("document") else "_" + path.name
        return FileResponse(path, filename=stem + suffix, headers={"Cache-Control": "no-store"})

    def guarded(fn):
        async def handler(request: Request) -> Response:
            try:
                return await fn(request)
            except Exception as e:  # noqa: BLE001 - every error becomes a JSON answer
                return _error(e)

        return handler

    api = [
        Route("/api/state", guarded(get_state), methods=["GET"]),
        Route("/api/prefs", guarded(put_prefs), methods=["PUT"]),
        Route("/api/keys/{provider}", guarded(put_key), methods=["PUT"]),
        Route("/api/keys/{provider}", guarded(delete_key), methods=["DELETE"]),
        Route("/api/conversations", guarded(list_conversations), methods=["GET"]),
        Route("/api/conversations", guarded(create_conversation), methods=["POST"]),
        Route("/api/conversations/{cid}", guarded(get_conversation), methods=["GET"]),
        Route("/api/conversations/{cid}", guarded(patch_conversation), methods=["PATCH"]),
        Route("/api/conversations/{cid}", guarded(delete_conversation), methods=["DELETE"]),
        Route("/api/conversations/{cid}/messages", guarded(post_message), methods=["POST"]),
        Route("/api/uploads", guarded(upload), methods=["POST"]),
        Route("/api/uploads/{uid}", guarded(delete_upload), methods=["DELETE"]),
        Route("/api/runs/{rid}/events", guarded(run_events), methods=["GET"]),
        Route("/api/runs/{rid}/cancel", guarded(cancel_run), methods=["POST"]),
        Route("/api/documents/{did}", guarded(get_document), methods=["GET"]),
        Route("/api/documents/{did}/elements/{eid}/source.png", guarded(element_source), methods=["GET"]),
        Route("/api/documents/{did}/pages/{n}/source.png", guarded(page_source), methods=["GET"]),
        Route("/api/documents/{did}/elements/{eid}/figure/{variant}", guarded(figure_image), methods=["GET"]),
        Route("/api/documents/{did}/elements/{eid}/redraw", guarded(redraw_figure), methods=["POST"]),
        Route("/api/documents/{did}/elements/{eid}/undo-review", guarded(undo_review), methods=["POST"]),
        Route("/api/documents/{did}/export/{fmt}", guarded(export), methods=["GET"]),
        Route("/api/documents/{did}/refine", guarded(refine_document), methods=["POST"]),
        Route("/api/documents/{did}/resume", guarded(resume_document), methods=["POST"]),
    ]
    routes = [Route("/", index, methods=["GET"]), *api,
              Mount("/static", app=StaticFiles(directory=str(static_dir())), name="static")]
    star = Starlette(routes=routes)
    star.state.app = app
    return LocalOnly(star, app)


def _discard_uploads(app: App, run, upload_ids: list[str]) -> None:
    """The service keeps its own preserved copy at ingestion; staged uploads go once the run has finished."""
    while run.state == "running":
        run.wait(len(run.events), 5.0)
    for u in upload_ids:
        try:
            app.uploads.delete(u)
        except ScholarOCRError:
            pass


class LocalOnly:
    """ASGI wrapper: Host allow-list, token and Origin checks, security headers."""

    def __init__(self, inner: Starlette, app: App):
        self.inner = inner
        self.app = app
        self.state = inner.state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        problem = self.check(headers, path, method, scope.get("query_string", b"").decode("latin-1"))
        if problem is not None:
            status, message = problem
            resp = JSONResponse({"error": "forbidden", "message": message}, status_code=status,
                                headers=SECURITY_HEADERS)
            return await resp(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                hs = list(message.get("headers", []))
                present = {k.decode("latin-1").lower() for k, _ in hs}
                for k, v in SECURITY_HEADERS.items():
                    if k.lower() not in present:
                        hs.append((k.encode("latin-1"), v.encode("latin-1")))
                message = {**message, "headers": hs}
            await send(message)

        return await self.inner(scope, receive, send_with_headers)

    def check(self, headers: dict[str, str], path: str, method: str, query: str) -> tuple[int, str] | None:
        host = headers.get("host", "")
        if host not in self.app.allowed_hosts:
            return 421, "this app only answers on 127.0.0.1"
        if not path.startswith("/api/"):
            return None
        origin = headers.get("origin")
        if method not in ("GET", "HEAD") and origin is not None and origin not in self.app.allowed_origins:
            return 403, "cross-origin request refused"
        if headers.get("sec-fetch-site") == "cross-site":
            return 403, "cross-site request refused"
        token = headers.get(TOKEN_HEADER) or None
        if token is None and method in ("GET", "HEAD"):
            from urllib.parse import parse_qs

            token = (parse_qs(query).get("t") or [None])[0]
        if not token or not hmac.compare_digest(token, self.app.token):
            return 403, "missing or wrong app token; reload the page"
        return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="scholarocr-app", description="ScholarOCR web app on this computer "
                                     "(127.0.0.1 only).")
    parser.add_argument("--port", type=int, default=None, help="port (default 8765)")
    parser.add_argument("--open", action="store_true", help="open the app in the default browser")
    parser.add_argument("--data-dir", help="where conversations and documents are stored "
                                           "(default: SCHOLAROCR_APP_DATA_DIR or <ScholarOCR data dir>/app)")
    args = parser.parse_args(argv)
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        raise SystemExit('uvicorn is missing: pip install "scholarocr[webapp]"') from None
    cfg = AppConfig()
    if args.port:
        cfg = cfg.model_copy(update={"port": args.port})
    if args.data_dir:
        cfg = cfg.model_copy(update={"data_dir": Path(args.data_dir).expanduser()})
    app = create_app(cfg)
    url = f"http://127.0.0.1:{cfg.port}/"
    print(f"ScholarOCR app: {url}  (data: {cfg.data_dir})")
    # Warm the engine list (runs `agy models` once) so the first page load is quick.
    threading.Thread(target=lambda: app.app.registry.statuses(app.app.prefs.load()), daemon=True).start()
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    main()
