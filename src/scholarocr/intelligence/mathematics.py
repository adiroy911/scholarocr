"""Deterministic checks for mathematical and chemical LaTeX. These never change content; they
produce issue strings that raise an element's uncertainty and trigger targeted review."""

from __future__ import annotations

import re

from ..latex_check import latex_issues

__all__ = ["latex_issues", "chemistry_issues"]


# Reaction signs: mhchem arrows (->, <-, <->, <-->, <=>, <=>>, <<=>), "=" as many textbooks print it
# (2H2 + O2 = 2H2O), Unicode arrows and LaTeX arrow commands.
REACTION_SIGN = re.compile(
    r"(<<=>|<=>>|<=>|<-->|<->|->|<-|=|→|⟶|←|⟵|⇌|⇄|↔|⇋|"
    r"\\(?:long)?(?:right|left|leftright)arrow|\\rightleftharpoons|\\rightleftarrows|\\xrightarrow|\\to\b)")


def chemistry_issues(latex: str | None, text: str) -> list[str]:
    issues = latex_issues(latex)
    body = latex or text
    if latex and "\\ce" not in latex and not re.search(r"\\(rightarrow|longrightarrow|rightleftharpoons|to)", latex):
        issues.append("chemical equation without \\ce{} or reaction arrow")
    if body and "+" in body and REACTION_SIGN.search(body) is None:
        issues.append("reaction without an arrow")
    return issues
