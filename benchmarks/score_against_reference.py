"""Second opinion on `scholarocr evaluate` for exam pages: the same reference, with spelling differences removed.

    python benchmarks/score_against_reference.py reference.json STORE_DIR DOCUMENT_ID OUT_DIR

reference.json: {"pages": [{"page_number", "subject", "text", "equations"}]}. In "text", math is LaTeX in $...$ and
tokens from a marks column are wrapped in « ». Writes adjusted-report.json and diffs.txt to OUT_DIR.

`evaluate` compares raw strings, so `\\frac` against `\\dfrac`, `10^{184}` against `10¹⁸⁴`, a number stored in
question.number instead of the text and a marks column placed elsewhere all count as errors. The adjusted score:

* hypothesis: non-furniture elements in reading order, table cells joined, the question number put in front when the
  text lacks it, marks-column tokens removed when they stand alone or end a line after a gap;
* reference: marks-column tokens removed;
* both: Unicode NFKC, LaTeX reduced to its characters (no $, braces, ^, _; symbol names as symbols; same-symbol
  spellings merged), one form of quotes and dashes, markdown emphasis and bullets removed, all whitespace removed.

So it cannot see: spacing errors, or a superscript read as a plain digit (x^2 and x2 become equal). diffs.txt lists
every remaining difference for reading by eye; only that reading says which ones are real.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
import unicodedata
from pathlib import Path

from scholarocr.config import Settings
from scholarocr.evaluation.metrics import levenshtein
from scholarocr.export.base import question_lead
from scholarocr.schemas import ElementType
from scholarocr.schemas.elements import FURNITURE_TYPES
from scholarocr.service import ScholarOCR

SYMBOLS = {
    "times": "×", "to": "→", "rightarrow": "→", "in": "∈", "cup": "∪", "infty": "∞", "pi": "π", "theta": "θ",
    "lambda": "λ", "mu": "μ", "omega": "ω", "neq": "≠", "ne": "≠", "ge": "≥", "geq": "≥", "le": "≤", "leq": "≤",
    "circ": "°", "imath": "i", "jmath": "j", "cdot": "·", "dfrac": "\\frac", "tfrac": "\\frac",
    "overrightarrow": "\\vec", "cot": "cot", "cos": "cos", "sin": "sin", "tan": "tan", "sec": "sec", "log": "log",
    "lim": "lim",
}
WRAPPERS = re.compile(r"\\(?:ce|text|mathrm|mathbf|boldsymbol|bm|operatorname)\{([^{}]*)\}")
SPACING = re.compile(r"\\(?:left|right|displaystyle|quad|qquad)(?![a-zA-Z])|\\[,;:! ]|~")
EMPHASIS = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", re.S)


def canon(s: str) -> str:
    s = EMPHASIS.sub(lambda m: m.group(1) or m.group(2), s)
    s = unicodedata.normalize("NFKC", s).replace("⁄", "/")
    while WRAPPERS.search(s):
        s = WRAPPERS.sub(r" \1 ", s)  # the space ends a symbol name before it: \circ\text{C}
    s = SPACING.sub("", s)
    s = re.sub(r"\\([a-zA-Z]+)", lambda m: SYMBOLS.get(m.group(1), "\\" + m.group(1)), s)
    s = re.sub("[‘’`´“”\"]", "'", s)
    s = re.sub("[–—−‐]", "-", s)
    s = re.sub(r"\\([{}])", r"\1", s)
    s = re.sub(r"[$^_{}•\s]", "", s)
    return s


def split_marks(text: str) -> tuple[str, list[str]]:
    marks = re.findall(r"«([^»]*)»", text)
    return re.sub(r"«[^»]*»", " ", text), marks


def strip_marks(line: str, marks: set[str]) -> str:
    if canon(line) in marks:
        return ""
    m = re.match(r"^(.*?\S)\s{2,}(\S+)\s*$", line)
    if m and canon(m.group(2)) in marks:
        return m.group(1)
    m = re.match(r"^(.*\S)\s*(\[[^\]]*\])\s*$", line)
    if m and canon(m.group(2)) in marks:
        return m.group(1)
    return line


def hypothesis(page, marks: set[str]) -> str:
    parts = []
    for el in sorted(page.elements, key=lambda e: e.reading_order):
        if el.type in FURNITURE_TYPES or el.type == ElementType.UNCERTAIN:
            continue
        if el.type == ElementType.TABLE and el.table:
            lines = [c for row in el.table.as_grid() for c in row if c]
        else:
            lines = (el.content or el.latex or "").split("\n")
        lines = [strip_marks(ln, marks) for ln in lines]
        text = "\n".join(ln for ln in lines if ln)
        lead = question_lead(el.question.number, text) if el.question else ""
        parts.append(f"{lead} {text}" if lead else text)
    return "\n".join(p for p in parts if p)


def main() -> None:
    ref_path, store, doc_id, out = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
    out.mkdir(parents=True, exist_ok=True)
    settings = Settings.from_env(environ={"SCHOLAROCR_PROVIDER": "host", "SCHOLAROCR_DATA_DIR": str(store),
                                          "SCHOLAROCR_ALLOWED_DIRS": str(store)})
    doc = ScholarOCR(settings).get_document(doc_id)
    pages = {p.page_number: p for p in doc.pages}
    rows, diffs = [], []
    for rp in json.loads(ref_path.read_text(encoding="utf-8"))["pages"]:
        n = rp["page_number"]
        ref_text, marks = split_marks(rp["text"])
        mark_set = {canon(m) for m in marks}
        ref, hyp = canon(ref_text), canon(hypothesis(pages[n], mark_set))
        dist = levenshtein(ref, hyp)
        found, missed = 0, []
        for eq in rp.get("equations", []):
            if canon(eq) in hyp:
                found += 1
            else:
                missed.append(eq)
        rows.append({"page_number": n, "subject": rp["subject"], "ref_chars": len(ref), "edit_distance": dist,
                     "adjusted_cer": round(dist / len(ref), 4), "equations": len(rp.get("equations", [])),
                     "equations_found": found, "equations_missed": missed})
        diffs.append(f"\n===== page {n} ({rp['subject']}) distance {dist} of {len(ref)} =====")
        for op, a, b, c, d in difflib.SequenceMatcher(None, ref, hyp, autojunk=False).get_opcodes():
            if op != "equal":
                diffs.append(f"{op:8} ref[{ref[a:b]}] hyp[{hyp[c:d]}]   ...{ref[max(0, a - 18):a]}|{ref[b:b + 12]}...")
    def total(selected: list[dict]) -> dict:
        chars = sum(r["ref_chars"] for r in selected)
        dist = sum(r["edit_distance"] for r in selected)
        return {"pages": len(selected), "ref_chars": chars, "edit_distance": dist, "adjusted_cer": round(dist / chars, 4),
                "equations": sum(r["equations"] for r in selected),
                "equations_found": sum(r["equations_found"] for r in selected)}
    report = {"all": total(rows), "by_subject": {s: total([r for r in rows if r["subject"] == s])
                                                 for s in sorted({r["subject"] for r in rows})}, "pages": rows}
    (out / "adjusted-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "diffs.txt").write_text("\n".join(diffs), encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
