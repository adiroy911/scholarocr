"""CLI tests via real subprocess invocations (exit codes, JSON output)."""

import json
import os
import subprocess
import sys

import pytest


MODEL_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "SCHOLAROCR_PROVIDER")


def clean_env(**extra) -> dict:
    """The inherited environment without any model key or provider choice (host mode)."""
    env = {k: v for k, v in os.environ.items() if k not in MODEL_ENV}
    env.update(extra)
    return env


@pytest.fixture
def run(settings, corpus, tmp_path):
    # Run outside the repo: the CLI reads .env from its working directory and treats an empty variable as
    # unset, so a developer's real key would otherwise be used (a live API call). Every model key is removed
    # (OPENROUTER_API_KEY alone would select OpenRouter under SCHOLAROCR_PROVIDER=auto) and host mode pinned.
    env = clean_env(SCHOLAROCR_DATA_DIR=str(settings.data_dir), SCHOLAROCR_PROVIDER="host",
                    SCHOLAROCR_ALLOWED_DIRS=str(corpus["clean_digital.pdf"].parent))

    def _run(*args):
        p = subprocess.run([sys.executable, "-m", "scholarocr.cli.main", *args], capture_output=True, text=True,
                           env=env, timeout=300, cwd=str(tmp_path))
        return p.returncode, p.stdout, p.stderr

    return _run


def test_cli_full_workflow(run, corpus, tmp_path):
    code, out, err = run("--json", "process", str(corpus["clean_digital.pdf"]))
    assert code == 0, err
    data = json.loads(out)
    doc = data["document_id"]
    assert data["status"] == "completed" and data["summary"]["routes"] == {"embedded_text": 2}

    code, out, _ = run("content", doc, "--format", "markdown")
    assert code == 0 and "# Introduction to Algebra" in out

    code, out, _ = run("--json", "page", doc, "1")
    assert code == 0 and json.loads(out)["page_number"] == 1

    code, out, _ = run("--json", "list")
    assert json.loads(out)["total"] == 1

    code, out, _ = run("verify", doc)
    assert code == 0 and "PASSED" in out

    for fmt in ("json", "markdown", "searchable-pdf", "docx"):
        code, out, err = run("--json", "export", doc, "--format", fmt)
        assert code == 0, err
        assert os.path.exists(json.loads(out)["primary_file"])

    region = tmp_path / "r.png"
    code, out, err = run("region", doc, "--page", "1", "--bbox", "60,70,300,100", "--out", str(region))
    assert code == 0 and region.read_bytes()[:4] == b"\x89PNG"

    code, out, _ = run("--json", "status", doc)
    assert code == 0 and json.loads(out)["status"] == "processed"


def test_cli_exit_codes(run, corpus, tmp_path):
    code, out, err = run("process", str(corpus["scanned_poor.pdf"]))
    assert code == 6 and "GEMINI_API_KEY" in (err + out) and "awaiting" in out  # host mode: needs a model
    code, _, err = run("process", str(corpus["encrypted.pdf"]))
    assert code == 5 and "password" in err
    outside = tmp_path / "x.pdf"
    outside.write_bytes(b"%PDF-1.4")
    code, _, err = run("process", str(outside))
    assert code == 4 and "Access denied" in err
    code, _, _ = run("status", "doc_ffffffffffffffff")
    assert code == 3
    code, _, _ = run("export", "doc_ffffffffffffffff", "--format", "pptx")
    assert code in (3, 1)
    code, _, _ = run("bogus-command")
    assert code == 2


def test_cli_info_redacts_secrets(settings, corpus, tmp_path):
    # ``info`` makes no model call; the fake key only checks redaction and provider selection. A key alone
    # never selects a provider (unset = host), so ask for gemini explicitly.
    env = clean_env(SCHOLAROCR_DATA_DIR=str(settings.data_dir), GEMINI_API_KEY="sk-very-secret",
                    SCHOLAROCR_PROVIDER="gemini")
    p = subprocess.run([sys.executable, "-m", "scholarocr.cli.main", "info"], capture_output=True, text=True, env=env,
                       cwd=str(tmp_path))
    assert p.returncode == 0 and "sk-very-secret" not in p.stdout and '"gemini_available": true' in p.stdout
    assert '"mode": "gemini"' in p.stdout


def test_cli_without_keys_is_host_mode(settings, corpus, tmp_path):
    env = clean_env(SCHOLAROCR_DATA_DIR=str(settings.data_dir))
    p = subprocess.run([sys.executable, "-m", "scholarocr.cli.main", "info"], capture_output=True, text=True, env=env,
                       cwd=str(tmp_path))
    assert p.returncode == 0 and '"mode": "host"' in p.stdout and '"gemini_available": false' in p.stdout
