"""The single gateway to the Gemini API. No other module talks to the SDK.

Responsibilities: authentication, request construction (text + image parts), structured JSON
output against a schema, Pydantic validation with one controlled repair attempt, retry with
exponential backoff + jitter on transient errors, a requests-per-minute limiter, timeouts,
error classification, and usage logging (identifiers and token counts only — never document
content or the API key).

The SDK is reached through a small ``Transport`` protocol so tests can inject a fake backend
and so a future switch (e.g. to the Interactions API) is confined to ``SdkTransport``.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..exceptions import GeminiError, GeminiUnavailableError, ResponseValidationError
from ..logging_config import get_logger
from .schema_utils import to_gemini_schema

log = get_logger("gemini")
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    data: bytes
    mime_type: str = "image/png"


Part = TextPart | ImagePart


@dataclass
class RawResponse:
    text: str | None
    finish_reason: str | None = None
    usage: dict[str, int | None] = field(default_factory=dict)
    model_version: str | None = None


@dataclass
class CallInfo:
    model: str
    purpose: str
    attempts: int
    latency_ms: int
    usage: dict[str, int | None]
    repaired: bool = False
    model_version: str | None = None


class Transport(Protocol):
    def generate(self, *, model: str, system: str, parts: list[Part], response_schema: dict[str, Any],
                 temperature: float, media_resolution: str | None, timeout_s: float) -> RawResponse: ...


class SdkTransport:
    """Transport backed by the official ``google-genai`` SDK (``client.models.generate_content``)."""

    def __init__(self, api_key: str, timeout_s: float):
        from google import genai
        from google.genai import types

        self._types = types
        # SDK-level retries are disabled (attempts=1): GeminiClient owns retry policy so that
        # attempts are counted and logged in one place.
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=int(timeout_s * 1000), retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )

    def generate(self, *, model, system, parts, response_schema, temperature, media_resolution, timeout_s):
        types = self._types
        sdk_parts = []
        for p in parts:
            if isinstance(p, TextPart):
                sdk_parts.append(types.Part.from_text(text=p.text))
            else:
                sdk_parts.append(types.Part.from_bytes(data=p.data, mime_type=p.mime_type,
                                                       media_resolution=media_resolution))
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            response_mime_type="application/json",
            response_json_schema=response_schema,
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),
        )
        resp = self._client.models.generate_content(
            model=model, contents=[types.Content(role="user", parts=sdk_parts)], config=config
        )
        finish = None
        if resp.candidates:
            fr = resp.candidates[0].finish_reason
            finish = fr.value if hasattr(fr, "value") else (str(fr) if fr else None)
        try:
            text = resp.text
        except Exception:  # pragma: no cover - SDK raises on some blocked responses
            text = None
        um = resp.usage_metadata
        usage = {
            "prompt_tokens": getattr(um, "prompt_token_count", None),
            "output_tokens": getattr(um, "candidates_token_count", None),
            "thought_tokens": getattr(um, "thoughts_token_count", None),
            "total_tokens": getattr(um, "total_token_count", None),
        } if um else {}
        return RawResponse(text=text, finish_reason=finish, usage=usage, model_version=resp.model_version)


class ProviderHTTPError(Exception):
    """HTTP failure from a non-SDK transport; ``code``/``status`` mirror the SDK's ClientError."""

    def __init__(self, code: int, status: str | None, message: str):
        super().__init__(f"HTTP {code}: {message}")
        self.code, self.status = code, status


def to_strict_schema(node: Any) -> Any:
    """OpenAI-style strict schema: every object lists all its properties as required and forbids extras.

    Optional fields already allow ``null`` (see ``to_gemini_schema``), so requiring them only forces the
    model to state a value explicitly. Pydantic still validates the result client-side.
    """
    if isinstance(node, list):
        return [to_strict_schema(v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {k: to_strict_schema(v) for k, v in node.items()}
    if isinstance(out.get("properties"), dict):
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


class OpenRouterTransport:
    """Transport for OpenRouter (OpenAI-compatible ``/chat/completions``) with image input.

    Images go as base64 data URLs; structured output uses ``response_format: json_schema``, and
    ``provider.require_parameters`` keeps OpenRouter from routing to a host that would ignore it.
    ``media_resolution`` is Gemini-only and ignored here.
    """

    def __init__(self, api_key: str, timeout_s: float, base_url: str = "https://openrouter.ai/api/v1",
                 max_tokens: int = 16384, reasoning_effort: str = "low"):
        import httpx

        self._max_tokens, self._reasoning_effort = max_tokens, reasoning_effort
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "ScholarOCR"},
        )

    def generate(self, *, model, system, parts, response_schema, temperature, media_resolution, timeout_s):
        import base64

        content: list[dict[str, Any]] = []
        for p in parts:
            if isinstance(p, TextPart):
                content.append({"type": "text", "text": p.text})
            else:
                url = f"data:{p.mime_type};base64,{base64.b64encode(p.data).decode('ascii')}"
                content.append({"type": "image_url", "image_url": {"url": url}})
        body = {
            "model": model,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            # Strict mode with every property required: without it, weaker models silently omit
            # optional-with-default fields such as an element's ``text`` and still validate.
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "result", "strict": True,
                                                "schema": to_strict_schema(response_schema)}},
            "provider": {"require_parameters": True},
            # Transcription needs little reasoning; uncapped, some models think until the provider's
            # output limit (DeepSeek V4.1 Flash spent 131k reasoning tokens on one page) and return nothing.
            "max_tokens": self._max_tokens,
            "reasoning": {"effort": self._reasoning_effort, "exclude": True},
        }
        resp = self._client.post("/chat/completions", json=body, timeout=timeout_s)
        data: dict[str, Any] = {}
        try:
            data = resp.json()
        except ValueError:
            pass
        err = data.get("error") if isinstance(data, dict) else None
        if resp.status_code >= 400 or err:
            code = resp.status_code if resp.status_code >= 400 else int((err or {}).get("code") or 502)
            msg = str((err or {}).get("message") or resp.reason_phrase)[:300]
            raise ProviderHTTPError(code, None, msg)
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content")
        if isinstance(text, list):  # some providers return content parts
            text = "".join(c.get("text", "") for c in text if isinstance(c, dict))
        finish = choice.get("finish_reason")
        if finish == "length":
            finish = "MAX_TOKENS"
        elif finish == "content_filter":
            finish = "SAFETY"
        u = data.get("usage") or {}
        usage = {
            "prompt_tokens": u.get("prompt_tokens"),
            "output_tokens": u.get("completion_tokens"),
            "thought_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            "total_tokens": u.get("total_tokens"),
        }
        return RawResponse(text=text, finish_reason=finish, usage=usage, model_version=data.get("model"))


def classify_exception(exc: BaseException) -> tuple[str, bool]:
    """Map an exception to (category, retryable)."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        if code == 429:
            return "rate_limited", True
        if code in (500, 502, 503, 504):
            return "server_error", True
        if code == 408:
            return "timeout", True
        if code in (401, 403):
            return "auth_error", False
        if code == 404:
            return "model_not_found", False
        if code == 400:
            return "invalid_request", False
        return f"http_{code}", code >= 500
    name = exc.__class__.__name__.lower()
    if "timeout" in name or isinstance(exc, TimeoutError):
        return "timeout", True
    if "connect" in name or isinstance(exc, ConnectionError):
        return "network_error", True
    return "unknown", False


class RateLimiter:
    """Sliding-window requests-per-minute limiter shared by all threads. 0 disables it."""

    def __init__(self, rpm: int, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.rpm = rpm
        self._clock, self._sleep = clock, sleep
        self._times: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self.rpm <= 0:
            return
        while True:
            with self._lock:
                now = self._clock()
                while self._times and now - self._times[0] >= 60.0:
                    self._times.popleft()
                if len(self._times) < self.rpm:
                    self._times.append(now)
                    return
                wait = 60.0 - (now - self._times[0])
            self._sleep(max(wait, 0.05))


class GeminiClient:
    def __init__(self, settings: Settings, *, transport: Transport | None = None, usage_sink: Callable[..., None] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.settings = settings
        self._transport = transport
        self._usage_sink = usage_sink
        self._sleep = sleep
        self.limiter = RateLimiter(settings.gemini_rpm_limit, sleep=sleep)
        self._schema_cache: dict[type, dict] = {}

    @property
    def available(self) -> bool:
        return self._transport is not None or bool(self.settings.api_key)

    def _get_transport(self) -> Transport:
        if self._transport is None:
            s = self.settings
            if not s.api_key:
                raise GeminiUnavailableError(
                    f"{s.api_key_env_name} is not configured (provider={s.provider}). Visual OCR, classification "
                    "and review need it; pages with a reliable text layer can still be processed deterministically."
                )
            if s.provider == "openrouter":
                self._transport = OpenRouterTransport(s.api_key, s.gemini_timeout_seconds, s.openrouter_base_url,
                                                      s.openrouter_max_tokens, s.openrouter_reasoning_effort)
            else:
                self._transport = SdkTransport(s.api_key, s.gemini_timeout_seconds)
        return self._transport

    def generate_structured(self, *, purpose: str, model: str, system: str, parts: list[Part], schema: type[T],
                            prompt_version: str, document_id: str | None = None, page_number: int | None = None,
                            temperature: float = 0.0, media_resolution: str | None = "MEDIA_RESOLUTION_HIGH",
                            ) -> tuple[T, CallInfo]:
        """Call Gemini and return a validated ``schema`` instance.

        On invalid output, one repair request is made that includes the validation errors. If that
        also fails, ``ResponseValidationError`` is raised — malformed output is never accepted.
        """
        transport = self._get_transport()
        json_schema = self._schema_cache.setdefault(schema, to_gemini_schema(schema))
        started = time.monotonic()
        total_attempts = 0
        usage_total: dict[str, int] = {}
        model_version = None

        def call(call_parts: list[Part]) -> RawResponse:
            nonlocal total_attempts, model_version
            raw, attempts = self._with_retries(
                lambda: transport.generate(model=model, system=system, parts=call_parts, response_schema=json_schema,
                                           temperature=temperature, media_resolution=media_resolution,
                                           timeout_s=self.settings.gemini_timeout_seconds),
                purpose=purpose, model=model, prompt_version=prompt_version, document_id=document_id,
                page_number=page_number,
            )
            total_attempts += attempts
            model_version = raw.model_version or model_version
            for k, v in raw.usage.items():
                if v:
                    usage_total[k] = usage_total.get(k, 0) + v
            return raw

        raw = call(parts)
        parsed, error = self._parse(raw, schema)
        repaired = False
        if parsed is None:
            # Never log the validation text: Pydantic errors can quote document content.
            log.warning("invalid %s response (doc=%s page=%s, %d issue line(s)); attempting repair", purpose,
                        document_id, page_number, error.count("\n") + 1)
            self._record(purpose=f"{purpose}:invalid", model=model, prompt_version=prompt_version, status="invalid",
                         document_id=document_id, page_number=page_number, attempts=0, latency_ms=0, usage={},
                         error_category="validation")
            repair_parts = list(parts) + [TextPart(
                "Your previous response was rejected because it did not validate against the required JSON "
                f"schema.\nValidation errors:\n{error[:2000]}\nPrevious response (truncated):\n"
                f"{(raw.text or '')[:4000]}\nReturn the complete corrected JSON only."
            )]
            raw = call(repair_parts)
            parsed, error = self._parse(raw, schema)
            repaired = True
            if parsed is None:
                latency = int((time.monotonic() - started) * 1000)
                self._record(purpose=purpose, model=model, prompt_version=prompt_version, status="invalid",
                             document_id=document_id, page_number=page_number, attempts=total_attempts,
                             latency_ms=latency, usage=usage_total, error_category="validation")
                raise ResponseValidationError(
                    f"Gemini returned invalid structured output for {purpose} after a repair attempt",
                    category="validation", details={"errors": error[:1000], "finish_reason": raw.finish_reason},
                )
        latency = int((time.monotonic() - started) * 1000)
        self._record(purpose=purpose, model=model, prompt_version=prompt_version, status="ok", document_id=document_id,
                     page_number=page_number, attempts=total_attempts, latency_ms=latency, usage=usage_total)
        return parsed, CallInfo(model=model, purpose=purpose, attempts=total_attempts, latency_ms=latency,
                                usage=usage_total, repaired=repaired, model_version=model_version)

    def _with_retries(self, fn: Callable[[], RawResponse], **ctx) -> tuple[RawResponse, int]:
        max_retries = self.settings.gemini_max_retries
        attempt = 0
        while True:
            attempt += 1
            self.limiter.acquire()
            try:
                raw = fn()
            except GeminiError:
                raise
            except Exception as exc:  # SDK / transport errors
                category, retryable = classify_exception(exc)
                if retryable and attempt <= max_retries:
                    delay = min(60.0, 2.0 * (2 ** (attempt - 1))) * (0.5 + random.random())
                    log.warning("gemini %s (%s), retry %d/%d in %.1fs", ctx["purpose"], category, attempt,
                                max_retries, delay)
                    self._sleep(delay)
                    continue
                self._record(status="error", attempts=attempt, latency_ms=0, usage={}, error_category=category, **ctx)
                raise GeminiError(f"Gemini request failed ({category}): {exc.__class__.__name__}",
                                  category=category, retryable=retryable,
                                  details={"status": getattr(exc, "status", None), "code": getattr(exc, "code", None)},
                                  ) from None
            if raw.text is None or raw.text.strip() == "":
                reason = raw.finish_reason or "empty"
                if reason in ("MAX_TOKENS",):
                    category = "max_tokens"
                elif reason in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"):
                    category = "blocked"
                else:
                    category = "empty_response"
                if category == "empty_response" and attempt <= max_retries:
                    self._sleep(min(30.0, 2.0 * attempt))
                    continue
                self._record(status="error", attempts=attempt, latency_ms=0, usage=raw.usage, error_category=category, **ctx)
                raise GeminiError(f"Gemini returned no content (finish_reason={reason})", category=category)
            return raw, attempt

    @staticmethod
    def _parse(raw: RawResponse, schema: type[T]) -> tuple[T | None, str]:
        text = (raw.text or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):] if "{" in text else text
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            return None, f"not valid JSON: {e}"
        try:
            return schema.model_validate(data), ""
        except ValidationError as e:
            return None, str(e)

    def _record(self, *, purpose, model, prompt_version, status, document_id, page_number, attempts, latency_ms, usage,
                error_category=None) -> None:
        if self._usage_sink is None:
            return
        try:
            self._usage_sink(purpose=purpose, model=model, prompt_version=prompt_version, status=status,
                             document_id=document_id, page_number=page_number, attempts=attempts, latency_ms=latency_ms,
                             prompt_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("output_tokens"),
                             thought_tokens=usage.get("thought_tokens"), total_tokens=usage.get("total_tokens"),
                             error_category=error_category)
        except Exception:  # usage logging must never break processing
            log.exception("failed to record gemini usage")
