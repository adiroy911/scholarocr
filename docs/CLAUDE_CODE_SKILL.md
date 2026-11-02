# Claude Code skill: `scholarocr`

Location in the repo: `claude-skill/scholarocr/` (`SKILL.md` + `references/`). The skill contains only
instructions; all OCR happens in the shared engine via MCP (or the CLI as fallback).

## What it teaches Claude

- When a task needs ScholarOCR (scans, photos, handwriting, equations, tables, conversions).
- Reuse before processing (`list_documents`); process only needed pages; poll jobs calmly.
- Retrieve narrowly by page range / element type; follow `<!-- el:ID uncertainty=… -->` anchors.
- Check uncertain items against the original with `get_source_region`; never fill in unresolved text.
- Verify before high-stakes use; export only when asked; cite page numbers.
- CLI fallback commands and exit codes.

References: `mcp-tools.md` (tool reference + IR fields), `workflows.md` (textbook exercise, PYQ
batches, handwritten notes, research papers, conversion, large documents), `output-formats.md`,
`verification-and-errors.md`.

## Install

```bash
python scripts/install_skill.py                # user scope: ~/.claude/skills/scholarocr
python scripts/install_skill.py --project .    # project scope: ./.claude/skills/scholarocr
```
The installer refuses to overwrite a different existing `scholarocr` skill unless `--force` (which
moves the old copy to `.claude/skill-backups/`, outside the skills directory), and prints the `claude mcp add` command for the server.

## Tested

In this environment (Claude Code 2.1.316, Linux): after installation the skill appeared in Claude
Code's skill list; a headless run (`claude -p … --mcp-config …`) loaded the skill and the MCP server
and performed the documented workflow end to end (see MCP_INTEGRATION.md). Not tested: Claude Code on
Windows, and workflows that need visual OCR (no Gemini key was available).
