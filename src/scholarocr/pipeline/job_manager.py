"""Job lifecycle: start (sync or background thread), progress, cancel, resume, crash recovery.

Job state lives in SQLite, so any process (CLI, another MCP server instance) can read status.
Resuming re-runs the pipeline for the job's page selection; finished pages are skipped through
their checkpoints, so a failure on page 37 never re-processes pages 1-36.
"""

from __future__ import annotations

import os
import threading
import uuid
from typing import Callable

from ..exceptions import JobError, NotFoundError
from ..logging_config import get_logger
from ..schemas import Job, JobStatus, ProcessingOptions
from ..schemas.jobs import TERMINAL
from ..storage import Repository
from .orchestrator import RunSummary

log = get_logger("jobs")

Runner = Callable[[str, ProcessingOptions, str, Callable[[], bool], Callable[[RunSummary, str], None]], RunSummary]


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":  # pragma: no cover - Windows
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


class JobManager:
    def __init__(self, repo: Repository, runner: Runner):
        self.repo = repo
        self.runner = runner
        self._threads: dict[str, threading.Thread] = {}
        self._local_running: set[str] = set()
        self._guard = threading.Lock()

    def recover_stale(self) -> list[str]:
        """Mark queued/running jobs whose process is gone as INTERRUPTED (resumable)."""
        recovered = []
        for job in self.repo.running_jobs():
            with self._guard:
                local = job.job_id in self._local_running
            if local:
                continue
            if job.pid != os.getpid() and _pid_alive(job.pid):
                continue
            self.repo.update_job(job.job_id, status=JobStatus.INTERRUPTED, stage="interrupted (process ended)")
            recovered.append(job.job_id)
        return recovered

    def start(self, document_id: str, options: ProcessingOptions, total_pages: int, *, background: bool) -> Job:
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        self.repo.create_job(job_id, document_id, total_pages, options.model_dump(mode="json"))
        return self._launch(job_id, document_id, options, background)

    def resume(self, job_id: str, *, background: bool) -> Job:
        job = self.get(job_id)
        if job.status in (JobStatus.RUNNING, JobStatus.QUEUED):
            self.recover_stale()
            job = self.get(job_id)
            if job.status in (JobStatus.RUNNING, JobStatus.QUEUED):
                raise JobError(f"Job {job_id} is still {job.status.value}")
        if job.status == JobStatus.COMPLETED:
            return job
        options = ProcessingOptions.model_validate(job.options)
        options.reprocess = False  # resuming must reuse finished pages
        self.repo.update_job(job_id, status=JobStatus.QUEUED, stage="resuming", cancel_requested=0, errors=[],
                             failed_pages=0, completed_pages=0, cached_pages=0, pid=os.getpid())
        return self._launch(job_id, job.document_id, options, background)

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status in TERMINAL:
            return job
        self.repo.update_job(job_id, cancel_requested=1, stage="cancellation requested")
        return self.get(job_id)

    def get(self, job_id: str) -> Job:
        job = self.repo.get_job(job_id)
        if job is None:
            raise NotFoundError(f"No job with id {job_id}")
        return job

    def wait(self, job_id: str, timeout: float | None = None) -> Job:
        t = self._threads.get(job_id)
        if t is not None:
            t.join(timeout)
        return self.get(job_id)

    def _launch(self, job_id: str, document_id: str, options: ProcessingOptions, background: bool) -> Job:
        if background:
            t = threading.Thread(target=self._execute, args=(job_id, document_id, options), name=f"job-{job_id}",
                                 daemon=True)
            with self._guard:
                self._threads[job_id] = t
                self._local_running.add(job_id)
            t.start()
        else:
            with self._guard:
                self._local_running.add(job_id)
            self._execute(job_id, document_id, options)
        return self.get(job_id)

    def _execute(self, job_id: str, document_id: str, options: ProcessingOptions) -> None:
        self.repo.update_job(job_id, status=JobStatus.RUNNING, stage="processing", pid=os.getpid())

        def should_cancel() -> bool:
            return self.repo.cancel_requested(job_id)

        def progress(s: RunSummary, stage: str) -> None:
            self.repo.update_job(job_id, completed_pages=s.finished, failed_pages=s.failed, cached_pages=s.cached,
                                 stage=stage, errors=s.errors[-20:])

        try:
            summary = self.runner(document_id, options, job_id, should_cancel, progress)
            if summary.cancelled:
                status = JobStatus.CANCELLED
            elif summary.failed and summary.finished:
                status = JobStatus.PARTIAL
            elif summary.failed:
                status = JobStatus.FAILED
            else:
                status = JobStatus.COMPLETED
            self.repo.update_job(job_id, status=status, stage="done", completed_pages=summary.finished,
                                 failed_pages=summary.failed, cached_pages=summary.cached, errors=summary.errors[-50:])
        except Exception as exc:
            log.exception("job %s crashed", job_id)
            msg = getattr(exc, "message", None) or f"{exc.__class__.__name__}: {exc}"
            self.repo.update_job(job_id, status=JobStatus.FAILED, stage="error", errors=[msg])
        finally:
            with self._guard:
                self._local_running.discard(job_id)
