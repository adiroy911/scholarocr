"""Deterministic LaTeX syntax checks shared by the intelligence layer (uncertainty rules) and the
export layer (isolating invalid math so one bad expression cannot break a whole LaTeX build)."""

from __future__ import annotations

import re

_BEGIN_END = re.compile(r"\\(begin|end)\{([^}]*)\}")


def latex_issues(latex: str | None) -> list[str]:
    if latex is None:
        return []
    s = latex.strip()
    if not s:
        return ["empty LaTeX"]
    issues: list[str] = []
    # Braces, ignoring escaped \{ \}
    depth = 0
    cleaned = re.sub(r"\\[{}]", "", s)
    for ch in cleaned:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                break
    if depth != 0:
        issues.append("unbalanced braces")
    lefts = len(re.findall(r"\\left(?![a-zA-Z])", s))
    rights = len(re.findall(r"\\right(?![a-zA-Z])", s))
    if lefts != rights:
        issues.append(f"\\left/\\right mismatch ({lefts} vs {rights})")
    stack: list[str] = []
    for kind, name in _BEGIN_END.findall(s):
        if kind == "begin":
            stack.append(name)
        elif not stack or stack.pop() != name:
            issues.append(f"\\end{{{name}}} without matching \\begin")
            break
    if stack:
        issues.append(f"unclosed environment(s): {', '.join(stack)}")
    if re.search(r"(?<!\\)\$", s):
        issues.append("stray $ inside LaTeX")
    if re.search(r"\\frac\s*\{[^{}]*\}\s*(?!\{)", s) and not re.search(r"\\frac\s*\{[^{}]*\}\s*\{", s):
        issues.append("\\frac with a single argument")
    if re.search(r"[\^_]\s*$", s):
        issues.append("dangling sub/superscript")
    if "\ufffd" in s:
        issues.append("replacement character in LaTeX")
    return issues
