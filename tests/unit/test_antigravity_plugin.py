"""The repository root is the Antigravity plugin root; keep it consistent and free of other agents' files."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import scholarocr

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SKILLS = {"ocr", "ocr-verify", "ocr-export"}


def manifest() -> dict:
    return json.loads((ROOT / ".antigravity-plugin" / "plugin.json").read_text(encoding="utf-8"))


def test_versions_agree():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert project == scholarocr.__version__ == manifest()["version"]


def test_manifest_fields_and_logo_at_plugin_root():
    m = manifest()
    assert m["name"] == "scholarocr"
    for key in ("displayName", "description", "version", "logo", "author"):
        assert m.get(key), key
    # Paths resolve from the plugin root (the repo root), not from .antigravity-plugin/.
    assert not m["logo"].startswith(".") and (ROOT / m["logo"]).is_file()
    assert not (ROOT / "plugin.json").exists(), "a root plugin.json would compete with .antigravity-plugin/"


def test_root_skills_are_only_the_plugin_skills():
    # Every folder in skills/ becomes an Antigravity skill, so nothing else may live there.
    found = {p.name for p in (ROOT / "skills").iterdir() if p.is_dir()}
    assert found == PLUGIN_SKILLS
    for name in PLUGIN_SKILLS:
        text = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert f"name: {name}\n" in text
    assert (ROOT / "claude-skill" / "scholarocr" / "SKILL.md").is_file()


def test_mcp_config_declares_no_variables():
    # Antigravity keeps a plugin disabled until declared variables are filled in; host mode needs none.
    cfg = json.loads((ROOT / "mcp_config.json").read_text(encoding="utf-8"))
    server = cfg["mcpServers"]["scholarocr"]
    assert server["command"] == "scholarocr-mcp"
    assert "env" not in server and "variables" not in server
    assert (ROOT / "rules" / "AGENTS.md").is_file()
    for component in ("agents", "bin", "hooks.json"):
        assert not (ROOT / component).exists(), component


def test_stage_script_copies_only_plugin_files(tmp_path):
    out = tmp_path / "staged"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "stage_antigravity_plugin.py"), "--out", str(out)],
                   check=True, capture_output=True, text=True)
    top = {p.name for p in out.iterdir()}
    assert top == {".antigravity-plugin", "mcp_config.json", "rules", "skills", "assets", "LICENSE",
                   ".scholarocr-staged"}
    assert {p.name for p in (out / "skills").iterdir()} == PLUGIN_SKILLS
    # Re-staging over its own output works; a foreign non-empty folder is refused.
    subprocess.run([sys.executable, str(ROOT / "scripts" / "stage_antigravity_plugin.py"), "--out", str(out)],
                   check=True, capture_output=True, text=True)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("x", encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "stage_antigravity_plugin.py"), "--out", str(foreign)],
                       capture_output=True, text=True)
    assert r.returncode != 0 and (foreign / "keep.txt").exists()
