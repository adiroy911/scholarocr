"""Cleaned text (``Element.clean_content``): the transcription with casing, punctuation and spacing normalised and
obvious slips of the pen fixed, never anything else.

``content`` stays exactly as written. The vision model proposes a cleaned version in the same call
(``clean_text``); ``check_clean`` accepts it only when, compared with ``content``:

* every math segment (``$...$``, ``$$...$$``, ``\\(...\\)``, ``\\[...\\]``), LaTeX command, number and symbol is
  identical and in the same order (only ``. , ; : ! ?``, whitespace and letter case may differ elsewhere);
* the words are the same words: word-level differences are only spacing (``time.But`` -> ``time. But``), a
  case change of a capitalised word (``Function`` <-> ``function``; never of a single letter such as a variable
  ``A``, an acronym or a mixed-case token such as ``pH``), or a one- or two-letter slip inside a word of at least
  four letters (``temperatre`` -> ``temperature``); no word is added, removed or exchanged;
* the total of such slip edits is small.

Otherwise ``rule_clean`` is used: a deterministic normaliser that fixes spacing around punctuation, ends a
sentence before a capitalised discourse word that clearly starts a new one (``time But the`` -> ``time. But
the``) and sentence-cases other capitalised words in mid-sentence unless they look like proper nouns (an
allowlist of names, units and terms, plus words capitalised more than once on the page). Headings and captions
only get spacing fixes (title case is legitimate there). Which one was used is recorded in
``clean_content_source`` (``model`` or ``rules``).
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

CLEANABLE_TYPES = {"title", "heading", "subheading", "paragraph", "text", "handwriting", "caption", "footnote",
                   "list"}
SPACING_ONLY_TYPES = {"title", "heading", "subheading", "caption"}
FREE_PUNCT = set(".,;:!?")
MAX_SLIP_EDITS_PER_WORD = 2
MIN_SLIP_WORD_LEN = 4

MATH_RE = re.compile(r"\$\$.+?\$\$|(?<!\\)\$(?!\$).+?(?<!\\)\$|\\\(.+?\\\)|\\\[.+?\\\]", re.S)
LATEX_CMD_RE = re.compile(r"\\[A-Za-z]+")
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
BRACKET_RE = re.compile(r"\[[^\]\n]*\]")  # editorial markup such as [illegible: 2 words], [crossed out: Supp]

SENTENCE_STARTERS = {"But", "However", "Hence", "Therefore", "Thus", "So", "Then", "Now", "Also", "Here", "This",
                     "These", "It", "We", "Consider", "Note", "Similarly", "Finally", "Again", "Since", "When",
                     "After", "Hereafter", "Moreover", "Furthermore", "Otherwise", "Next"}
PROPER_WORDS = {
    # people whose names are in units, laws and equations
    "Newton", "Kelvin", "Celsius", "Fahrenheit", "Joule", "Watt", "Ohm", "Ampere", "Volta", "Coulomb", "Faraday",
    "Maxwell", "Boltzmann", "Planck", "Einstein", "Euler", "Gauss", "Laplace", "Bernoulli", "Stefan", "Wien",
    "Fourier", "Kirchhoff", "Hooke", "Pascal", "Archimedes", "Avogadro", "Bohr", "Rutherford", "Dalton", "Hertz",
    "Tesla", "Henry", "Weber", "Lenz", "Ampère", "Carnot", "Clausius", "Rankine", "Prandtl", "Nusselt", "Reynolds",
    "Biot", "Savart", "Lorentz", "Doppler", "Huygens", "Young", "Snell", "Fermat", "Hamilton", "Lagrange",
    "Schrodinger", "Schrödinger", "Heisenberg", "Pauli", "Dirac", "Fermi", "Bose", "Raman", "Bragg", "Compton",
    "Galileo", "Kepler", "Copernicus", "Darwin", "Mendel", "Mendeleev", "Pythagoras", "Euclid", "Cauchy",
    "Riemann", "Leibniz", "Taylor", "Maclaurin", "Rolle", "Lagrangian", "Hamiltonian", "Gaussian", "Newtonian",
    "Cartesian", "Euclidean", "Fourier's", "Newton's", "Ohm's", "Hooke's", "Kirchhoff's", "Gauss's", "Faraday's",
    "Lenz's", "Coulomb's", "Pascal's", "Boyle's", "Charles's", "Boyle", "Charles", "Wiedemann", "Franz",
    # calendar, places, languages
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March",
    "April", "May", "June", "July", "August", "September", "October", "November", "December", "English", "Hindi",
    "Bengali", "French", "German", "Latin", "Greek", "India", "Indian", "Earth", "Sun", "Moon", "God",
    # document vocabulary that is conventionally capitalised before a number or label
    "Fig", "Figure", "Table", "Eq", "Equation", "Section", "Chapter", "Example", "Exercise", "Question", "Theorem",
    "Lemma", "Definition", "Problem", "Q", "Ans", "Answer", "Solution", "Part", "Unit", "Class", "Step", "Case",
    "Note", "NB",
}


# ---------------------------------------------------------------------------------------------- comparison
def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _split_math(text: str) -> tuple[list[str], str]:
    """(math segments in order, text with each segment replaced by a placeholder)."""
    maths = MATH_RE.findall(text)
    return maths, MATH_RE.sub(" \x00 ", text)


def _protected(text: str) -> tuple[list[str], list[str], list[str], list[str]]:
    maths, rest = _split_math(text)
    maths = [re.sub(r"\s+", "", m) for m in maths]  # spacing inside LaTeX never changes its meaning
    brackets = BRACKET_RE.findall(rest)
    rest = BRACKET_RE.sub(" \x01 ", rest)
    cmds = LATEX_CMD_RE.findall(rest)
    numbers = NUMBER_RE.findall(rest)
    symbols = [c for c in rest if not (c.isalnum() or c.isspace() or c in FREE_PUNCT or c in "\x00\x01")]
    return maths + brackets, cmds, numbers, symbols


def _words(text: str) -> list[str]:
    _, rest = _split_math(text)
    rest = BRACKET_RE.sub(" \x01 ", rest)
    rest = NUMBER_RE.sub(" \x02 ", rest)
    rest = "".join(c if (c.isalnum() or c in "'\x00\x01\x02") else " " for c in rest)
    return rest.split()


def _case_change_ok(a: str, b: str) -> bool:
    """``a`` -> ``b`` differ only in letter case; allowed for a capitalised word becoming lower case or the
    reverse (sentence start), never for single letters, acronyms or mixed-case tokens."""
    if len(a) < 2 or not a.isalpha():
        return False
    def kind(w: str) -> str:
        if w.islower():
            return "lower"
        if w[0].isupper() and (len(w) == 1 or w[1:].islower()):
            return "title"
        return "other"
    return {kind(a), kind(b)} == {"lower", "title"}


def check_clean(content: str, candidate: str) -> tuple[bool, str]:
    """(accepted, reason). See the module docstring for what may differ."""
    if candidate is None:
        return False, "no candidate"
    if candidate == content:
        return True, "identical"
    if not candidate.strip():
        return False, "empty"
    pa, pb = _protected(content), _protected(candidate)
    names = ("math or bracketed markup", "LaTeX commands", "numbers", "symbols")
    for name, x, y in zip(names, pa, pb):
        if x != y:
            return False, f"{name} differ"
    wa, wb = _words(content), _words(candidate)
    slips = 0
    sm = difflib.SequenceMatcher(a=[w.lower() for w in wa], b=[w.lower() for w in wb], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for a, b in zip(wa[i1:i2], wb[j1:j2]):
                if a != b and not _case_change_ok(a, b):
                    return False, f"case of {a!r} changed"
            continue
        joined_a, joined_b = "".join(wa[i1:i2]), "".join(wb[j1:j2])
        if joined_a.lower() == joined_b.lower():
            continue  # spacing only
        if op == "replace" and i2 - i1 == 1 and j2 - j1 == 1:
            a, b = wa[i1], wb[j1]
            d = _levenshtein(a.lower(), b.lower())
            if (len(a) >= MIN_SLIP_WORD_LEN and a.isalpha() and b.isalpha() and d <= MAX_SLIP_EDITS_PER_WORD
                    and d <= len(a) // 4 + 1):
                slips += d
                continue
        return False, f"words changed: {' '.join(wa[i1:i2])!r} -> {' '.join(wb[j1:j2])!r}"
    total = sum(len(w) for w in wa) or 1
    if slips > max(3, total // 40):
        return False, f"too many spelling edits ({slips})"
    return True, "ok"


# ---------------------------------------------------------------------------------------------- rule normaliser
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z']*|\d+(?:[.,]\d+)*|\s+|.", re.S)


def page_proper_nouns(texts: Iterable[str]) -> set[str]:
    """Words capitalised in mid-sentence at least twice and never written in lower case on the page: likely
    names or terms the writer capitalises on purpose."""
    caps: dict[str, int] = {}
    lower: set[str] = set()
    for t in texts:
        _, rest = _split_math(t or "")
        for m in re.finditer(r"(?<=[a-z,;] )([A-Za-z][a-z]+)", rest):
            w = m.group(1)
            if w[0].isupper():
                caps[w] = caps.get(w, 0) + 1
        lower.update(w for w in re.findall(r"\b[a-z][a-z]+\b", rest))
    return {w for w, n in caps.items() if n >= 2 and w.lower() not in lower}


def _fix_spacing(seg: str) -> str:
    seg = re.sub(r"[ \t]{2,}", " ", seg)
    seg = re.sub(r"(?<=[A-Za-z0-9)\]])[ \t]+([,;:!?](?![-=]))", r"\1", seg)  # "word ," -> "word,"; ":-" kept
    seg = re.sub(r"(?<=[A-Za-z)])[ \t]+\.(?=\s|$)", ".", seg)  # "word ." -> "word." (not ". 5" or "...")
    seg = re.sub(r"(?<=[a-z]{2})([,;])(?=[A-Za-z])", r"\1 ", seg)  # "a,b" words only (never "1,000")
    seg = re.sub(r"(?<=[a-z]{2})\.(?=[A-Z][a-z])", ". ", seg)  # "time.But" -> "time. But"
    return seg


BULLETS = set("*•●▪‣")


def _clean_segment(seg: str, at_start: bool, proper: set[str], spacing_only: bool) -> tuple[str, bool]:
    """Clean one text segment (no math, no bracketed markup). Returns (text, at sentence start at the end).

    A capitalised word is changed only when the token before it is a word (not punctuation, a number, a bracket
    or a symbol), so capitals after a colon, a dash, a number or an opening bracket are left alone."""
    seg = _fix_spacing(seg)
    if spacing_only:
        return seg, False
    out: list[str] = []
    start = at_start
    last: str | None = None  # the previous non-space token
    for tok in _TOKEN_RE.findall(seg):
        if tok.isspace():
            out.append(tok)
            if "\n" in tok:
                start = True
            continue
        if tok[0].isalpha():
            w = tok
            titled = len(w) >= 2 and w.isalpha() and w[0].isupper() and w[1:].islower()
            after_word = last is not None and last[0].isalpha()
            if titled and after_word and not start and w not in proper and w not in PROPER_WORDS:
                if w in SENTENCE_STARTERS and last.islower() and len(last) >= 2:
                    i = len(out) - 1
                    while i >= 0 and out[i].isspace():
                        i -= 1
                    out.insert(i + 1, ".")  # a new sentence clearly starts here: end the previous one
                else:
                    w = w.lower()
            out.append(w)
            start = False
            last = w
            continue
        out.append(tok)
        if tok in ".!?" or tok in BULLETS:
            start = True
        last = tok
    return "".join(out), start


def rule_clean(text: str, element_type: str = "paragraph", proper: set[str] | None = None) -> str:
    """Deterministic normaliser (see the module docstring). Math and bracketed markup are never touched."""
    if not text:
        return text
    proper = proper or set()
    spacing_only = element_type in SPACING_ONLY_TYPES
    parts = re.split(f"({MATH_RE.pattern}|{BRACKET_RE.pattern})", text, flags=re.S)
    out = []
    start = True
    for part in parts:
        if not part:
            continue
        if MATH_RE.fullmatch(part) or BRACKET_RE.fullmatch(part):
            out.append(part)
            start = False
            continue
        cleaned, start = _clean_segment(part, start, proper, spacing_only)
        out.append(cleaned)
    result = "".join(out)
    ok, _ = check_clean(text, result)
    return result if ok else text


def resolve_clean(content: str, candidate: str | None, element_type: str, *,
                  proper: set[str] | None = None) -> tuple[str | None, str | None, str | None]:
    """(clean_content, source, rejection reason) for one element. Non-text types get (None, None, None)."""
    if element_type not in CLEANABLE_TYPES or not content:
        return None, None, None
    if candidate is not None and candidate.strip():
        ok, reason = check_clean(content, candidate)
        if ok:
            return candidate, "model", None
        return rule_clean(content, element_type, proper), "rules", reason
    return rule_clean(content, element_type, proper), "rules", None
