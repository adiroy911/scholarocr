"""ScholarOCR hosted MCP server for ChatGPT apps (streamable HTTP at ``/mcp``).

Differences from the local server (``scholarocr.mcp.server``, unchanged):

* No tool takes a filesystem path. Documents arrive as ChatGPT file parameters
  (``_meta["openai/fileParams"]``: objects with ``download_url`` and ``file_id``) or, as a fallback
  for other MCP clients, base64. They are staged in the caller's sandbox and ingested through the
  normal ``ScholarOCR.process`` pipeline; staging files are deleted right after ingestion.
* Every user has an isolated sandbox and service instance (own database, documents and exports).
* No key is needed. By default an account runs in host mode: the server does every deterministic step and
  ChatGPT's own model transcribes the pages that need vision (get_page_image -> submit_page_content). A user
  may link their own Gemini/OpenRouter key (at sign-in or later via manage_model_key) for the server-side
  pipeline. The operator's key is only used with ``SCHOLAROCR_REMOTE_OPERATOR_KEY_MODE=1``.
* Exports are delivered as short-lived signed download links instead of server paths.
* ``show_document_pages`` renders an MCP Apps UI resource (``ui://scholarocr/page-viewer-v1.html``);
  ``get_page_image`` renders ``ui://scholarocr/page-image-v1.html``, which makes the page image visible to
  ChatGPT's model (uploadFile + widget-state imageIds), since ChatGPT does not reliably show tool-result
  images to the model.

Run:  scholarocr-remote --public-url https://ocr.example.com      (see docs/CHATGPT_APP.md)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from .. import __version__
from ..config import APP_NAME
from ..exceptions import InputError, ScholarOCRError
from ..logging_config import get_logger
from ..schemas import DocumentType, ElementType, ProcessingOptions
from ..utils import parse_page_range
from .config import RemoteConfig, print_warnings
from ..mcp.server import client_name
from .oauth import SCOPE, ByokOAuthProvider, make_connect_routes, make_key_link_route
from .ratelimit import RateLimited, UserLimits
from .sandbox import Fetcher, HttpFetcher, UserSandbox, decode_base64
from .services import ByoKeyRequired, ServicePool, TransportFactory
from .store import AuthStore, derive_key

log = get_logger("remote.server")
WIDGET_URI = "ui://scholarocr/page-viewer-v1.html"
WIDGET_MIME = "text/html;profile=mcp-app"
PAGE_IMAGE_URI = "ui://scholarocr/page-image-v1.html"
PAGE_IMAGE_META = "scholarocr/pageImage"  # _meta key: delivered to the component only, hidden from the model
KEY_LINK_TTL_S = 900
DEFAULT_ORIGINS = ["https://chatgpt.com", "https://chat.openai.com"]
# Names a remote tool must never accept (enforced by tests/remote/test_tool_surface.py).
FORBIDDEN_PARAMS = {"file_path", "path", "paths", "output_dir", "folder", "directory"}

INSTRUCTIONS = f"""{APP_NAME} extracts structured, source-traceable content from academic documents the user
uploads (scanned PDFs, page photos, handwritten notes, exam papers, papers). Flow: ocr_upload with the user's
files -> get_document_status if a job_id came back -> show_document_pages to display pages with uncertainty
highlights, or get_document_content for text -> get_source_region to check an uncertain element against the
original image -> verify_document before relying on uncertain content -> export_document for a download link.

HOST MODE (no key linked; ocr_upload reports "mode"): YOU are the vision model. Pages with a reliable PDF text
layer are done at once; the rest are listed in "awaiting_transcription". One page at a time:
get_page_image(document_id) -> transcribe that page exactly -> submit_page_content(document_id, page_number,
elements=[...]). If a submission is rejected, fix every listed problem and resubmit the whole page. In ChatGPT
the page image reaches you through the viewer as an attached image (possibly on the next turn); if you cannot
see it, say so instead of guessing. A user who prefers the server-side pipeline can add a key with
manage_model_key(action="link").

Uncertainty labels are ordinal judgements, not probabilities; "unresolved" content was not guessed."""

DocTypeLit = Literal[tuple(t.value for t in DocumentType)]  # type: ignore[valid-type]
ElementTypeLit = Literal[tuple(t.value for t in ElementType)]  # type: ignore[valid-type]
FormatLit = Literal["json", "markdown", "searchable-pdf", "docx", "latex", "csv", "xlsx"]
ScopeLit = Literal["completeness", "duplicates", "page_order", "content", "reading_order", "math", "tables",
                   "structure", "uncertainty", "traceability", "exports"]
OptDocType = Annotated[DocTypeLit | None, Field(description="Optional hint, e.g. exam_paper, handwritten_notes.")]
OptLang = Annotated[str | None, Field(description="e.g. 'en', 'hi, en'")]
OptCtx = Annotated[str | None, Field(description="Free-text context, e.g. 'Class 11 chemistry exam'. A hint.")]
OptPages = Annotated[str | None, Field(description="Page range like '1-5,8'. Default: all.")]
OptDetail = Annotated[Literal["standard", "high"], Field(description="'high' adds a second model pass for every "
                                                                      "equation/table (more model cost).")]
OptWait = Annotated[bool | None, Field(description="True: block until done. False: return a job id at once. "
                                                   "Default: wait only for small page selections.")]


def _ann(read_only: bool, destructive: bool = False, open_world: bool = False, idempotent: bool = True):
    return ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive, idempotent_hint=idempotent,
                           open_world_hint=open_world)


class OpenAIFile(BaseModel):
    """A file the user attached in ChatGPT. ChatGPT fills ``download_url`` and ``file_id``."""

    download_url: str = Field(description="Temporary download URL provided by ChatGPT.")
    file_id: str = Field(description="ChatGPT file id.")
    mime_type: str = Field(default="", description="MIME type, if known.")
    file_name: str = Field(default="", description="Original file name, if known.")


class Base64File(BaseModel):
    file_name: str = Field(default="", description="Original file name, e.g. 'page1.jpg'.")
    data_base64: str = Field(description="File bytes, base64-encoded (a data: URL prefix is accepted).")
    mime_type: str = Field(default="", description="Ignored for detection; types are sniffed from the bytes.")


class RemoteToolError(Exception):
    def __init__(self, payload: dict[str, Any], *, auth_challenge: str | None = None):
        super().__init__(payload.get("message", ""))
        self.payload = payload
        self.auth_challenge = auth_challenge


# ---------------------------------------------------------------------- signed download links
class DownloadSigner:
    def __init__(self, secret: str, ttl_s: int):
        self._key = derive_key(secret, "scholarocr/download/v1")
        self.ttl_s = ttl_s

    @staticmethod
    def _b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    @staticmethod
    def _unb64(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    def sign(self, user_id: str, rel_path: str, now: float | None = None) -> str:
        body = self._b64(json.dumps({"u": user_id, "p": rel_path, "e": int((now or time.time()) + self.ttl_s)},
                                    separators=(",", ":")).encode())
        mac = self._b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest()[:24])
        return f"{body}.{mac}"

    def verify(self, token: str, now: float | None = None) -> tuple[str, str] | None:
        try:
            body, mac = token.split(".", 1)
            expected = self._b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest()[:24])
            if not hmac.compare_digest(mac, expected):
                return None
            data = json.loads(self._unb64(body))
            if data["e"] < (now or time.time()):
                return None
            return str(data["u"]), str(data["p"])
        except (ValueError, KeyError, TypeError):
            return None


def _scrub(obj: Any, roots: list[str]) -> Any:
    """Remove server filesystem locations from anything returned to the client."""
    if isinstance(obj, str):
        for r in roots:
            if r and r in obj:
                obj = obj.replace(r, "")
        return obj
    if isinstance(obj, dict):
        return {k: _scrub(v, roots) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, roots) for v in obj]
    return obj


def _text(payload: Any, limit: int = 60000) -> str:
    s = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + " …(truncated; see structuredContent)"


@dataclass
class RemoteComponents:
    cfg: RemoteConfig
    store: AuthStore
    pool: ServicePool
    limits: UserLimits
    signer: DownloadSigner
    provider: ByokOAuthProvider | None
    mcp: MCPServer


IdentityFn = Callable[[], str | None]


def default_identity(cfg: RemoteConfig) -> IdentityFn:
    def identity() -> str | None:
        if cfg.auth == "none":
            return "local"
        token = get_access_token()
        return token.subject if token is not None else None

    return identity


def build_remote_server(cfg: RemoteConfig, *, store: AuthStore, pool: ServicePool, limits: UserLimits | None = None,
                        fetcher: Fetcher | None = None, identity: IdentityFn | None = None,
                        provider: ByokOAuthProvider | None = None,
                        signer: DownloadSigner | None = None) -> RemoteComponents:
    limits = limits or UserLimits(cfg.calls_per_minute, cfg.ocr_calls_per_hour, cfg.pages_per_day)
    fetcher = fetcher or HttpFetcher(cfg.download_allowed_hosts)
    identity = identity or default_identity(cfg)
    signer = signer or DownloadSigner(cfg.secret, cfg.download_link_ttl_s)
    resource_metadata = f"{cfg.public_url}/.well-known/oauth-protected-resource/mcp"

    auth_kwargs: dict[str, Any] = {}
    if cfg.auth == "oauth":
        provider = provider or ByokOAuthProvider(cfg, store)
        auth_kwargs = {
            "auth_server_provider": provider,
            "auth": AuthSettings(
                issuer_url=cfg.public_url, resource_server_url=cfg.mcp_url, required_scopes=[SCOPE],
                validate_token_resource=False,  # load_access_token checks the RFC 8707 resource itself
                client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE],
                                                                      default_scopes=[SCOPE]),
                revocation_options=RevocationOptions(enabled=True),
            ),
        }
    security = ([{"type": "oauth2", "scopes": [SCOPE]}] if cfg.auth == "oauth" else [{"type": "noauth"}])
    mcp = MCPServer("scholarocr-remote", title=APP_NAME, instructions=INSTRUCTIONS, version=__version__,
                    website_url=cfg.public_url, **auth_kwargs)

    def tool_meta(extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"securitySchemes": security, **(extra or {})}

    # ------------------------------------------------------------------ plumbing
    def challenge(description: str) -> str | None:
        if cfg.auth != "oauth":
            return None
        desc = description.replace('"', "'")
        return (f'Bearer resource_metadata="{resource_metadata}", error="invalid_token", '
                f'error_description="{desc}"')

    def current_user() -> str:
        uid = identity()
        if not uid:
            raise RemoteToolError({"error": "unauthenticated", "message": "Sign in to ScholarOCR to continue."},
                                  auth_challenge=challenge("sign-in required"))
        return uid

    def roots_for(uid: str) -> list[str]:
        root = cfg.users_dir / uid
        out = set()
        for p in (root, root.resolve()):
            out.add(str(p))
            out.add(str(p).replace("\\", "/"))
            out.add(str(p).replace("\\", "\\\\"))
        return sorted(out, key=len, reverse=True)

    def run(fn: Callable[[str], Any], *, ocr: bool = False) -> CallToolResult:
        try:
            uid = current_user()
            limits.calls.hit(uid, "tool calls per minute")
            if ocr:
                limits.ocr_calls.check(uid, "OCR requests per hour")
                limits.pages.check(uid, "pages per day")
            if not store.account_exists(uid) and cfg.auth == "oauth":
                raise RemoteToolError({"error": "unauthenticated", "message": "Account not found; reconnect."},
                                      auth_challenge=challenge("account removed"))
            store.touch_user(uid)
            result = fn(uid)
            if ocr:
                limits.ocr_calls.record(uid)
            if isinstance(result, CallToolResult):
                return result
            clean = _scrub(result, roots_for(uid))
            return CallToolResult(content=[TextContent(type="text", text=_text(clean))], structured_content=clean)
        except RemoteToolError as e:
            return _error(e.payload, e.auth_challenge)
        except ByoKeyRequired as e:
            return _error({"error": "operator_key_missing",
                           "message": f"{e}. The server operator must configure the key, or the user can link "
                                      "their own with manage_model_key(action='link')."})
        except RateLimited as e:
            return _error({"error": "rate_limited", "message": str(e), "retry_after_s": int(e.retry_after_s) + 1})
        except ScholarOCRError as e:
            return _error(e.to_dict())
        except ValueError as e:
            return _error({"error": "invalid_input", "message": str(e)})
        except Exception as e:  # never leak server paths or stack details to the client
            import httpx

            if isinstance(e, httpx.HTTPError):
                return _error({"error": "download_failed", "message": f"could not download the file "
                                                                       f"({e.__class__.__name__})"})
            log.exception("remote tool failed")
            return _error({"error": "internal_error", "message": f"unexpected server error ({e.__class__.__name__})"})

    def _error(payload: dict[str, Any], auth_challenge: str | None = None) -> CallToolResult:
        meta = {"mcp/www_authenticate": [auth_challenge]} if auth_challenge else None
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                              is_error=True, meta=meta)

    def svc_ro(uid: str):
        return pool.get_readonly(uid)

    def options(document_type, language_hint, context_hint, pages, detail, force_visual, auto_crop, review):
        return ProcessingOptions(document_type=document_type, language_hint=language_hint, context_hint=context_hint,
                                 pages=pages, detail=detail, force_visual=force_visual, auto_crop=auto_crop,
                                 review=review, reprocess=False)

    def ingest(uid: str, items: list[tuple[bytes, str]], opts: ProcessingOptions, wait: bool | None) -> dict:
        svc = pool.get(uid)
        sandbox = pool.sandbox(uid)
        batch = sandbox.new_batch()
        try:
            staged = [sandbox.stage(batch, data, name, i) for i, (data, name) in enumerate(items)]
            result = svc.process([s.path for s in staged], opts, wait=wait)
        finally:
            UserSandbox.discard(batch)  # the service keeps its own preserved copy
        limits.pages.record(uid, int(result.get("selected_pages") or 0))
        result["files"] = [{"name": s.filename, "type": s.media_type, "bytes": s.size} for s in staged]
        return result

    def check_batch(n: int) -> None:
        if n == 0:
            raise InputError("attach at least one file")
        if n > cfg.max_files_per_call:
            raise InputError(f"at most {cfg.max_files_per_call} files per call (SCHOLAROCR_REMOTE_MAX_FILES)")

    # ------------------------------------------------------------------ upload tools
    @mcp.tool(annotations=_ann(False, open_world=True, idempotent=False),
              meta=tool_meta({"openai/fileParams": ["files"],
                              "openai/toolInvocation/invoking": "Reading your document…",
                              "openai/toolInvocation/invoked": "Document read"}))
    def ocr_upload(
        files: Annotated[list[OpenAIFile], Field(description="The user's uploaded files, in page order. All files "
                                                             "form ONE document (e.g. several page photos).")],
        document_type: OptDocType = None, language_hint: OptLang = None, context_hint: OptCtx = None,
        pages: OptPages = None, detail: OptDetail = "standard",
        force_visual: Annotated[bool, Field(description="Ignore embedded PDF text; always run visual OCR.")] = False,
        auto_crop: Annotated[bool, Field(description="Crop photos/screenshots to the page.")] = True,
        review: Annotated[bool, Field(description="Targeted second-pass review of flagged regions.")] = True,
        wait: OptWait = None,
    ) -> CallToolResult:
        """Extract documents the user uploaded (PDF or images) into structured, source-traceable content.

        Returns document_id, "mode" and a summary or a job_id to poll with get_document_status. mode "host"
        (no key linked): pages in awaiting_transcription need YOUR vision via get_page_image ->
        submit_page_content. With a linked key the server's pipeline reads them. Re-sending reuses results."""

        def go(uid: str):
            check_batch(len(files))
            max_bytes = int(cfg.max_file_mb * 1048576)
            items, total = [], 0
            for f in files:
                data, _ctype = fetcher.fetch(f.download_url, max_bytes=max_bytes, timeout_s=cfg.download_timeout_s)
                total += len(data)
                if total > cfg.max_call_mb * 1048576:
                    raise InputError(f"files exceed {cfg.max_call_mb:.0f} MB in total (SCHOLAROCR_REMOTE_MAX_CALL_MB)")
                items.append((data, f.file_name or None))
            return ingest(uid, items, options(document_type, language_hint, context_hint, pages, detail,
                                              force_visual, auto_crop, review), wait)

        return run(go, ocr=True)

    if cfg.allow_base64:
        @mcp.tool(annotations=_ann(False, open_world=True, idempotent=False),
                  meta=tool_meta({"openai/toolInvocation/invoking": "Reading your document…",
                                  "openai/toolInvocation/invoked": "Document read"}))
        def ocr_upload_base64(
            files: Annotated[list[Base64File], Field(description="Files as base64, in page order; one document.")],
            document_type: OptDocType = None, language_hint: OptLang = None, context_hint: OptCtx = None,
            pages: OptPages = None, detail: OptDetail = "standard",
            force_visual: Annotated[bool, Field(description="Ignore embedded PDF text.")] = False,
            auto_crop: Annotated[bool, Field(description="Crop photos/screenshots to the page.")] = True,
            review: Annotated[bool, Field(description="Targeted second-pass review of flagged regions.")] = True,
            wait: OptWait = None,
        ) -> CallToolResult:
            """Fallback for MCP clients without file uploads: same as ocr_upload, with small base64 files
            (limit per file set by SCHOLAROCR_REMOTE_MAX_BASE64_MB). Prefer ocr_upload in ChatGPT."""

            def go(uid: str):
                check_batch(len(files))
                max_bytes = int(min(cfg.max_base64_mb, cfg.max_file_mb) * 1048576)
                items = [(decode_base64(f.data_base64, max_bytes), f.file_name or None) for f in files]
                if sum(len(d) for d, _ in items) > cfg.max_call_mb * 1048576:
                    raise InputError(f"files exceed {cfg.max_call_mb:.0f} MB in total")
                return ingest(uid, items, options(document_type, language_hint, context_hint, pages, detail,
                                                  force_visual, auto_crop, review), wait)

            return run(go, ocr=True)

    # ------------------------------------------------------------------ retrieval
    @mcp.tool(annotations=_ann(True), meta=tool_meta())
    def get_document_status(
        job_id: Annotated[str | None, Field(description="Job id returned by ocr_upload.")] = None,
        document_id: Annotated[str | None, Field(description="Alternatively, a document id.")] = None,
    ) -> CallToolResult:
        """Progress of a processing job (status, completed/total pages, errors), or a document summary
        (page statuses, outline, uncertainty counts)."""

        def go(uid: str):
            svc = svc_ro(uid)
            if job_id:
                return svc.job_status(job_id)
            if document_id:
                return svc.document_summary(document_id)
            raise InputError("Provide job_id or document_id")

        return run(go)

    @mcp.tool(annotations=_ann(True), meta=tool_meta())
    def get_document_content(
        document_id: str,
        pages: Annotated[str | None, Field(description="Page range like '3-7'. Default: all.")] = None,
        element_types: Annotated[list[ElementTypeLit] | None, Field(description="Only these element types.")] = None,
        format: Annotated[Literal["markdown", "json", "text"], Field(description="markdown (with element id "
                                                                     "anchors), json (full IR) or text.")] = "markdown",
        max_chars: Annotated[int, Field(ge=1000, le=100000)] = 30000,
        cursor: Annotated[int | None, Field(description="next_cursor from a previous call.")] = None,
    ) -> CallToolResult:
        """Extracted content, paginated by page. Markdown has <!-- el:ID --> anchors usable with
        get_source_region. Unreadable regions appear as ⟦unclear: …⟧."""
        return run(lambda uid: svc_ro(uid).get_content(document_id, pages=pages, element_types=element_types,
                                                       fmt=format, max_chars=max_chars, cursor=cursor))

    @mcp.tool(annotations=_ann(True), meta=tool_meta())
    def get_page_content(
        document_id: str,
        page_number: Annotated[int, Field(ge=1)],
        compact: Annotated[bool, Field(description="Only id, type, role, content, latex, uncertainty (+reasons), "
                                       "question and bbox per element.")] = False,
        fields: Annotated[list[str] | None, Field(description="Keep only these element fields; element_id is "
                                                  "always kept.")] = None,
    ) -> CallToolResult:
        """Structured IR of one page: elements with type, content, LaTeX, tables, bounding boxes, uncertainty,
        reading order and source references. Use compact=true or fields=[...] unless you need everything."""
        return run(lambda uid: svc_ro(uid).get_page(document_id, page_number, compact=compact, fields=fields))

    @mcp.tool(annotations=_ann(True), meta=tool_meta())
    def get_source_region(
        document_id: str,
        element_id: Annotated[str | None, Field(description="Element id from extracted content.")] = None,
        page_number: Annotated[int | None, Field(ge=1, description="Page, if no element_id.")] = None,
        bbox: Annotated[list[float] | None, Field(description="[x0,y0,x1,y1] in PDF points with page_number.")] = None,
    ) -> CallToolResult:
        """The ORIGINAL source image of an element (cropped) or of a page/region, plus metadata. Use it to check
        uncertain equations, tables, handwriting or figures against the source."""

        def go(uid: str):
            png, meta = svc_ro(uid).get_source_region(document_id, element_id=element_id, page_number=page_number,
                                                      bbox=bbox)
            meta = _scrub(meta, roots_for(uid))
            return CallToolResult(content=[ImageContent(type="image", data=base64.b64encode(png).decode(),
                                                        mime_type="image/png"),
                                           TextContent(type="text", text=json.dumps(meta, ensure_ascii=False))],
                                  structured_content=meta)

        return run(go)

    # ------------------------------------------------------------------ host mode
    @mcp.tool(annotations=_ann(True),
              meta=tool_meta({"ui": {"resourceUri": PAGE_IMAGE_URI}, "openai/outputTemplate": PAGE_IMAGE_URI,
                              "openai/toolInvocation/invoking": "Preparing the page…",
                              "openai/toolInvocation/invoked": "Page ready"}))
    def get_page_image(
        document_id: str,
        page_number: Annotated[int | None, Field(ge=1, description="Page to transcribe. Default: the next page "
                                                 "awaiting transcription.")] = None,
        instructions: Annotated[Literal["auto", "full", "short"], Field(
            description="'full': rules + element JSON examples; 'short': a reminder; 'auto': full until the "
                        "first page of this document is submitted.")] = "auto",
    ) -> CallToolResult:
        """HOST MODE: one page that needs your vision, with transcription rules and the exact element shape.
        Transcribe it, then call submit_page_content. Boxes: box_2d = [ymin, xmin, ymax, xmax] in 0-1000 of the
        page image. In ChatGPT the image is attached through the viewer. One page per call."""

        def go(uid: str):
            data, mime, info = pool.get(uid).host_page_image(document_id, page_number, instructions=instructions)
            info = _scrub(info, roots_for(uid))
            brief = info.pop("instructions", None)
            if data is None:
                return CallToolResult(content=[TextContent(type="text", text=json.dumps(info))], structured_content=info)
            b64 = base64.b64encode(data).decode()
            text = (f"{brief}\n\nPage info: {json.dumps(info, ensure_ascii=False)}\n\nThe page image is shown to "
                    "the user in the viewer and attached for you. If you cannot see an image of this page, say so "
                    "and do not transcribe from memory or guess.")
            return CallToolResult(content=[ImageContent(type="image", data=b64, mime_type=mime),
                                           TextContent(type="text", text=text)],
                                  structured_content=info, meta={PAGE_IMAGE_META: {"data": b64, "mime_type": mime}})

        return run(go)

    @mcp.tool(annotations=_ann(False), meta=tool_meta({"openai/toolInvocation/invoking": "Saving the page…",
                                                       "openai/toolInvocation/invoked": "Page saved"}))
    def submit_page_content(
        document_id: str,
        page_number: Annotated[int, Field(ge=1)],
        # Typed loosely on purpose: a malformed list (e.g. [0, 1, 2]) or a JSON string must reach
        # validate_submission, which answers with path/problem/fix instead of a raw pydantic error.
        elements: Annotated[list[Any] | str, Field(description=(
            "Elements in reading order. Each: {type, text, latex?, role?, level?, items?, table?, figure?, "
            "question?, box_2d:[ymin,xmin,ymax,xmax] 0-1000, uncertainty (doubt): low|medium|high|unresolved, "
            "uncertainty_reasons?, handwritten?, language?, caption_of?, parent_index?}. "
            "See get_page_image(instructions='full') for examples."))],
        page_language: Annotated[str | None, Field(description="BCP-47 code, e.g. 'en'.")] = None,
        page_label: Annotated[str | None, Field(description="Printed page number, if visible.")] = None,
        page_doubt: Annotated[str | None, Field(description="Overall DOUBT about the page (not quality): low "
                                                "for a clean page | medium | high | unresolved.")] = None,
        rotation_detected: Annotated[int, Field(description="0, or 90/180/270 if the image is rotated clockwise.")] = 0,
        notes: Annotated[list[str] | None, Field(description="Problems such as cut-off text.")] = None,
        legibility: Annotated[str | None, Field(description="Deprecated old name of page_doubt.")] = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> CallToolResult:
        """HOST MODE: store your transcription of one page (from get_page_image). Strictly validated; on
        rejection every problem is listed with its path and a fix, nothing is stored, and you resubmit the
        complete page. Returns counts, review_suggested and the next waiting page."""
        payload: dict[str, Any] = {"elements": elements, "page_language": page_language, "page_label": page_label,
                                   "rotation_detected": rotation_detected, "notes": notes or []}
        if page_doubt is not None:
            payload["page_doubt"] = page_doubt
        if legibility is not None:
            payload["legibility"] = legibility
        who = client_name(ctx)
        return run(lambda uid: pool.get(uid).submit_page_content(document_id, page_number, payload, transcriber=who))

    @mcp.tool(annotations=_ann(False, idempotent=False), meta=tool_meta())
    def submit_review(
        document_id: str,
        element_id: str,
        verdict: Annotated[Literal["confirmed", "corrected", "unreadable"], Field(
            description="After comparing the element with its crop from get_source_region.")],
        text: Annotated[str, Field(description="Corrected text (verdict 'corrected').")] = "",
        latex: Annotated[str | None, Field(description="Corrected LaTeX.")] = None,
        table: Annotated[dict[str, Any] | None, Field(description="Corrected table {rows, header_rows}.")] = None,
        figure: Annotated[dict[str, Any] | None, Field(description="For figures: {description, labels, "
                                                       "relationships, kind}.")] = None,
        uncertainty: Annotated[str, Field(description="low|medium|high|unresolved after your check.")] = "low",
        notes: list[str] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> CallToolResult:
        """Second pass on one element after looking at get_source_region(element_id): record your verdict.
        Replaced values are kept in revisions."""
        payload: dict[str, Any] = {"verdict": verdict, "text": text, "uncertainty": uncertainty, "notes": notes or []}
        for k, v in (("latex", latex), ("table", table), ("figure", figure)):
            if v is not None:
                payload[k] = v
        who = client_name(ctx)
        return run(lambda uid: pool.get(uid).submit_review(document_id, element_id, payload, transcriber=who))

    @mcp.tool(annotations=_ann(True),
              meta=tool_meta({"ui": {"resourceUri": WIDGET_URI}, "openai/outputTemplate": WIDGET_URI,
                              "openai/toolInvocation/invoking": "Loading pages…",
                              "openai/toolInvocation/invoked": "Pages shown"}))
    def show_document_pages(
        document_id: str,
        pages: Annotated[str | None, Field(description="Up to 5 pages, e.g. '1-3'. Default: first 3.")] = None,
    ) -> CallToolResult:
        """Show extracted pages to the user in an interactive viewer with uncertainty highlights
        (medium / high / unresolved). Use after ocr_upload has completed."""

        def go(uid: str):
            data = page_view(svc_ro(uid), document_id, pages)
            flagged = sum(data["counts"].get(k, 0) for k in ("medium", "high", "unresolved"))
            summary = (f"Showing pages {data['range']['first']}-{data['range']['last']} of {data['page_count']} "
                       f"({flagged} flagged element(s)). The user sees them in the viewer.")
            return CallToolResult(content=[TextContent(type="text", text=summary)],
                                  structured_content=_scrub(data, roots_for(uid)))

        return run(go)

    # ------------------------------------------------------------------ quality, export, catalogue
    @mcp.tool(annotations=_ann(False, open_world=True), meta=tool_meta())
    def verify_document(
        document_id: str,
        scope: Annotated[list[ScopeLit] | None, Field(description="Checks to run. Default: all.")] = None,
        review: Annotated[bool, Field(description="With a linked key: send flagged elements to the review model. "
                                      "Host mode: list review_candidates for you to re-check (get_source_region "
                                      "-> submit_review).")] = False,
    ) -> CallToolResult:
        """Quality checks on an extraction: missing/duplicate/failed pages, reading order, invalid LaTeX, table
        structure, question numbering, unresolved regions, traceability."""

        def go(uid: str):
            report = pool.get(uid).verify(document_id, scope=scope, review=review)
            d = report.model_dump(mode="json")
            d["counts"] = report.counts()
            if len(d["findings"]) > 200:
                d["findings_truncated"] = len(d["findings"]) - 200
                d["findings"] = d["findings"][:200]
            return d

        return run(go)

    @mcp.tool(annotations=_ann(False), meta=tool_meta())
    def export_document(
        document_id: str,
        format: FormatLit,
        options: Annotated[dict[str, Any] | None, Field(description="Format options, e.g. {'include_furniture': "
                                                        "true} for markdown.")] = None,
    ) -> CallToolResult:
        """Generate a file from the stored extraction (no OCR is re-run) and return short-lived download links."""

        def go(uid: str):
            res = svc_ro(uid).export(document_id, format, options=options)
            data_dir = pool.sandbox(uid).data_dir
            links = []
            for f in res.get("files") or []:
                p = Path(f).resolve()
                if p.is_file() and p.is_relative_to(data_dir):
                    rel = p.relative_to(data_dir).as_posix()
                    links.append({"name": p.name, "bytes": p.stat().st_size,
                                  "url": f"{cfg.public_url}/download/{signer.sign(uid, rel)}"})
            primary = next((lk["url"] for lk in links if res.get("primary_file") and
                            Path(res["primary_file"]).name == lk["name"]), links[0]["url"] if links else None)
            return {"document_id": document_id, "format": res.get("format", format), "download_url": primary,
                    "files": links, "expires_in_s": signer.ttl_s, "warnings": res.get("warnings", []),
                    "verification": res.get("verification")}

        return run(go)

    @mcp.tool(annotations=_ann(True), meta=tool_meta())
    def list_documents(
        query: Annotated[str | None, Field(description="Substring of filename or title.")] = None,
        status: str | None = None,
        document_type: DocTypeLit | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> CallToolResult:
        """List this user's documents (deleted automatically after the retention period)."""
        return run(lambda uid: svc_ro(uid).list_documents(query=query, status=status, document_type=document_type,
                                                          limit=limit, offset=offset))

    @mcp.tool(annotations=_ann(False, open_world=True), meta=tool_meta())
    def classify_document(
        document_id: str,
        context_hint: Annotated[str | None, Field(description="Optional context; a prior that may be wrong.")] = None,
        pages: Annotated[str | None, Field(description="Pages to consider (max 10).")] = None,
    ) -> CallToolResult:
        """Classify the document (exam paper, textbook, handwritten notes, research paper, ...) with subjects,
        level, topics and an ordinal certainty. Needs a linked model key; in host mode it returns
        model_unavailable (classify from the extracted content yourself)."""
        return run(lambda uid: pool.get(uid).classify(document_id, context_hint=context_hint, pages=pages))

    @mcp.tool(annotations=_ann(False, destructive=True), meta=tool_meta())
    def delete_document(
        document_id: str,
        mode: Annotated[Literal["generated", "all"], Field(description="'generated': extraction and exports only. "
                                                           "'all': also the stored upload and record.")] = "all",
        confirm: Annotated[bool, Field(description="Required for mode='all'. Only when the user asked.")] = False,
    ) -> CallToolResult:
        """Delete a stored document on explicit user instruction."""
        return run(lambda uid: svc_ro(uid).delete(document_id, mode=mode, confirm=confirm))

    @mcp.tool(annotations=_ann(False, open_world=True, idempotent=False), meta=tool_meta())
    def manage_job(job_id: str, action: Literal["cancel", "resume"]) -> CallToolResult:
        """Cancel a running job, or resume a cancelled/failed one (finished pages are reused)."""
        if action == "cancel":
            return run(lambda uid: svc_ro(uid).cancel_job(job_id))
        return run(lambda uid: pool.get(uid).resume_job(job_id, wait=False))

    # ------------------------------------------------------------------ account
    @mcp.tool(annotations=_ann(True), meta=tool_meta())
    def account_status() -> CallToolResult:
        """How this account reads pages (host mode, own key or operator quota; the key is never shown), usage
        against limits, and retention."""

        def go(uid: str):
            k = pool.key_for(uid)
            own = bool(k and k.has_own_key)
            mode = (k.provider if own else "operator" if cfg.operator_key_mode else "host")
            return {"mode": mode,
                    "provider": k.provider if own else None,
                    "key": (k.key_hint or "linked") if own else ("operator-provided" if cfg.operator_key_mode
                                                                  else "none (host mode)"),
                    "billing": ("your own key" if own else "operator" if cfg.operator_key_mode
                                else "no model API calls (your chat model reads the pages)"),
                    "change_key": "manage_model_key(action='link' | 'unlink')",
                    "limits": limits.snapshot(uid),
                    "storage_mb": round(pool.sandbox(uid).usage_bytes() / 1048576, 2),
                    "storage_quota_mb": cfg.user_quota_mb, "retention_hours": cfg.retention_hours,
                    "max_file_mb": cfg.max_file_mb, "max_pages": cfg.max_pages}

        return run(go)

    @mcp.tool(annotations=_ann(False, idempotent=False), meta=tool_meta())
    def manage_model_key(
        action: Annotated[Literal["link", "unlink"], Field(description="'link': get a one-time page where the user "
                                                           "adds or replaces their own Gemini/OpenRouter key. "
                                                           "'unlink': remove the key (back to host mode).")],
    ) -> CallToolResult:
        """Optional own model key for the server-side OCR pipeline. Never ask the user to paste a key into the
        chat: 'link' returns a short-lived page URL for the user to open. Documents are kept either way."""

        def go(uid: str):
            if action == "unlink":
                removed = store.unlink_key(uid)
                pool.drop(uid)
                return {"unlinked": removed, "mode": "operator" if cfg.operator_key_mode else "host",
                        "note": "No key is stored any more. Pages that need vision are now transcribed by you "
                                "(host mode)." if not cfg.operator_key_mode else "Using the operator quota."}
            token = store.new_link_token(uid, KEY_LINK_TTL_S)
            return {"url": f"{cfg.public_url}/byok/key?t={token}", "expires_in_s": KEY_LINK_TTL_S,
                    "message": "Give the user this link to open in their browser. The key is entered on that page, "
                               "never in the chat. The link works once."}

        return run(go)

    @mcp.tool(annotations=_ann(False, destructive=True), meta=tool_meta())
    def delete_my_data(
        confirm: Annotated[bool, Field(description="Must be true. Only when the user explicitly asked.")] = False,
    ) -> CallToolResult:
        """Delete everything ScholarOCR stores for this user: documents, results, exports, the linked API key and
        all sessions. The app must be reconnected afterwards."""

        def go(uid: str):
            if not confirm:
                raise InputError("pass confirm=true to delete all your data and unlink your key")
            from .retention import Janitor

            Janitor(pool, store).remove_user(uid)
            return {"deleted": True, "note": "All documents, results and the linked key were removed."}

        return run(go)

    # ------------------------------------------------------------------ UI resource
    @mcp.resource(WIDGET_URI, name="scholarocr-page-viewer", title="ScholarOCR page viewer", mime_type=WIDGET_MIME,
                  description="Extracted pages with uncertainty highlights.",
                  meta={"ui": {"prefersBorder": True, "domain": cfg.widget_domain or cfg.public_url,
                               "csp": {"connectDomains": [], "resourceDomains": []}},
                        "openai/widgetDescription": "Shows extracted document pages; flagged elements are "
                                                    "highlighted by uncertainty level."})
    def page_viewer() -> str:
        return widget_html()

    @mcp.resource(PAGE_IMAGE_URI, name="scholarocr-page-image", title="ScholarOCR page image", mime_type=WIDGET_MIME,
                  description="A page waiting for transcription in host mode.",
                  meta={"ui": {"prefersBorder": True, "domain": cfg.widget_domain or cfg.public_url,
                               "csp": {"connectDomains": [], "resourceDomains": []}},
                        "openai/widgetDescription": "Shows the page image the assistant is transcribing and attaches "
                                                    "it for the assistant."})
    def page_image_widget() -> str:
        return resources.files("scholarocr.remote").joinpath("page_image.html").read_text(encoding="utf-8")

    return RemoteComponents(cfg, store, pool, limits, signer, provider, mcp)


def widget_html() -> str:
    return resources.files("scholarocr.remote").joinpath("widget.html").read_text(encoding="utf-8")


MAX_VIEW_PAGES = 5
MAX_VIEW_ELEMENTS = 150
MAX_VIEW_TEXT = 600


def page_view(svc, document_id: str, pages: str | None) -> dict[str, Any]:
    """Compact page payload for the viewer widget (structuredContent)."""
    manifest = svc.load_manifest(document_id)
    total = len(manifest.pages)
    selected = parse_page_range(pages, total) if pages else list(range(1, min(total, 3) + 1))
    if not selected:
        raise InputError("no pages selected")
    selected = selected[:MAX_VIEW_PAGES]
    doc = svc.get_document(document_id, page_numbers=selected)
    counts = {"low": 0, "medium": 0, "high": 0, "unresolved": 0}
    out_pages, n_el, truncated = [], 0, False
    for p in doc.pages:
        els = []
        for e in sorted(p.elements, key=lambda x: x.reading_order):
            counts[e.uncertainty.value] = counts.get(e.uncertainty.value, 0) + 1
            if n_el >= MAX_VIEW_ELEMENTS:
                truncated = True
                continue
            text = e.text_for_search() if e.type in (ElementType.TABLE, ElementType.LIST) else e.content
            if len(text) > MAX_VIEW_TEXT:
                text, truncated = text[:MAX_VIEW_TEXT] + "…", True
            item = {"id": e.element_id, "type": e.type.value, "text": text, "uncertainty": e.uncertainty.value}
            if e.latex:
                item["latex"] = e.latex[:MAX_VIEW_TEXT]
            if e.uncertainty_reasons:
                item["reasons"] = e.uncertainty_reasons[:3]
            els.append(item)
            n_el += 1
        entry = {"page_number": p.page_number, "status": p.status.value, "elements": els}
        if p.error:
            entry["error"] = str(p.error)[:300]
        out_pages.append(entry)
    first, last = selected[0], selected[-1]
    span = last - first + 1
    nxt = f"{last + 1}-{min(total, last + span)}" if last < total else None
    prv = f"{max(1, first - span)}-{first - 1}" if first > 1 else None
    return {"document_id": document_id, "title": doc.title, "page_count": total,
            "range": {"first": first, "last": last}, "pages": out_pages, "counts": counts,
            "next_pages": nxt, "prev_pages": prv, "truncated": truncated}


# ---------------------------------------------------------------------- HTTP app
def create_app(cfg: RemoteConfig | None = None, *, transport_factory: TransportFactory | None = None,
               fetcher: Fetcher | None = None, environ: dict[str, str] | None = None):
    """Build the Starlette app: /mcp, OAuth endpoints, /byok/connect, /download/{token}, /healthz."""
    cfg = cfg or RemoteConfig.from_env()
    cfg.root_dir.mkdir(parents=True, exist_ok=True)
    store = AuthStore(cfg.auth_db, cfg.secret)
    pool = ServicePool(cfg, store, transport_factory=transport_factory, environ=environ)
    comps = build_remote_server(cfg, store=store, pool=pool, fetcher=fetcher)
    mcp = comps.mcp

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__})

    @mcp.custom_route("/download/{token}", methods=["GET"])
    async def download(request: Request) -> Response:
        hit = comps.signer.verify(request.path_params.get("token", ""))
        if hit is None:
            return PlainTextResponse("link expired or invalid", status_code=404)
        uid, rel = hit
        try:
            base = UserSandbox(cfg, uid).data_dir
        except ScholarOCRError:
            return PlainTextResponse("not found", status_code=404)
        target = (base / rel).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(target, filename=target.name, headers={"Cache-Control": "no-store",
                                                                   "X-Content-Type-Options": "nosniff"})

    if cfg.domain_challenge:
        @mcp.custom_route("/.well-known/openai-apps-challenge", methods=["GET"])
        async def challenge(request: Request) -> Response:
            return PlainTextResponse(cfg.domain_challenge or "")

    if comps.provider is not None:
        mcp.custom_route("/byok/connect", methods=["GET", "POST"])(make_connect_routes(cfg, store, comps.provider))
        mcp.custom_route("/byok/key", methods=["GET", "POST"])(make_key_link_route(cfg, store, on_change=pool.drop))

        # The SDK serves RFC 9728 metadata at /.well-known/oauth-protected-resource/mcp and advertises it in
        # WWW-Authenticate; OpenAI's auth guide names the root path, so serve the same document there too.
        @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
        async def protected_resource_root(request: Request) -> Response:
            return JSONResponse({"resource": cfg.mcp_url, "authorization_servers": [cfg.public_url],
                                 "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"]},
                                headers={"Access-Control-Allow-Origin": "*"})

    origins = sorted(set([cfg.public_url, "http://127.0.0.1:*", "http://localhost:*"] + DEFAULT_ORIGINS
                         + cfg.allowed_origins))
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted({cfg.public_host, "127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost"}),
        allowed_origins=origins)
    app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=cfg.json_response,
                                  stateless_http=cfg.stateless_http, transport_security=security,
                                  max_request_body_size=cfg.max_request_body_bytes, host=cfg.host)
    app.state.components = comps
    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="scholarocr-remote", description=f"{APP_NAME} hosted MCP server "
                                     "(ChatGPT apps). Configuration: SCHOLAROCR_REMOTE_* variables.")
    parser.add_argument("--host", help="bind address (SCHOLAROCR_REMOTE_HOST, default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port (SCHOLAROCR_REMOTE_PORT, default 8000)")
    parser.add_argument("--public-url", help="public origin, e.g. https://ocr.example.com")
    parser.add_argument("--auth", choices=["oauth", "none"], help="oauth (default) or none (local testing only)")
    args = parser.parse_args(argv)
    cfg = RemoteConfig.from_env()
    updates = {k: v for k, v in (("host", args.host), ("port", args.port), ("public_url", args.public_url),
                                 ("auth", args.auth)) if v is not None}
    if updates:
        cfg = RemoteConfig.model_validate({**cfg.model_dump(), **updates})
    print_warnings(cfg)
    import uvicorn

    from .retention import Janitor

    app = create_app(cfg)
    comps: RemoteComponents = app.state.components
    janitor = Janitor(comps.pool, comps.store)
    try:
        janitor.sweep()
    except Exception as e:  # pragma: no cover
        print(f"warning: initial retention sweep failed: {e.__class__.__name__}")
    janitor.start(cfg.sweep_interval_s)
    try:
        # proxy_headers trusts X-Forwarded-* only from FORWARDED_ALLOW_IPS (uvicorn default: 127.0.0.1).
        uvicorn.run(app, host=cfg.host, port=cfg.port, proxy_headers=True)
    finally:
        janitor.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
