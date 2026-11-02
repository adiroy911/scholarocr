"""Job and verification schemas."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"  # finished, some pages failed (resumable)
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"  # process died mid-run (resumable)
    # Host mode: the server's work is done; some pages wait for the host model (submit_page_content).
    AWAITING_TRANSCRIPTION = "awaiting_transcription"


TERMINAL = {JobStatus.COMPLETED, JobStatus.PARTIAL, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED,
            JobStatus.AWAITING_TRANSCRIPTION}


class Job(BaseModel):
    job_id: str
    document_id: str
    status: JobStatus
    stage: str = "queued"
    total_pages: int = 0
    completed_pages: int = 0
    failed_pages: int = 0
    skipped_pages: int = 0
    cached_pages: int = 0
    options: dict[str, Any] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    pid: int | None = None


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Finding(BaseModel):
    check: str
    severity: Severity
    message: str
    page_number: int | None = None
    element_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class VerificationReport(BaseModel):
    document_id: str
    created_at: str
    scope: list[str]
    passed: bool  # no ERROR findings
    findings: list[Finding] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    reviewed_elements: list[str] = Field(default_factory=list)
    # Host mode with review=True: elements the host model should re-check (get_source_region -> submit_review).
    review_candidates: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in Severity}
        for f in self.findings:
            out[f.severity.value] += 1
        return out
