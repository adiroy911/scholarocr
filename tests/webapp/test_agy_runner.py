"""AgyRunner with the subprocess replaced: command line, settings file, schema, parsing, timeout, cancel."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from scholarocr.webapp.agy import (AgyCancelled, AgyError, AgyRunner, AgyTimeout, list_models, parse_structured,
                                   settings_document)


class FakeStream:
    """A pipe: yields the given lines, then blocks until closed (or ends at once with ``end=True``)."""

    def __init__(self, lines: list[bytes], end: bool = True):
        self.lines = list(lines)
        self.end = end
        self.closed = threading.Event()
        if end:
            self.closed.set()

    def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        self.closed.wait(10)
        return b""


class FakeProc:
    def __init__(self, stdout_lines: list[bytes], *, end: bool = True, stderr: bytes = b""):
        self.stdout = FakeStream(stdout_lines, end)
        self.stderr = FakeStream([stderr] if stderr else [])
        self.pid = 4242
        self.killed = False
        self.returncode = None if not end else 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self.returncode if self.returncode is not None else -9
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.closed.set()


class FakePopen:
    def __init__(self, proc: FakeProc):
        self.proc = proc
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return self.proc


def ev(kind: str, body: dict) -> bytes:
    return (json.dumps({"event": kind, kind if kind != "step_update" else "step_update": body}) + "\n").encode()


def result_line(**over) -> bytes:
    res = {"conversation_id": "conv-1", "status": "SUCCESS", "response": "{\"elements\": []}",
           "duration_seconds": 12.5, "usage": {"input_tokens": 20000, "output_tokens": 300, "thinking_tokens": 100,
                                              "cache_read_tokens": 0, "total_tokens": 20300}}
    res.update(over)
    return (json.dumps({"event": "result", "result": res}) + "\n").encode()


def runner(tmp_path: Path, proc: FakeProc) -> tuple[AgyRunner, FakePopen]:
    popen = FakePopen(proc)
    return AgyRunner("C:/tools/agy.exe", tmp_path / "runs", popen=popen, kill=lambda p: p.kill()), popen


def test_command_line_settings_and_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "should-not-reach-agy")
    proc = FakeProc([ev("init", {"conversation_id": "conv-1"}), result_line(structured_output={"elements": []})])
    r, popen = runner(tmp_path, proc)
    img_dir = tmp_path / "run 1" / "page"
    img_dir.mkdir(parents=True)
    out = r.run("PROMPT", run_dir=tmp_path / "run 1", model="gemini-3.8-flash-low", read_dirs=[img_dir],
                schema={"type": "object"}, timeout_s=60)
    cmd, kwargs = popen.calls[0]
    assert cmd[:3] == ["C:/tools/agy.exe", "-p", "PROMPT"]
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("--model") + 1] == "gemini-3.8-flash-low"
    assert "--disable-slash-commands" in cmd
    assert cmd[cmd.index("--print-timeout") + 1] == "75s"  # agy's own limit sits just past ours
    assert "--mode" not in cmd and "--dangerously-skip-permissions" not in cmd
    settings = json.loads(Path(cmd[cmd.index("--settings") + 1]).read_text())
    allow = settings["permissions"]["allow"]
    assert allow == [f"read_file({str(img_dir.resolve()).replace(chr(92), '/')})"]  # nothing but reading the page dir
    assert json.loads(Path(cmd[cmd.index("--json-schema") + 1]).read_text()) == {"type": "object"}
    assert kwargs["cwd"] == str(tmp_path / "run 1")
    assert "GEMINI_API_KEY" not in kwargs["env"]  # agy must use the signed-in account
    assert out.structured == {"elements": []}
    assert out.usage["total_tokens"] == 20300 and out.usage["input_tokens"] == 20000
    assert out.duration_s == 12.5 and out.conversation_id == "conv-1"


def test_chat_call_has_no_schema_and_no_read_rules(tmp_path):
    deltas = []
    lines = [ev("step_update", {"conversation_id": "c", "step_type": "agent_response", "state": "ACTIVE", "text_delta": "Hel"}),
             ev("step_update", {"conversation_id": "c", "step_type": "agent_response", "state": "DONE", "text_delta": "lo"}),
             result_line(response="Hello")]
    r, popen = runner(tmp_path, FakeProc(lines))
    out = r.run("Q", run_dir=tmp_path / "chat", model=None, read_dirs=[], schema=None, on_delta=deltas.append)
    cmd = popen.calls[0][0]
    assert "--json-schema" not in cmd and "--model" not in cmd
    assert json.loads(Path(cmd[cmd.index("--settings") + 1]).read_text()) == {"permissions": {"allow": []}}
    assert deltas == ["Hel", "lo"] and out.response == "Hello"


def test_conversation_resume_flag(tmp_path):
    r, popen = runner(tmp_path, FakeProc([result_line()]))
    r.run("fix it", run_dir=tmp_path / "r", model="m", read_dirs=[], schema={"type": "object"}, conversation_id="conv-9")
    cmd = popen.calls[0][0]
    assert cmd[cmd.index("--conversation") + 1] == "conv-9"


def test_structured_output_falls_back_to_response_text(tmp_path):
    reply = 'Here you go:\n```json\n{"elements": [{"type": "text", "text": "a"}]}\n```\n{"elements": []}'
    r, _ = runner(tmp_path, FakeProc([result_line(response=reply)]))
    out = r.run("p", run_dir=tmp_path / "r", model="m", schema={"type": "object"})
    assert out.structured == {"elements": [{"type": "text", "text": "a"}]}
    assert parse_structured("no json here") is None


def test_error_status_raises(tmp_path):
    r, _ = runner(tmp_path, FakeProc([result_line(status="ERROR", error="a tool required the permission")]))
    with pytest.raises(AgyError, match="permission"):
        r.run("p", run_dir=tmp_path / "r", model="m")


def test_no_result_raises_with_stderr_tail(tmp_path):
    r, _ = runner(tmp_path, FakeProc([b"not json\n"], stderr=b"boom: not signed in\n"))
    with pytest.raises(AgyError, match="not signed in"):
        r.run("p", run_dir=tmp_path / "r", model="m")


def test_timeout_kills_the_process(tmp_path):
    proc = FakeProc([], end=False)
    r, _ = runner(tmp_path, proc)
    with pytest.raises(AgyTimeout):
        r.run("p", run_dir=tmp_path / "r", model="m", timeout_s=0.4)
    assert proc.killed


def test_cancel_kills_the_process(tmp_path):
    proc = FakeProc([], end=False)
    r, _ = runner(tmp_path, proc)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    with pytest.raises(AgyCancelled):
        r.run("p", run_dir=tmp_path / "r", model="m", timeout_s=30, cancel=cancel)
    assert proc.killed


def test_prompt_length_limit(tmp_path):
    r, popen = runner(tmp_path, FakeProc([result_line()]))
    with pytest.raises(AgyError, match="limit"):
        r.run("x" * 40000, run_dir=tmp_path / "r", model="m")
    assert popen.calls == []


def test_settings_document_uses_forward_slashes(tmp_path):
    doc = settings_document([tmp_path / "a b"])
    assert "\\" not in doc["permissions"]["allow"][0]


def test_list_models_parses_and_fails_clearly():
    class P:
        def __init__(self, out, code=0, err=b""):
            self.stdout, self.returncode, self.stderr = out, code, err

    ok = list_models("agy", run=lambda *a, **k: P(b"Fetching available models...\ngemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
                                                    b"gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"))
    assert [m["id"] for m in ok] == ["gemini-3.8-flash-high", "gemini-3.8-flash-low"]
    with pytest.raises(AgyError, match="sign in"):
        list_models("agy", run=lambda *a, **k: P(b"", 1))


def test_page_prompt_and_schema_use_page_doubt():
    # 0.3.0 regression: "legibility: high" was read as "very legible" and raised every element to "some doubt".
    from scholarocr.intelligence.responses import PageExtractionResponse
    from scholarocr.intelligence.schema_utils import to_gemini_schema
    from scholarocr.webapp import prompts as app_prompts

    from scholarocr.schemas import Route

    task = {"route": Route.VISUAL_HANDWRITING, "hints": {"context_hint": "Class 11 physics"}, "text_layer": None}
    text = app_prompts.page_prompt("C:/x/page.png", app_prompts.page_brief(task))
    assert "page_doubt" in text and "legibility" not in text and "DOUBT" in text
    assert "Class 11 physics" in text and "handwritten" in text  # caller hints and the page profile
    # compact: the schema already defines the fields, so the MCP call shape and type list are not repeated
    assert "submit_page_content" not in text and "bibliography_entry" not in text
    assert len(app_prompts.page_prompt("C:/x/page.png", app_prompts.PAGE_RULES)) < 3800
    props = to_gemini_schema(PageExtractionResponse)["properties"]
    assert "page_doubt" in props and "legibility" not in props


# ---------------------------------------------------------------------- speed: effort, reader agent, throttling
def test_effort_goes_through_the_model_family():
    from scholarocr.webapp.agy import model_for_effort

    ids = ["gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low", "claude-sonnet-4-6"]
    # agy refuses "--model gemini-3.8-flash-medium --effort low" as a conflict; the family + --effort resolves
    assert model_for_effort("gemini-3.8-flash-medium", "low", ids) == ("gemini-3.8-flash", "low")
    assert model_for_effort("gemini-3.8-flash-low", "medium", ids) == ("gemini-3.8-flash", "medium")
    # no such variant, or a model without effort variants: the model as chosen, no --effort
    assert model_for_effort("gemini-3.8-flash-medium", "max", ids) == ("gemini-3.8-flash-medium", None)
    assert model_for_effort("claude-sonnet-4-6", "low", ids) == ("claude-sonnet-4-6", None)
    assert model_for_effort(None, "low", ids) == (None, None)
    assert model_for_effort("gemini-3.8-flash-medium", None, ids) == ("gemini-3.8-flash-medium", None)


def test_page_read_uses_the_reader_agent_and_effort(tmp_path):
    from scholarocr.webapp.agy import READER_AGENT

    AgyRunner.reader_agent_ok = True
    r, popen = runner(tmp_path, FakeProc([result_line(structured_output={"elements": []})]))
    img_dir = tmp_path / "run" / "page"
    img_dir.mkdir(parents=True)
    r.run("P", run_dir=tmp_path / "run", model="gemini-3.8-flash", read_dirs=[img_dir], schema={"type": "object"},
          effort="low")
    cmd = popen.calls[0][0]
    assert cmd[cmd.index("--effort") + 1] == "low" and cmd[cmd.index("--agent") + 1] == READER_AGENT
    agent = (tmp_path / "run" / ".agents" / "agents" / f"{READER_AGENT}.md").read_text(encoding="utf-8")
    assert "  - read_file\n  - finish\n" in agent and "mainAgent: true" in agent and "finish tool" in agent
    # chat (nothing to read) keeps agy's default agent
    r2, popen2 = runner(tmp_path, FakeProc([result_line(response="hi")]))
    r2.run("Q", run_dir=tmp_path / "chat", model="m", read_dirs=[], schema=None)
    assert "--agent" not in popen2.calls[0][0] and "--effort" not in popen2.calls[0][0]


def test_unknown_reader_agent_falls_back_to_the_default_agent(tmp_path):
    AgyRunner.reader_agent_ok = True
    refused = FakeProc([result_line(status="ERROR", error='unknown agent "scholarocr-reader"')])
    ok = FakeProc([result_line(structured_output={"elements": []})])
    procs = [refused, ok]
    calls = []

    def popen(cmd, **kw):
        calls.append(cmd)
        return procs.pop(0)

    r = AgyRunner("agy", tmp_path / "runs", popen=popen, kill=lambda p: p.kill())
    img_dir = tmp_path / "run" / "page"
    img_dir.mkdir(parents=True)
    try:
        out = r.run("P", run_dir=tmp_path / "run", model="m", read_dirs=[img_dir], schema={"type": "object"})
        assert out.structured == {"elements": []}
        assert "--agent" in calls[0] and "--agent" not in calls[1]
        assert AgyRunner.reader_agent_ok is False  # not tried again in this process
    finally:
        AgyRunner.reader_agent_ok = True


def test_rate_limits_are_recognised(tmp_path):
    from scholarocr.webapp.agy import AgyRateLimited, is_rate_limited

    assert is_rate_limited("googleapi: Error 429: RESOURCE_EXHAUSTED") and is_rate_limited("individual quota reached")
    assert is_rate_limited("503 UNAVAILABLE: the model is overloaded")
    assert not is_rate_limited("a tool required the permission")
    r, _ = runner(tmp_path, FakeProc([result_line(status="ERROR", error="Error 429: RESOURCE_EXHAUSTED")]))
    with pytest.raises(AgyRateLimited):
        r.run("p", run_dir=tmp_path / "r", model="m")
    r, _ = runner(tmp_path, FakeProc([b"x\n"], stderr=b"error: individual quota reached, resets at 14:00\n"))
    with pytest.raises(AgyRateLimited):
        r.run("p", run_dir=tmp_path / "r2", model="m")
