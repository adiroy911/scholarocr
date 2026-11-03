"""ScholarOCR command-line interface (testing, debugging, and environments without MCP).

Human-readable output by default; ``--json`` prints machine-readable JSON on stdout.
Diagnostics and progress go to stderr.

Exit codes: 0 ok · 1 error · 2 usage · 3 not found · 4 access denied · 5 invalid input ·
6 model unavailable/failed, or pages awaiting transcription (host mode) · 7 completed with problems (partial job / verification errors) ·
130 interrupted (job cancelled, resumable with `scholarocr job resume`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import Settings
from ..exceptions import (AccessDeniedError, GeminiError, InputError, NotFoundError, ScholarOCRError)
from ..schemas import ProcessingOptions
from ..schemas.jobs import TERMINAL, JobStatus

EXIT_OK, EXIT_ERR, EXIT_NOT_FOUND, EXIT_DENIED, EXIT_INPUT, EXIT_GEMINI, EXIT_PROBLEMS = 0, 1, 3, 4, 5, 6, 7


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scholarocr", description="Academic document OCR and intelligence engine.")
    p.add_argument("--version", action="version", version=f"scholarocr {__version__}")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--data-dir", help="override SCHOLAROCR_DATA_DIR")
    p.add_argument("--allowed-dir", action="append", default=None, help="authorized source dir (repeatable)")
    p.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("process", help="ingest and extract a document")
    s.add_argument("paths", nargs="+", help="file(s) or folder forming ONE document, in order")
    s.add_argument("--pages", help="page range, e.g. 1-10,12")
    s.add_argument("--type", dest="document_type", help="document type hint, e.g. exam-paper, handwritten-notes")
    s.add_argument("--lang", dest="language_hint")
    s.add_argument("--context", dest="context_hint", help="free-text context hint")
    s.add_argument("--detail", choices=["standard", "high"], default="standard")
    s.add_argument("--force-visual", action="store_true")
    s.add_argument("--no-crop", action="store_true", help="don't crop images to a detected document region")
    s.add_argument("--no-review", action="store_true", help="disable targeted second-pass review")
    s.add_argument("--reprocess", action="store_true", help="ignore cached page results")

    s = sub.add_parser("status", help="job progress or document summary")
    s.add_argument("id", help="job id (job_...) or document id (doc_...)")

    s = sub.add_parser("content", help="print extracted content")
    s.add_argument("document_id")
    s.add_argument("--pages")
    s.add_argument("--types", help="comma-separated element types")
    s.add_argument("--format", choices=["markdown", "json", "text"], default="markdown")
    s.add_argument("--max-chars", type=int, default=10_000_000)
    s.add_argument("--cursor", type=int)

    s = sub.add_parser("page", help="full IR of one page (JSON)")
    s.add_argument("document_id")
    s.add_argument("page_number", type=int)

    s = sub.add_parser("region", help="save the original source image of an element/page region")
    s.add_argument("document_id")
    s.add_argument("--element")
    s.add_argument("--page", type=int)
    s.add_argument("--bbox", help="x0,y0,x1,y1 in PDF points")
    s.add_argument("--out", required=True, help="output PNG path")

    s = sub.add_parser("verify", help="run quality checks")
    s.add_argument("document_id")
    s.add_argument("--scope", help="comma-separated checks")
    s.add_argument("--review", action="store_true", help="send flagged elements to the review model")

    s = sub.add_parser("export", help="export a processed document")
    s.add_argument("document_id")
    s.add_argument("--format", required=True,
                   help="json | markdown | searchable-pdf | layout-pdf | docx | latex | csv | xlsx")
    s.add_argument("--option", action="append", default=[], help="key=value format option (repeatable)")
    s.add_argument("--out-dir")

    s = sub.add_parser("refine", help="update a document read by an older version: snap boxes to the ink, crop "
                                      "figures, add cleaned text (no model call)")
    s.add_argument("document_id")
    s.add_argument("--pages", help="page range like 1-5,8 (default: all read pages)")
    s.add_argument("--no-clean-text", action="store_true", help="leave cleaned text as it is")

    s = sub.add_parser("list", help="list documents")
    s.add_argument("--query")
    s.add_argument("--status")
    s.add_argument("--type", dest="document_type")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--offset", type=int, default=0)

    s = sub.add_parser("classify", help="classify a document (Gemini)")
    s.add_argument("document_id")
    s.add_argument("--context")
    s.add_argument("--pages")

    s = sub.add_parser("delete", help="delete stored artifacts")
    s.add_argument("document_id")
    s.add_argument("--all", action="store_true", help="also delete the preserved source copy and record")
    s.add_argument("--yes", action="store_true", help="confirm --all")

    s = sub.add_parser("job", help="cancel or resume a job")
    s.add_argument("action", choices=["cancel", "resume"])
    s.add_argument("job_id")

    s = sub.add_parser("usage", help="Gemini usage (and cost if a pricing file is configured)")
    s.add_argument("document_id", nargs="?")

    sub.add_parser("info", help="version and effective configuration (secrets redacted)")

    s = sub.add_parser("purge", help="apply the retention policy (deletes generated artifacts only)")
    s.add_argument("--days", type=float, help="override SCHOLAROCR_RETENTION_DAYS")
    s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("evaluate", help="score extractions against ground-truth files")
    s.add_argument("ground_truth", help="ground-truth JSON file or directory")
    s.add_argument("--report", help="write the full report JSON here")
    return p


def _settings(args) -> Settings:
    s = Settings.from_env()
    if args.data_dir:
        s.data_dir = Path(args.data_dir).expanduser()
    if args.allowed_dir:
        s.allowed_dirs = [Path(d).expanduser() for d in args.allowed_dir]
    if args.log_level:
        s.log_level = args.log_level
    elif not os.environ.get("SCHOLAROCR_LOG_LEVEL"):
        s.log_level = "WARNING"
    return s


def _out(args, data: Any, human: str | None = None) -> None:
    if args.json or human is None:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(human)


def _doc_type(value: str | None) -> str | None:
    return value.replace("-", "_") if value else None


def _run_job_with_progress(svc, job_id: str, quiet: bool) -> dict:
    last = None
    try:
        while True:
            job = svc.job_status(job_id)
            line = f"[{job['status']}] {job['progress']} pages  {job['stage']}"
            if not quiet and line != last:
                print(line, file=sys.stderr)
                last = line
            if JobStatus(job["status"]) in TERMINAL:
                return job
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\ncancelling… (resume later with: scholarocr job resume " + job_id + ")", file=sys.stderr)
        svc.cancel_job(job_id)
        svc.wait_for_job(job_id, timeout=120)
        raise


def cmd_process(svc, args) -> int:
    opts = ProcessingOptions(document_type=_doc_type(args.document_type), language_hint=args.language_hint,
                             context_hint=args.context_hint, pages=args.pages, detail=args.detail,
                             force_visual=args.force_visual, auto_crop=not args.no_crop, review=not args.no_review, reprocess=args.reprocess)
    started = svc.process(args.paths, opts, wait=False)
    job = _run_job_with_progress(svc, started["job_id"], quiet=args.json)
    summary = svc.document_summary(started["document_id"])
    data = {**started, "status": job["status"], "job": job, "summary": summary}
    lines = [f"document: {started['document_id']}", f"job:      {job['job_id']} ({job['status']})",
             f"pages:    {job['completed_pages']}/{job['total_pages']} done, {job['failed_pages']} failed, "
             f"{job['cached_pages']} reused from cache",
             f"routes:   {summary['routes']}", f"uncertainty: {summary['uncertainty']}"]
    for w in started["warnings"]:
        lines.append(f"warning:  {w}")
    for f in summary["failed_pages"]:
        lines.append(f"failed:   page {f['page']}: {f['error']}")
    awaiting = summary.get("awaiting_transcription") or []
    if awaiting:
        lines.append(f"awaiting: {len(awaiting)} page(s) need visual OCR: {awaiting[:20]}. No model key is configured "
                     "(host mode). Set GEMINI_API_KEY or OPENROUTER_API_KEY and run process again, or use "
                     "ScholarOCR from an MCP client (Antigravity CLI, Claude, ChatGPT) whose model transcribes them.")
    _out(args, data, "\n".join(lines))
    if job["status"] == "completed":
        return EXIT_OK
    if job["status"] == "awaiting_transcription":
        print(f"error: {len(awaiting)} page(s) await transcription (host mode, no GEMINI_API_KEY / "
              "OPENROUTER_API_KEY)", file=sys.stderr)
        return EXIT_GEMINI
    if job["status"] == "failed" and any("GEMINI_API_KEY" in (f["error"] or "") for f in summary["failed_pages"]):
        return EXIT_GEMINI
    return EXIT_PROBLEMS if job["status"] in ("partial", "failed") else EXIT_ERR


def dispatch(svc, args) -> int:
    c = args.cmd
    if c == "process":
        return cmd_process(svc, args)
    if c == "status":
        data = svc.job_status(args.id) if args.id.startswith("job_") else svc.document_summary(args.id)
        _out(args, data)
        return EXIT_OK
    if c == "content":
        types = [t.strip() for t in args.types.split(",")] if args.types else None
        data = svc.get_content(args.document_id, pages=args.pages, element_types=types, fmt=args.format,
                               max_chars=args.max_chars, cursor=args.cursor)
        if args.json or args.format == "json":
            _out(args, data)
        else:
            print(data["content"])
            if data["next_cursor"]:
                print(f"\n[more content: --cursor {data['next_cursor']}]", file=sys.stderr)
        return EXIT_OK
    if c == "page":
        _out(args, svc.get_page(args.document_id, args.page_number))
        return EXIT_OK
    if c == "region":
        bbox = [float(v) for v in args.bbox.split(",")] if args.bbox else None
        png, meta = svc.get_source_region(args.document_id, element_id=args.element, page_number=args.page, bbox=bbox)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(png)
        _out(args, {**meta, "saved_to": str(out)}, f"saved {out} ({meta['image_size_px'][0]}x{meta['image_size_px'][1]})")
        return EXIT_OK
    if c == "verify":
        scope = [s.strip() for s in args.scope.split(",")] if args.scope else None
        rep = svc.verify(args.document_id, scope=scope, review=args.review)
        lines = [f"verification {'PASSED' if rep.passed else 'FAILED'}  {rep.counts()}"]
        for f in rep.findings:
            if f.severity.value != "info":
                loc = f"p{f.page_number}" if f.page_number else "doc"
                lines.append(f"  {f.severity.value:7s} {f.check:13s} {loc:5s} {f.message}"
                             + (f"  [{f.element_id}]" if f.element_id else ""))
        _out(args, {**rep.model_dump(mode="json"), "counts": rep.counts()}, "\n".join(lines))
        return EXIT_OK if rep.passed else EXIT_PROBLEMS
    if c == "export":
        options: dict[str, Any] = {}
        for kv in args.option:
            if "=" not in kv:
                raise InputError(f"--option must be key=value, got {kv!r}")
            k, v = kv.split("=", 1)
            options[k.strip()] = v.strip()
        res = svc.export(args.document_id, args.format, options=options, output_dir=args.out_dir)
        lines = [f"wrote {res['primary_file']}"] + [f"warning: {w}" for w in res["warnings"]]
        _out(args, res, "\n".join(lines))
        return EXIT_OK
    if c == "refine":
        res = svc.refine_document(args.document_id, pages=args.pages, clean_text=not args.no_clean_text)
        t = res["totals"]
        lines = [f"pages updated: {len(res['pages'])}" + (f" (skipped, not read: {res['skipped_pages']})"
                                                          if res["skipped_pages"] else ""),
                 f"boxes refined: {t['refined']} (model box kept: {t['kept_model_box']})",
                 f"figures cropped: {t['figures']} (cleaned: {t['figures_cleaned']})",
                 f"cleaned text added: {t['clean_text_added']}"]
        _out(args, res, "\n".join(lines))
        return EXIT_OK if res["pages"] else EXIT_PROBLEMS
    if c == "list":
        data = svc.list_documents(query=args.query, status=args.status, document_type=_doc_type(args.document_type),
                                  limit=args.limit, offset=args.offset)
        rows = [f"{d['document_id']}  {d['status']:20s} {d['page_count']:4d}p  {', '.join(d['filenames'])[:60]}"
                for d in data["documents"]]
        _out(args, data, "\n".join(rows) or "no documents")
        return EXIT_OK
    if c == "classify":
        _out(args, svc.classify(args.document_id, context_hint=args.context, pages=args.pages))
        return EXIT_OK
    if c == "delete":
        mode = "all" if args.all else "generated"
        _out(args, svc.delete(args.document_id, mode=mode, confirm=args.yes))
        return EXIT_OK
    if c == "job":
        if args.action == "cancel":
            _out(args, svc.cancel_job(args.job_id))
            return EXIT_OK
        svc.resume_job(args.job_id, wait=False)
        job = _run_job_with_progress(svc, args.job_id, quiet=args.json)
        _out(args, job)
        return EXIT_OK if job["status"] == "completed" else EXIT_PROBLEMS
    if c == "usage":
        _out(args, svc.usage(args.document_id))
        return EXIT_OK
    if c == "info":
        _out(args, svc.info())
        return EXIT_OK
    if c == "purge":
        _out(args, svc.apply_retention(days=args.days, dry_run=args.dry_run))
        return EXIT_OK
    if c == "evaluate":
        from ..evaluation.metrics import evaluate_path

        report = evaluate_path(svc, Path(args.ground_truth))
        if args.report:
            Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        _out(args, report)
        return EXIT_OK
    raise InputError(f"unknown command {c}")  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        from ..service import ScholarOCR

        svc = ScholarOCR(_settings(args))
        return dispatch(svc, args)
    except KeyboardInterrupt:
        return 130
    except ScholarOCRError as e:
        if args.json:
            print(json.dumps(e.to_dict(), indent=2), file=sys.stdout)
        print(f"error: {e.message}", file=sys.stderr)
        if isinstance(e, NotFoundError):
            return EXIT_NOT_FOUND
        if isinstance(e, AccessDeniedError):
            return EXIT_DENIED
        if isinstance(e, InputError):
            return EXIT_INPUT
        if isinstance(e, GeminiError):
            return EXIT_GEMINI
        return EXIT_ERR
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
