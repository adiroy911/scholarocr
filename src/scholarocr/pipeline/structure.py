"""Deterministic academic-structure annotation and document outline.

Gemini already assigns roles on visual pages; these rules fill gaps (text-layer pages, DOCX)
without overriding a role that is already set.
"""

from __future__ import annotations

import re

from ..schemas import Element, ElementType, QuestionInfo, Role
from ..schemas.elements import HEADING_TYPES

QUESTION_RE = re.compile(r"^\s*(?:Q(?:uestion)?\.?\s*)?(\d{1,3})\s*[.):]\s+\S", re.I)
SUBQ_RE = re.compile(r"^\s*\(?([a-h]|i{1,3}|iv|v|vi{0,3})\)\s+\S", re.I)
MARKS_RE = re.compile(r"[\[(]\s*(\d+(?:\s*[+×x]\s*\d+)*)\s*(?:marks?|M)?\s*[\])]\s*$", re.I)
ROLE_WORDS = [
    (re.compile(r"^\s*definition\b", re.I), Role.DEFINITION),
    (re.compile(r"^\s*theorem\b", re.I), Role.THEOREM),
    (re.compile(r"^\s*lemma\b", re.I), Role.LEMMA),
    (re.compile(r"^\s*example\b", re.I), Role.EXAMPLE),
    (re.compile(r"^\s*proof\b", re.I), Role.PROOF),
    (re.compile(r"^\s*(solution|sol\.)", re.I), Role.SOLUTION),
    (re.compile(r"^\s*(answer|ans\.)", re.I), Role.ANSWER),
    (re.compile(r"^\s*exercise\b", re.I), Role.EXERCISE),
    (re.compile(r"^\s*abstract\b", re.I), Role.ABSTRACT),
    (re.compile(r"^\s*(keywords|key words)\b", re.I), Role.KEYWORDS),
    (re.compile(r"^\s*chapter\s+\w+", re.I), Role.CHAPTER),
]


def annotate(elements: list[Element]) -> None:
    last_question: Element | None = None
    for el in sorted(elements, key=lambda e: e.reading_order):
        text = el.content or ""
        if el.role == Role.NONE and el.type not in (ElementType.HEADER, ElementType.FOOTER, ElementType.PAGE_NUMBER):
            m = QUESTION_RE.match(text)
            if m and el.type in (ElementType.PARAGRAPH, ElementType.TEXT, ElementType.LIST, ElementType.HANDWRITING):
                el.role = Role.QUESTION
                el.question = el.question or QuestionInfo(number=m.group(1))
            elif SUBQ_RE.match(text) and last_question is not None and el.type in (ElementType.PARAGRAPH, ElementType.TEXT):
                el.role = Role.SUBQUESTION
                el.question = el.question or QuestionInfo(number=SUBQ_RE.match(text).group(1),  # type: ignore[union-attr]
                                                          parent_number=last_question.question.number if last_question.question else None)
                el.parent_id = el.parent_id or last_question.element_id
            else:
                for rx, role in ROLE_WORDS:
                    if rx.match(text):
                        el.role = role
                        break
        if el.role in (Role.QUESTION, Role.SUBQUESTION):
            mm = MARKS_RE.search(text)
            if mm:
                el.question = el.question or QuestionInfo()
                if not el.question.marks:
                    el.question.marks = mm.group(1).replace(" ", "")
        if el.role == Role.QUESTION:
            last_question = el


def outline(elements: list[Element]) -> list[dict]:
    """Flat outline of headings and questions with element/page references (for navigation)."""
    out = []
    for el in elements:
        if el.type in HEADING_TYPES or el.role in (Role.CHAPTER, Role.SECTION):
            level = el.level or (0 if el.type == ElementType.TITLE else 1 if el.type == ElementType.HEADING else 2)
            out.append({"kind": "heading", "level": level, "text": el.content[:200], "page": el.page_number,
                        "element_id": el.element_id})
        elif el.role in (Role.QUESTION, Role.SUBQUESTION) and el.question:
            out.append({"kind": el.role.value, "number": el.question.number, "marks": el.question.marks,
                        "page": el.page_number, "element_id": el.element_id, "text": el.content[:120]})
    return out
