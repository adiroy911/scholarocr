"""Install the ScholarOCR Claude Code skill.

    python scripts/install_skill.py                 # user scope: ~/.claude/skills/scholarocr
    python scripts/install_skill.py --project DIR   # project scope: DIR/.claude/skills/scholarocr
    python scripts/install_skill.py --force         # replace an existing, different scholarocr skill
                                                    # (old copy moved to <.claude>/skill-backups/)

Refuses to overwrite a different existing skill without --force. Prints the command that registers
the MCP server with Claude Code.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "skills" / "scholarocr"


def same_tree(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return False
    return all(same_tree(a / d, b / d) for d in cmp.common_dirs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="install into <project>/.claude/skills instead of ~/.claude/skills")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    root = Path(args.project).expanduser().resolve() / ".claude" / "skills" if args.project else Path.home() / ".claude" / "skills"
    dest = root / "scholarocr"
    if dest.exists():
        if same_tree(SRC, dest):
            print(f"skill already installed and up to date: {dest}")
        elif not args.force:
            print(f"refusing to overwrite existing, different skill at {dest} (use --force)", file=sys.stderr)
            return 1
        else:
            # Keep the backup OUTSIDE the skills directory: anything inside it is loaded as a skill.
            from datetime import datetime

            backup = root.parent / "skill-backups" / f"scholarocr-{datetime.now():%Y%m%d-%H%M%S}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(backup))
            shutil.copytree(SRC, dest)
            print(f"replaced skill at {dest} (previous version kept at {backup})")
    else:
        root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(SRC, dest)
        print(f"installed skill: {dest}")
    exe = Path(sys.executable).parent / ("scholarocr-mcp.exe" if sys.platform == "win32" else "scholarocr-mcp")
    scope = "project" if args.project else "user"
    print("\nRegister the MCP server with Claude Code (once):")
    print(f'  claude mcp add --scope {scope} scholarocr -e SCHOLAROCR_ALLOWED_DIRS="<your document folders>" -- "{exe}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
