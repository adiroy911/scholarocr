"""Run Antigravity CLI (``agy -p``) headless, one call per page or chat turn.

Each call gets its own run folder with a ``--settings`` file whose only allow rules are ``read_file(<dir>)``
for the folders given (the page image folder, or none for chat). Headless mode cannot ask for permission, so
every other tool call (shell commands, edits, browsing, MCP tools) is refused by agy. The user's
``~/.gemini`` settings are never touched: ``--settings`` applies to that one session only.

Output is ``--output-format stream-json``: one JSON event per line; text deltas are forwarded to
``on_delta`` and the final ``result`` event carries the response, ``structured_output`` (with
``--json-schema``), ``usage`` and ``duration_seconds``.

Reader agent (measured with agy 1.4.12): a page read with agy's default agent costs three model calls (the
``read_file`` call, a text reply, then agy's own request to return the schema output), each carrying agy's
~12k-token system prompt and tool list. A run that reads an image therefore writes a workspace agent file
(``<run>/.agents/agents/scholarocr-reader.md``) with only ``read_file`` (and ``finish`` when a schema is set)
and selects it with ``--agent``: the model reads the image and returns the JSON through ``finish`` in two calls
with a much shorter system prompt (a 1-word probe image: 39.1k -> 16.8k input tokens). An agy that does not
know the agent falls back to the default agent for the rest of the process.

An image cannot be attached to the first message instead: ``--input-format stream-json`` accepts only ``text``
content blocks (``image``, ``image_url``, ``inline_data`` and ``file`` blocks end the run with "content block
type ... is not supported"), and ``@path`` in a ``-p`` prompt passes the path, not the image.

Effort: agy selects reasoning effort through the model variant. ``--model gemini-3.8-flash-medium --effort low``
is refused as a conflict, ``--model gemini-3.8-flash --effort low`` resolves to ``gemini-3.8-flash-low``, and
models without variants (``claude-*``) refuse ``--effort``. ``model_for_effort`` picks the arguments.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..exceptions import ScholarOCRError
from ..logging_config import get_logger

log = get_logger("webapp.agy")
MODEL_KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY")
MAX_PROMPT_CHARS = 28000  # Windows limits a command line to 32767 characters
USAGE_KEYS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
_EFFORT_SUFFIX = re.compile(r"^(?P<family>.+)-(?P<effort>low|medium|high|xhigh|max)$")
READER_AGENT = "scholarocr-reader"
AGENT_REFUSED = re.compile(r"unknown agent|agent\b.{0,60}\b(not found|not available|unsupported|invalid)|"
                           r"custom agents?\b.{0,40}\b(unavailable|disabled|not supported)", re.I)
# What agy (or the model API behind it) reports when it is out of quota or throttling: agy retries per-minute 429s
# itself and surfaces the ones it cannot recover ("RESOURCE_EXHAUSTED", "individual quota reached", 503s).
RATE_LIMIT = re.compile(r"\b429\b|\b503\b|RESOURCE_EXHAUSTED|rate[ -]?limit|quota|too many requests|capacity|"
                        r"overloaded|UNAVAILABLE|exhausted", re.I)
READER_AGENT_BODY = """You are the vision model of the ScholarOCR desktop app. Read the image file or files named in the \
message with read_file (your only file tool), then answer exactly as the message asks.{finish} Never try any other \
tool: everything else is denied in this run."""
READER_FINISH = (" Return the JSON answer by calling the finish tool with it as the structured output; do not "
                 "write the JSON as a text reply first.")


class AgyError(ScholarOCRError):
    code = "agy_error"


class AgyTimeout(AgyError):
    code = "agy_timeout"


class AgyCancelled(AgyError):
    code = "cancelled"


class AgyRateLimited(AgyError):
    """agy reported throttling or an exhausted quota (or the model was unavailable)."""

    code = "agy_rate_limited"


def is_rate_limited(text: str) -> bool:
    return bool(RATE_LIMIT.search(text or ""))


def model_for_effort(model: str | None, effort: str | None, available: Iterable[str] = ()
                     ) -> tuple[str | None, str | None]:
    """``(--model, --effort)`` for a chosen model and effort. agy refuses an effort that conflicts with a model
    variant (``gemini-3.8-flash-medium`` + ``low``), so the family name is passed with ``--effort`` when the
    variant for that effort is listed by ``agy models``; otherwise the model is used as chosen, without
    ``--effort`` (models without effort variants, or an effort the model does not offer)."""
    if not model or not effort or effort not in EFFORTS:
        return model, None
    ids = set(available)
    m = _EFFORT_SUFFIX.match(model)
    family = m.group("family") if m else model
    if f"{family}-{effort}" in ids:
        return family, effort
    return model, None


@dataclass
class AgyResult:
    response: str
    structured: Any | None
    usage: dict[str, int]
    duration_s: float | None  # as reported by agy
    wall_s: float
    conversation_id: str | None
    tools_used: list[str] = field(default_factory=list)


def find_agy(explicit: str | None = None, which: Callable[[str], str | None] = shutil.which) -> str | None:
    if explicit:
        return explicit if Path(explicit).is_file() else which(explicit)
    return which("agy")


def rule_path(p: Path) -> str:
    """Paths inside allow rules use forward slashes (agy accepts them on Windows)."""
    return str(p.resolve()).replace("\\", "/")


def settings_document(read_dirs: list[Path]) -> dict[str, Any]:
    return {"permissions": {"allow": [f"read_file({rule_path(d)})" for d in read_dirs]}}


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def parse_structured(text: str) -> Any | None:
    """The first JSON object in a model reply (code fences and trailing text are tolerated)."""
    s = (text or "").strip()
    start = s.find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[start:])
        return obj
    except ValueError:
        return None


class AgyRunner:
    # Shared by every runner in the process: once agy says it does not know the reader agent, stop asking.
    reader_agent_ok = True

    def __init__(self, agy_path: str, runs_dir: Path, *, popen: Callable[..., Any] = subprocess.Popen,
                 kill: Callable[[Any], None] = _kill_tree, clock: Callable[[], float] = time.monotonic,
                 use_reader_agent: bool = True):
        self.agy_path = agy_path
        self.runs_dir = runs_dir
        self._popen = popen
        self._kill = kill
        self._clock = clock
        self.use_reader_agent = use_reader_agent

    def build_command(self, prompt: str, *, model: str | None, settings_file: Path, schema_file: Path | None,
                      timeout_s: float, conversation_id: str | None = None, effort: str | None = None,
                      agent: str | None = None) -> list[str]:
        cmd = [self.agy_path, "-p", prompt, "--output-format", "stream-json", "--settings", str(settings_file),
               "--print-timeout", f"{int(max(1, timeout_s))}s", "--disable-slash-commands"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if agent:
            cmd += ["--agent", agent]
        if schema_file is not None:
            cmd += ["--json-schema", str(schema_file)]
        if conversation_id:
            cmd += ["--conversation", conversation_id]
        return cmd

    @staticmethod
    def write_reader_agent(run_dir: Path, *, schema: bool) -> str:
        """The workspace agent for reading images (see the module docstring). Returns its name."""
        tools = "  - read_file\n" + ("  - finish\n" if schema else "")
        body = READER_AGENT_BODY.format(finish=READER_FINISH if schema else "")
        doc = (f"---\nname: {READER_AGENT}\ndescription: Reads images and answers as asked.\ntools:\n{tools}"
               f"mainAgent: true\nsubagent: false\nmodel: inherit\n---\n{body}\n")
        path = run_dir / ".agents" / "agents" / f"{READER_AGENT}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc, encoding="utf-8")
        return READER_AGENT

    @staticmethod
    def environment() -> dict[str, str]:
        """The user's environment without model API keys: agy must use the signed-in account."""
        return {k: v for k, v in os.environ.items() if k not in MODEL_KEY_VARS}

    def run(self, prompt: str, *, run_dir: Path, model: str | None, read_dirs: list[Path] | None = None,
            schema: dict[str, Any] | None = None, timeout_s: float = 300.0, cancel: threading.Event | None = None,
            on_delta: Callable[[str], None] | None = None, on_event: Callable[[dict], None] | None = None,
            conversation_id: str | None = None, effort: str | None = None) -> AgyResult:
        """One headless turn. Calls that may read an image (``read_dirs``) use the reader agent."""
        agent = None
        if read_dirs and self.use_reader_agent and AgyRunner.reader_agent_ok:
            run_dir.mkdir(parents=True, exist_ok=True)
            agent = self.write_reader_agent(run_dir, schema=schema is not None)
        try:
            return self._run(prompt, run_dir=run_dir, model=model, read_dirs=read_dirs, schema=schema,
                             timeout_s=timeout_s, cancel=cancel, on_delta=on_delta, on_event=on_event,
                             conversation_id=conversation_id, effort=effort, agent=agent)
        except AgyError as e:
            if agent is None or isinstance(e, (AgyCancelled, AgyTimeout)) or not AGENT_REFUSED.search(e.message):
                raise
            # an agy without workspace agents (or with a changed agent format): use its default agent from now on
            log.warning("agy refused the reader agent (%s); using its default agent", e.message[:200])
            AgyRunner.reader_agent_ok = False
            return self._run(prompt, run_dir=run_dir, model=model, read_dirs=read_dirs, schema=schema,
                             timeout_s=timeout_s, cancel=cancel, on_delta=on_delta, on_event=on_event,
                             conversation_id=conversation_id, effort=effort, agent=None)

    def _run(self, prompt: str, *, run_dir: Path, model: str | None, read_dirs: list[Path] | None,
             schema: dict[str, Any] | None, timeout_s: float, cancel: threading.Event | None,
             on_delta: Callable[[str], None] | None, on_event: Callable[[dict], None] | None,
             conversation_id: str | None, effort: str | None, agent: str | None) -> AgyResult:
        if len(prompt) > MAX_PROMPT_CHARS:
            raise AgyError(f"prompt is {len(prompt)} characters; the limit is {MAX_PROMPT_CHARS}")
        run_dir.mkdir(parents=True, exist_ok=True)
        settings_file = run_dir / "agy-settings.json"
        settings_file.write_text(json.dumps(settings_document(read_dirs or []), indent=1), encoding="utf-8")
        schema_file = None
        if schema is not None:
            schema_file = run_dir / "page-schema.json"
            schema_file.write_text(json.dumps(schema), encoding="utf-8")
        # agy's own limit, a little later than ours so our timeout wins and reports clearly.
        cmd = self.build_command(prompt, model=model, settings_file=settings_file, schema_file=schema_file,
                                 timeout_s=timeout_s + 15, conversation_id=conversation_id, effort=effort,
                                 agent=agent)
        kwargs: dict[str, Any] = {"cwd": str(run_dir), "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                                  "stderr": subprocess.PIPE, "env": self.environment()}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        started = self._clock()
        try:
            proc = self._popen(cmd, **kwargs)
        except OSError as e:
            raise AgyError(f"could not start agy: {e.__class__.__name__}") from None

        lines: queue.Queue = queue.Queue()
        err_chunks: list[bytes] = []

        def pump_out():
            try:
                for raw in iter(proc.stdout.readline, b""):
                    lines.put(raw)
            finally:
                lines.put(None)

        def pump_err():
            try:
                for raw in iter(proc.stderr.readline, b""):
                    if sum(len(c) for c in err_chunks) < 20000:
                        err_chunks.append(raw)
            except Exception:
                pass

        threading.Thread(target=pump_out, daemon=True).start()
        threading.Thread(target=pump_err, daemon=True).start()
        result: dict[str, Any] | None = None
        conv: str | None = None
        tools: list[str] = []
        deadline = started + timeout_s
        while True:
            if cancel is not None and cancel.is_set():
                self._kill(proc)
                raise AgyCancelled("cancelled")
            if self._clock() > deadline:
                self._kill(proc)
                raise AgyTimeout(f"agy did not finish within {int(timeout_s)} s")
            try:
                raw = lines.get(timeout=0.25)
            except queue.Empty:
                continue
            if raw is None:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("event")
            if kind == "result":
                result = ev.get("result") or {}
            elif kind == "step_update":
                step = ev.get("step_update") or {}
                conv = conv or step.get("conversation_id")
                if step.get("step_type") == "agent_response" and step.get("text_delta") and on_delta:
                    on_delta(step["text_delta"])
                stype = step.get("step_type") or ""
                if stype not in ("user_input", "agent_response") and step.get("state") == "ACTIVE" \
                        and stype not in tools:
                    tools.append(stype)
                if on_event:
                    on_event(step)
            elif kind == "init":
                conv = conv or (ev.get("init") or {}).get("conversation_id")
        try:
            proc.wait(timeout=10)
        except Exception:
            self._kill(proc)
        wall = self._clock() - started
        stderr = b"".join(err_chunks).decode("utf-8", "replace").strip()
        if result is None:
            tail = stderr.splitlines()[-1][:300] if stderr else "no result"
            err = AgyRateLimited if is_rate_limited(stderr[-2000:]) else AgyError
            raise err(f"agy exited without a result ({tail})")
        if (result.get("status") or "").upper() != "SUCCESS":
            detail = str(result.get("error") or stderr or "no details")
            err = AgyRateLimited if is_rate_limited(detail[:2000]) else AgyError
            raise err(f"agy reported {result.get('status') or 'an error'}: {detail[:400]}")
        usage = {k: int((result.get("usage") or {}).get(k) or 0) for k in USAGE_KEYS}
        response = result.get("response") or ""
        structured = result.get("structured_output")
        if structured is None and schema is not None:
            structured = parse_structured(response)
        return AgyResult(response=response, structured=structured, usage=usage,
                         duration_s=result.get("duration_seconds"), wall_s=round(wall, 2),
                         conversation_id=result.get("conversation_id") or conv, tools_used=tools)


def list_models(agy_path: str, *, timeout_s: float = 45.0, run: Callable[..., Any] = subprocess.run
                ) -> list[dict[str, str]]:
    """``agy models`` -> [{"id", "label"}]. Raises AgyError when agy fails (not signed in, offline)."""
    kwargs: dict[str, Any] = {"capture_output": True, "timeout": timeout_s, "stdin": subprocess.DEVNULL,
                              "env": AgyRunner.environment()}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = run([agy_path, "models"], **kwargs)
    except subprocess.TimeoutExpired:
        raise AgyTimeout("`agy models` timed out") from None
    except OSError as e:
        raise AgyError(f"could not start agy: {e.__class__.__name__}") from None
    out = (proc.stdout or b"").decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else (proc.stdout or "")
    models = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        mid, _, label = line.partition("\t")
        mid = mid.strip()
        if mid and " " not in mid:
            models.append({"id": mid, "label": label.strip() or mid})
    if proc.returncode != 0 or not models:
        err = (proc.stderr or b"")
        err = err.decode("utf-8", "replace") if isinstance(err, bytes) else err
        raise AgyError("`agy models` failed: " + (err.strip().splitlines()[-1][:200] if err.strip() else
                                                  "no models listed; sign in by running agy once"))
    return models
