"""Copy only the Antigravity plugin files of this checkout into a clean folder.

The repository root is the plugin root (``.antigravity-plugin/plugin.json``, ``mcp_config.json``,
``rules/``, ``skills/``, ``assets/``), so ``agy plugin install https://github.com/adiroy911/scholarocr``
works. ``agy plugin install <local folder>`` copies the whole folder, including ``.venv/``, ``.git/``
and a local ``.env`` that may hold keys, so do not point it at a working checkout. Stage first:

    python scripts/stage_antigravity_plugin.py                 # -> <checkout>/build/antigravity-plugin
    python scripts/stage_antigravity_plugin.py --out DIR       # any empty or previously staged folder
    agy plugin validate --strict <staged folder>
    agy plugin install <staged folder>

The staged folder holds no Python code: the plugin runs ``scholarocr-mcp`` from PATH
(``uv tool install`` the package separately).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Everything Antigravity loads from the plugin root, plus the licence.
PLUGIN_PATHS = [".antigravity-plugin", "mcp_config.json", "rules", "skills", "assets", "LICENSE"]
MARKER = ".scholarocr-staged"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "antigravity-plugin")
    args = ap.parse_args()
    out = args.out.expanduser().resolve()
    if out == ROOT or out in ROOT.parents:
        sys.exit("--out must be a separate folder, not the checkout or one of its parents")
    if out.exists():
        if not (out / MARKER).exists() and any(out.iterdir()):
            sys.exit(f"{out} exists, is not empty and was not created by this script; choose another --out")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for rel in PLUGIN_PATHS:
        src = ROOT / rel
        if not src.exists():
            sys.exit(f"missing plugin file: {src}")
        dst = out / rel
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(src, dst)
    (out / MARKER).write_text("staged by scripts/stage_antigravity_plugin.py\n", encoding="utf-8")
    manifest = json.loads((out / ".antigravity-plugin" / "plugin.json").read_text(encoding="utf-8"))
    print(f"staged {manifest['name']} {manifest['version']} -> {out}")
    print(f'next: agy plugin validate --strict "{out}"  &&  agy plugin install "{out}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
