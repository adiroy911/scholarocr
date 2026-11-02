"""Build the Gemini CLI extension release archive.

Gemini CLI installs an extension from a GitHub release when the release has an archive asset
whose root holds ``gemini-extension.json``
(https://geminicli.com/docs/extensions/releasing/). The extension lives in
``extensions/gemini-cli/`` of this repo, so this script packs the *contents* of that folder
(without ``antigravity-plugin/``, which is a separate Antigravity plugin) plus the repo
``LICENSE`` into one platform-independent archive.

Asset naming: Gemini CLI first looks for ``{platform}.{arch}.`` and ``{platform}.`` prefixed
assets, and otherwise uses a generic asset **only if it is the only asset on the release**. The
default name below contains no platform word, so attach exactly this one file to the release.

Usage:
    python scripts/build_gemini_extension_archive.py            # -> dist-extension/scholarocr-gemini-extension.tar.gz
    python scripts/build_gemini_extension_archive.py --tag v0.1.0   # also checks the tag matches the manifest version
    gh release create v0.1.0 dist-extension/scholarocr-gemini-extension.tar.gz --title v0.1.0 --notes-file NOTES.md
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT_DIR = ROOT / "extensions" / "gemini-cli"
EXCLUDE_DIRS = {"antigravity-plugin", "__pycache__", "node_modules", ".git"}
EXCLUDE_NAMES = {".DS_Store", "Thumbs.db", ".env"}
FIXED_MTIME = 1_767_225_600  # 2026-01-01T00:00:00Z, so rebuilding the same files gives the same bytes
PLATFORM_WORDS = ("darwin", "linux", "win32")


def collect_files() -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for path in sorted(EXT_DIR.rglob("*")):
        rel = path.relative_to(EXT_DIR)
        if any(part in EXCLUDE_DIRS for part in rel.parts) or path.name in EXCLUDE_NAMES:
            continue
        if path.is_file():
            files.append((path, rel.as_posix()))
    licence = ROOT / "LICENSE"
    if licence.exists() and not (EXT_DIR / "LICENSE").exists():
        files.append((licence, "LICENSE"))
    return sorted(files, key=lambda item: item[1])


def check_versions(tag: str | None) -> str:
    manifest = json.loads((EXT_DIR / "gemini-extension.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    problems = []
    if version != project:
        problems.append(f"gemini-extension.json version {version} != pyproject.toml version {project}")
    plugin = EXT_DIR / "antigravity-plugin" / "plugin.json"
    if plugin.exists():
        pv = json.loads(plugin.read_text(encoding="utf-8")).get("version")
        if pv != version:
            problems.append(f"antigravity plugin.json version {pv} != {version}")
    if tag is not None and tag.lstrip("v") != version:
        problems.append(f"tag {tag} does not match manifest version {version}")
    if problems:
        sys.exit("version check failed:\n  " + "\n  ".join(problems))
    return version


def build(out: Path) -> list[str]:
    files = collect_files()
    names = [name for _, name in files]
    if "gemini-extension.json" not in names:
        sys.exit("gemini-extension.json not found at the extension root")
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path, name in files:
            data = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = FIXED_MTIME
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    with open(out, "wb") as fh, gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=FIXED_MTIME) as gz:
        gz.write(raw.getvalue())
    return names


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "dist-extension" / "scholarocr-gemini-extension.tar.gz")
    ap.add_argument("--tag", help="release tag to check against the manifest version, e.g. v0.1.0")
    args = ap.parse_args()
    if not args.out.name.endswith((".tar.gz", ".zip")):
        sys.exit("--out must end in .tar.gz (Gemini CLI picks the extractor from the file name)")
    if any(word in args.out.name.lower() for word in PLATFORM_WORDS):
        print("warning: the name contains a platform word, so Gemini CLI treats it as platform-specific")
    version = check_versions(args.tag)
    names = build(args.out)
    print(f"built {args.out} (version {version}, {len(names)} files):")
    for name in names:
        print(f"  {name}")


if __name__ == "__main__":
    main()
