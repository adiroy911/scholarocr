"""Exception hierarchy. Every error carries a stable ``code`` that CLI/MCP surface to agents."""

from __future__ import annotations


class ScholarOCRError(Exception):
    code = "scholarocr_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, **({"details": self.details} if self.details else {})}


class ConfigurationError(ScholarOCRError):
    code = "configuration_error"


class AccessDeniedError(ScholarOCRError):
    """Path outside the authorized directories, or traversal attempt."""

    code = "access_denied"


class InputError(ScholarOCRError):
    code = "invalid_input"


class UnsupportedFormatError(InputError):
    code = "unsupported_format"


class CorruptDocumentError(InputError):
    code = "corrupt_document"


class EncryptedDocumentError(InputError):
    code = "encrypted_document"


class LimitExceededError(InputError):
    code = "limit_exceeded"


class NotFoundError(ScholarOCRError):
    code = "not_found"


class GeminiError(ScholarOCRError):
    code = "gemini_error"

    def __init__(self, message: str, *, category: str = "unknown", retryable: bool = False, details: dict | None = None):
        super().__init__(message, details=details)
        self.category = category
        self.retryable = retryable


class GeminiUnavailableError(GeminiError):
    """No API key configured or SDK client could not be created."""

    code = "gemini_unavailable"


class ResponseValidationError(GeminiError):
    """Model output failed schema validation even after the repair attempt."""

    code = "response_validation_failed"


class JobError(ScholarOCRError):
    code = "job_error"


class ExportError(ScholarOCRError):
    code = "export_error"


class SubmissionRejected(InputError):
    """Host mode: content submitted by the client's model failed validation. ``details["errors"]`` lists
    each problem with its JSON path and a fix; nothing was stored, so the model can correct and resubmit."""

    code = "submission_rejected"


class ModelUnavailableError(GeminiUnavailableError):
    """A server-side model call was requested in host mode (no key, no sampling)."""

    code = "model_unavailable"
