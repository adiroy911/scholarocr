"""CLI tests via real subprocess invocations (exit codes, JSON output)."""

import json
import os
import subprocess
import sys

import pytest


@pytest.fixture
def run(settings, corpus, tmp_path):
    # Run outside the repo: the CLI reads .env from its working directory and treats an empty
    # GEMINI_API_KEY as unset, so a developer's real key would otherwise be used (a live API call).
    env = {**os.environ, "SCHOLAROCR_DATA_DIR": str(settings.data_dir),
           "SCHOLAROCR_ALLOWED_DIRS": str(corpus["clean_digital.pdf"].parent), "GEMINI_API_KEY": "",
           "GOOGLE_API_KEY": ""}

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
    code, _, err = run("process", str(corpus["scanned_poor.pdf"]))
    assert code == 6 and "GEMINI_API_KEY" in (err + _)
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


def test_cli_info_redacts_secrets(settings, corpus):
    env = {**os.environ, "SCHOLAROCR_DATA_DIR": str(settings.data_dir), "GEMINI_API_KEY": "sk-very-secret"}
    p = subprocess.run([sys.executable, "-m", "scholarocr.cli.main", "info"], capture_output=True, text=True, env=env)
    assert p.returncode == 0 and "sk-very-secret" not in p.stdout and '"gemini_available": true' in p.stdout
