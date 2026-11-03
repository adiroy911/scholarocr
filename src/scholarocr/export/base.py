"""Shared types and helpers for the export engine.

Exports are generated only from the stored IR plus the preserved sources and saved region
crops. Nothing here calls a model or re-runs OCR.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..exceptions import ExportError
from ..schemas import (
    Document,
    Element,
    ElementType,
    Manifest,
    Page,
    TableCell,
    TableData,
    Uncertainty,
)
from ..storage.artifacts import atomic_write_text

FORMATS = ("json", "markdown", "searchable-pdf", "layout-pdf", "docx", "latex", "csv", "xlsx")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ExportContext(BaseModel):
    """Everything an exporter needs. Relative artifact paths resolve against ``doc_dir``."""

    document: Document
    manifest: Manifest
    doc_dir: Path
    out_dir: Path
    options: dict[str, Any] = Field(default_factory=dict)
    text_layer_font: Path | None = None

    def option(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)

    def flag(self, name: str, default: bool) -> bool:
        """Boolean option that also accepts the strings CLI/MCP callers send ("true", "0", ...)."""
        value = self.options.get(name, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ExportError(f"Option {name!r} must be a boolean, got {value!r}")

    def resolve(self, rel: str) -> Path:
        """Resolve an IR artifact reference, refusing paths that escape the document directory."""
        base = self.doc_dir.resolve()
        path = (base / rel).resolve()
        if not path.is_relative_to(base):
            raise ExportError(f"Artifact reference escapes the document directory: {rel!r}")
        return path


class ExportResult(BaseModel):
    format: str
    primary_file: str
    files: list[str]
    warnings: list[str] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ element helpers
def ordered_pages(document: Document) -> list[Page]:
    return sorted(document.pages, key=lambda p: p.page_number)


def ordered_elements(page: Page) -> list[Element]:
    return sorted(page.elements, key=lambda e: e.reading_order)


def iter_document(document: Document) -> Iterator[tuple[Page, Element]]:
    for page in ordered_pages(document):
        for el in ordered_elements(page):
            yield page, el


def is_unclear(el: Element) -> bool:
    """True when the element must be shown as unclear rather than as certain text."""
    return el.type == ElementType.UNCERTAIN or el.uncertainty == Uncertainty.UNRESOLVED


def unclear_text(el: Element) -> str:
    """Best available description of what could not be read (never invented content)."""
    text = " ".join(el.content.split())
    if not text and el.uncertainty_reasons:
        text = "; ".join(el.uncertainty_reasons)
    return text or "unreadable region"


def heading_level(el: Element) -> int:
    """0 for a title, otherwise the 1-based section level (heading=1, subheading=2 by default)."""
    if el.type == ElementType.TITLE:
        return 0
    if el.level and el.level > 0:
        return el.level
    return 2 if el.type == ElementType.SUBHEADING else 1


def marks_label(marks: str) -> str:
    inner = marks.strip().strip("[]()").strip()
    return f"[{inner} marks]" if inner else ""


def plain_text(el: Element) -> str:
    """Element text with internal whitespace collapsed per line."""
    lines = [" ".join(line.split()) for line in el.content.splitlines()]
    return "\n".join(line for line in lines if line)


_WRAPPERS = (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$"))


def clean_latex(latex: str) -> str:
    """Strip outer math delimiters and collapse blank lines (which are errors in display math)."""
    s = latex.strip()
    changed = True
    while changed and s:
        changed = False
        for start, end in _WRAPPERS:
            if len(s) >= len(start) + len(end) and s.startswith(start) and s.endswith(end):
                s = s[len(start) : len(s) - len(end)].strip()
                changed = True
                break
    return re.sub(r"\n\s*\n+", "\n", s)


def math_latex(el: Element) -> str | None:
    """LaTeX for a math/chemistry element, or None when only a linearised reading exists."""
    if el.latex and el.latex.strip():
        return clean_latex(el.latex)
    return None


# ------------------------------------------------------------------ chemistry (\ce)
_ARROWS = (("<=>", r" \rightleftharpoons "), ("<->", r" \leftrightarrow "), ("->", r" \rightarrow "),
           ("<-", r" \leftarrow "))


def _balanced_group(s: str, start: int) -> int:
    """Index just past the brace group opening at ``s[start] == '{'``, or -1 if unbalanced."""
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _ce_token(tok: str) -> str:
    if tok in ("+", "=") or tok.startswith("\\"):
        return tok
    m = re.match(r"^(\d+(?:/\d+)?)(.*)$", tok)  # stoichiometric coefficient
    coeff, body = (m.group(1), m.group(2)) if m and m.group(2) else ("", tok)
    charge = ""
    # charge: explicit "^..." (X^2-, X^{3+}) or a single trailing sign (Na+, NO3-)
    cm = re.match(r"^(.*?[A-Za-z0-9)\]])(?:\^(\{[^}]*\}|[^{}\s]+)|([+-]))$", body)
    if cm:
        body, charge = cm.group(1), (cm.group(2) or cm.group(3)).strip("{}")
    body = re.sub(r"(?<=[A-Za-z)\]])(\d+)", r"_{\1}", body)
    return coeff + body + (f"^{{{charge}}}" if charge else "")


def ce_body_to_math(body: str) -> str:
    """Approximate mhchem syntax with plain math (subscripts, charges, arrows)."""
    for arrow, repl in _ARROWS:
        body = body.replace(arrow, repl)
    return " ".join(_ce_token(t) for t in body.split())


def ce_to_math(latex: str) -> str:
    r"""Replace every ``\ce{...}`` with ``\mathrm{...}`` math that needs no mhchem package."""
    out: list[str] = []
    i = 0
    while True:
        j = latex.find(r"\ce", i)
        if j < 0:
            out.append(latex[i:])
            break
        k = j + 3
        while k < len(latex) and latex[k] == " ":
            k += 1
        end = _balanced_group(latex, k) if k < len(latex) and latex[k] == "{" else -1
        if end < 0 or (j + 3 < len(latex) and latex[j + 3].isalpha()):
            out.append(latex[i : j + 3])
            i = j + 3
            continue
        out.append(latex[i:j])
        out.append(r"\mathrm{" + ce_body_to_math(latex[k + 1 : end - 1]) + "}")
        i = end
    return "".join(out)


# ------------------------------------------------------------------ questions and inline math
def starts_with_question_number(number: str | None, text: str) -> bool:
    """True when ``text`` already begins with the printed question number ("4. If", "Q4)", "(4)")."""
    if not number:
        return False
    stripped = text.lstrip("*_ \\")
    return re.match(rf"^(Q(uestion)?\.?\s*)?\(?{re.escape(number)}\b", stripped, re.I) is not None


# $...$ not preceded by a backslash, not $$, no leading/trailing space inside (so "$5 and $10" stays text).
INLINE_MATH = re.compile(r"(?<![\\$])\$(?!\$)(?=\S)([^$\n]+?)(?<=\S)(?<!\\)\$(?!\$)")


def split_inline_math(text: str) -> list[tuple[str, bool]]:
    """[(segment, is_math)] for a line of text with inline ``$...$`` math."""
    out: list[tuple[str, bool]] = []
    pos = 0
    for m in INLINE_MATH.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out


_GREEK = {n: c for n, c in zip(
    "alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi pi varpi rho "
    "sigma varsigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi "
    "Omega".split(), "αβγδεεζηθϑικλμνξπϖρσςτυφφχψωΓΔΘΛΞΠΣΥΦΨΩ")}
_SYMBOLS = {"times": "×", "cdot": "·", "pm": "±", "mp": "∓", "div": "÷", "leq": "≤", "le": "≤", "geq": "≥",
            "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡", "sim": "∼", "propto": "∝",
            "infty": "∞", "partial": "∂", "nabla": "∇", "int": "∫", "oint": "∮", "sum": "Σ", "prod": "Π",
            "to": "→", "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "leftrightarrow": "↔",
            "rightleftharpoons": "⇌", "degree": "°", "circ": "°", "prime": "′", "in": "∈", "notin": "∉",
            "subset": "⊂", "cup": "∪", "cap": "∩", "forall": "∀", "exists": "∃", "hbar": "ħ", "ell": "ℓ",
            "angle": "∠", "perp": "⊥", "parallel": "∥", "cdots": "⋯", "ldots": "…", "dots": "…",
            "langle": "⟨", "rangle": "⟩", ",": " ", ";": " ", "quad": " ", "qquad": "  ", "!": ""}
_SUP = dict(zip("0123456789+-=()nix", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˣ"))
_SUB = dict(zip("0123456789+-=()aeoxijnmkt", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓᵢⱼₙₘₖₜ"))


def _script(s: str, table: dict[str, str], mark: str) -> str:
    s = s.strip()
    if s and all(c in table for c in s):
        return "".join(table[c] for c in s)
    return f"{mark}({s})" if len(s) > 1 else f"{mark}{s}"


def latex_to_readable(tex: str) -> str:
    r"""Plain-text rendering of simple inline LaTeX for documents without native equations:
    ``x^2`` -> ``x²``, ``\gamma_0`` -> ``γ₀``, ``\frac{a}{b}`` -> ``a/b``, ``\ce{SO4^2-}`` -> ``SO₄²⁻``.
    Anything it does not know is kept as readable text (command names without backslashes)."""
    s = tex
    # \ce{...}: mhchem body -> subscripted formula with charges and arrows
    def ce(m: re.Match) -> str:
        body = m.group(1)
        for arrow, repl in (("<=>", " ⇌ "), ("<->", " ↔ "), ("->", " → "), ("<-", " ← ")):
            body = body.replace(arrow, repl)
        body = re.sub(r"\^\{?([0-9]*[+-])\}?", lambda c: _script(c.group(1), _SUP, "^"), body)
        body = re.sub(r"(?<=[A-Za-z)\]])(\d+)", lambda c: _script(c.group(1), _SUB, "_"), body)
        return " ".join(body.split())
    s = re.sub(r"\\ce\s*\{((?:[^{}]|\{[^{}]*\})*)\}", ce, s)
    for _ in range(3):  # nested groups, innermost first
        s = re.sub(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}",
                   lambda m: (m.group(1) if len(m.group(1)) < 2 else f"({m.group(1)})") + "/"
                   + (m.group(2) if len(m.group(2)) < 2 else f"({m.group(2)})"), s)
        s = re.sub(r"\\sqrt\s*\{([^{}]*)\}", lambda m: "√" + (m.group(1) if len(m.group(1)) < 2 else f"({m.group(1)})"), s)
        s = re.sub(r"\\(?:text|mathrm|mathbf|mathit|mathsf|mathcal|operatorname|boldsymbol|vec|bar|hat)\s*\{([^{}]*)\}",
                   r"\1", s)
    s = re.sub(r"\\([A-Za-z]+|[,;!])", lambda m: _GREEK.get(m.group(1)) or _SYMBOLS.get(m.group(1), m.group(1)), s)
    for _ in range(3):
        s = re.sub(r"\^\s*\{([^{}]*)\}", lambda m: _script(m.group(1), _SUP, "^"), s)
        s = re.sub(r"_\s*\{([^{}]*)\}", lambda m: _script(m.group(1), _SUB, "_"), s)
    s = re.sub(r"\^\s*(\\?[A-Za-z0-9+\-]|[α-ω])", lambda m: _script(m.group(1), _SUP, "^"), s)
    s = re.sub(r"_\s*([A-Za-z0-9+\-]|[α-ω])", lambda m: _script(m.group(1), _SUB, "_"), s)
    s = s.replace("\\left", "").replace("\\right", "").replace("{", "").replace("}", "")
    return " ".join(s.split())


# ------------------------------------------------------------------ tables
@dataclass(frozen=True)
class PlacedCell:
    row: int
    col: int
    row_span: int
    col_span: int
    cell: TableCell


@dataclass
class TableLayout:
    """Cells placed on a rectangular grid (HTML semantics: spanned positions are omitted
    from the following cells of the IR row / the following rows)."""

    n_rows: int
    n_cols: int
    cells: list[PlacedCell]
    owner: list[list[PlacedCell | None]]
    header_rows: int

    def anchor_at(self, r: int, c: int) -> PlacedCell | None:
        p = self.owner[r][c]
        return p if p is not None and p.row == r and p.col == c else None

    def text_grid(self) -> list[list[str]]:
        """Text in each anchor cell, empty strings in spanned positions."""
        grid = [[""] * self.n_cols for _ in range(self.n_rows)]
        for p in self.cells:
            grid[p.row][p.col] = p.cell.text
        return grid


def layout_table(table: TableData) -> TableLayout:
    occupied: dict[tuple[int, int], PlacedCell] = {}
    placed: list[PlacedCell] = []
    n_rows = len(table.rows)
    for r, row in enumerate(table.rows):
        c = 0
        for cell in row:
            while (r, c) in occupied:
                c += 1
            rs = max(1, min(cell.row_span, n_rows - r))
            cs = max(1, cell.col_span)
            p = PlacedCell(r, c, rs, cs, cell)
            placed.append(p)
            for dr in range(rs):
                for dc in range(cs):
                    occupied.setdefault((r + dr, c + dc), p)
            c += cs
    n_cols = max((c + 1 for (_, c) in occupied), default=0)
    owner: list[list[PlacedCell | None]] = [[None] * n_cols for _ in range(n_rows)]
    for (r, c), p in occupied.items():
        owner[r][c] = p
    header = table.header_rows
    if header <= 0:
        for row in table.rows:
            if row and all(cell.is_header for cell in row):
                header += 1
            else:
                break
    return TableLayout(n_rows, n_cols, placed, owner, min(header, n_rows))


# ------------------------------------------------------------------ assets
def copy_asset(ctx: ExportContext, rel: str, dest_dir: Path, warnings: list[str], name: str | None = None) -> Path | None:
    """Copy a stored region crop into ``dest_dir`` (as ``name`` if given, else its own file name).

    Returns the copy, or None with a warning when the crop is missing or unsafe."""
    try:
        src = ctx.resolve(rel)
    except ExportError as e:
        warnings.append(str(e))
        return None
    if not src.is_file():
        warnings.append(f"Region image missing on disk: {rel}")
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (name or src.name)
    shutil.copyfile(src, dest)
    return dest


def region_image(page: Page, el: Element) -> str | None:
    """The element's saved crop, never the whole page image."""
    rel = el.source.region_image
    if not rel or rel in (page.original_image, page.processed_image, el.source.page_image):
        return None
    return rel


def write_text(path: Path, text: str) -> None:
    atomic_write_text(path, text)


# ------------------------------------------------------------------ text variants and figure images
TEXT_VARIANTS = ("clean", "faithful")
FIGURE_PREFERENCES = ("clean", "original", "svg")
_FIGURE_ORDER = {"svg": ("svg", "clean", "original"), "clean": ("clean", "original"), "original": ("original",)}
_FIGURE_ALIASES = {"redrawn": "svg", "vector": "svg", "cleaned": "clean", "crop": "original", "as_written": "original"}


def element_text(el: Element, variant: str) -> str:
    """The element's text in ``variant``: ``"clean"`` gives ``clean_content`` when it exists, anything else (or
    a missing clean version) gives ``content``, the faithful transcription."""
    if variant == "clean" and el.clean_content is not None:
        return el.clean_content
    return el.content


def svg_is_faithful(el: Element) -> bool:
    """A redrawn SVG exists and passed the fidelity check against the crop (``figure.svg_fidelity.passed``)."""
    fig = el.figure
    return bool(fig and fig.svg and (fig.svg_fidelity or {}).get("passed"))


def redraw_note(el: Element, prefer: str) -> str | None:
    """Warning for an export that asked for redrawn figures where this figure's redraw is not usable."""
    if resolve_figure_preference(prefer) != "svg" or not (el.figure and el.figure.svg) or svg_is_faithful(el):
        return None
    score = (el.figure.svg_fidelity or {}).get("score")
    pct = f" (fidelity {score:.0%})" if isinstance(score, (int, float)) else ""
    return f"figure {el.element_id}: the redraw is not faithful{pct}; the cleaned crop is used instead"


def _figure_rels(el: Element) -> dict[str, str | None]:
    fig = el.figure
    # A redraw that failed the fidelity check is never used in place of the crop.
    return {"svg": fig.svg if svg_is_faithful(el) else None, "clean": fig.image_clean if fig else None,
            "original": el.source.region_image}


def figure_image_rel(el: Element, prefer: str) -> str | None:
    """The stored image path (relative to the document dir) to show for a figure, by preference with fallback:
    ``svg`` -> svg, clean, original; ``clean`` -> clean, original; ``original`` -> original. No disk check."""
    rels = _figure_rels(el)
    for kind in _FIGURE_ORDER[resolve_figure_preference(prefer)]:
        if rels[kind]:
            return rels[kind]
    return None


def figure_image_path(doc_dir: Path, el: Element, prefer: str) -> Path | None:
    """Absolute path of the figure image to use, by ``prefer`` (``"clean"``, ``"original"`` or ``"svg"``) with the
    same fallback as ``figure_image_rel``, skipping files that are missing or would escape ``doc_dir``."""
    base = Path(doc_dir).resolve()
    rels = _figure_rels(el)
    for kind in _FIGURE_ORDER[resolve_figure_preference(prefer)]:
        rel = rels[kind]
        if not rel:
            continue
        path = (base / rel).resolve()
        if path.is_relative_to(base) and path.is_file():
            return path
    return None


def resolve_figure_preference(value: str | None) -> str:
    v = (value or "clean").strip().lower().replace("-", "_")
    v = _FIGURE_ALIASES.get(v, v)
    if v not in FIGURE_PREFERENCES:
        raise ExportError(f"figure_variant must be one of clean, original, redrawn (svg); got {value!r}")
    return v


TEXT_TYPES_FOR_VARIANT = {ElementType.TITLE, ElementType.HEADING, ElementType.SUBHEADING, ElementType.PARAGRAPH,
                          ElementType.TEXT, ElementType.HANDWRITING, ElementType.CAPTION, ElementType.FOOTNOTE,
                          ElementType.LIST}


def document_is_handwritten(document: Document) -> bool:
    """True when most text elements were read as handwriting."""
    els = [el for p in document.pages for el in p.elements if el.type in TEXT_TYPES_FOR_VARIANT]
    return bool(els) and sum(1 for el in els if el.handwritten) * 2 >= len(els)


def default_text_variant(document: Document) -> str:
    """``clean`` for handwritten documents (slips of the pen are noise there), ``faithful`` for printed ones."""
    return "clean" if document_is_handwritten(document) else "faithful"


def resolve_text_variant(document: Document, requested: str | None) -> str:
    v = (requested or "auto").strip().lower().replace("-", "_")
    v = {"cleaned": "clean", "as_written": "faithful", "original": "faithful", "exact": "faithful"}.get(v, v)
    if v == "auto":
        return default_text_variant(document)
    if v not in TEXT_VARIANTS:
        raise ExportError(f"text_variant must be clean, faithful or auto; got {requested!r}")
    return v


def with_text_variant(document: Document, variant: str) -> Document:
    """A copy of ``document`` whose ``content`` is the chosen variant (``clean`` swaps in ``clean_content``
    where it exists). ``faithful`` returns the document unchanged."""
    if variant != "clean":
        return document
    doc = document.model_copy(deep=True)
    for page in doc.pages:
        for el in page.elements:
            if el.clean_content is not None:
                el.content = el.clean_content
    return doc


def export_variant_context(ctx: ExportContext) -> tuple[ExportContext, str]:
    """``ctx`` with its document in the requested text variant (option ``text_variant``: clean | faithful |
    auto), and the variant used."""
    variant = resolve_text_variant(ctx.document, ctx.option("text_variant"))
    return ctx.model_copy(update={"document": with_text_variant(ctx.document, variant)}), variant


def figure_preference(ctx: ExportContext) -> str:
    """Option ``figure_variant``: clean (default) | original | redrawn (the sanitised SVG, if made)."""
    return resolve_figure_preference(ctx.option("figure_variant", ctx.option("figures")))


def svg_to_png(svg_path: Path, out: Path, *, width_px: int = 1600) -> Path:
    """Rasterise a stored (already sanitised) SVG with PyMuPDF, for formats that cannot embed SVG."""
    import pymupdf

    with pymupdf.open(svg_path) as d:
        page = d[0]
        zoom = max(0.5, min(8.0, width_px / max(page.rect.width, 1.0)))
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        out.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(out))
    return out


def svg_to_pdf(svg_path: Path, out: Path) -> Path:
    """Convert a stored SVG to a one-page vector PDF (LaTeX ``\\includegraphics`` cannot read SVG)."""
    import pymupdf

    with pymupdf.open(svg_path) as d:
        data = d.convert_to_pdf()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out
