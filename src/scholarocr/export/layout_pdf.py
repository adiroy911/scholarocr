"""Layout PDF: a typeset remake of each page that keeps the original layout.

Every output page has the original page size and every element (headings, paragraphs, equations, tables,
figures, page numbers, ...) is placed at its bbox and set in clean type: text auto-fits its box, equations are
typeset with KaTeX (mhchem for chemistry), tables are real tables and figures are the stored crops. This is
different from ``searchable-pdf``, which keeps the original page image and adds invisible text.

Rendering: the pages are built as positioned HTML and printed by a headless Chromium-family browser (Edge,
Chrome, Chromium, Brave or Playwright's Chromium; see ``layout_html.find_browser``). Without a browser a basic
PyMuPDF renderer is used instead (same positions and auto-fit; equations as plain text), with a warning.

Nothing here calls a model or re-reads the page: only the IR, the stored crops and the page images are used.
"""

from __future__ import annotations

import html
import json
import re
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pymupdf
from PIL import Image

from ..exceptions import ExportError
from ..schemas import Document, DocumentType, Element, ElementType, Page
from ..schemas.elements import HEADING_TYPES, MATH_TYPES, VISUAL_TYPES
from ..utils import parse_page_range
from . import base as _base
from . import layout_html
from .base import (
    ExportContext,
    ExportResult,
    heading_level,
    is_unclear,
    latex_to_readable,
    layout_table,
    marks_label,
    math_latex,
    ordered_elements,
    ordered_pages,
    split_inline_math,
    question_lead,
    unclear_text,
)

OUTPUT_NAME = "document.layout.pdf"
BODY_MIN, BODY_MAX = 6.0, 28.0
HEADING_MAX, TITLE_MAX = 36.0, 40.0
MATH_MIN, MATH_MAX = 5.0, 34.0
TABLE_MIN, TABLE_MAX = 4.0, 16.0
# (size, floor) of each kind of text relative to the page's body size. Headings never shrink below the body
# size; equations never below 0.9 of it (they extend past a tight box instead).
SCALES = {"text": (1.0, 0.75), "title": (1.5, 1.0), "heading": (1.3, 1.0), "subheading": (1.15, 1.0),
          "small": (0.88, 0.7), "furniture": (0.85, 0.6), "code": (0.9, 0.7), "unclear": (0.95, 0.7),
          "math": (1.05, 0.9)}
SIZE_TOLERANCE_PT = 0.5
MIN_BOX_PT = 2.0
IN_FIGURE = 0.6  # share of a text box inside a figure above which it is treated as a label of the figure
NESTED = 0.9  # share of the smaller text box inside the larger one above which they are not split
KEEP_AFTER_TRIM = 0.5  # a box is trimmed out of an overlap only if it keeps at least this share of its height
RECALL_OK = 0.6
NATURAL_SLACK_PT = 24.0  # a crop up to this much larger than its box is drawn at natural size (crop margin)
FREE_MARGIN_PT, FREE_GAP_PT = 6.0, 2.0  # a grown box keeps this far from the page edge / the next element

TEXT_VARIANTS = ("clean", "faithful")
FIGURE_MODES = ("clean", "original", "svg")
BACKGROUNDS = ("blank", "paper")
RENDERERS = ("auto", "chromium", "pymupdf")
FURNITURE = {ElementType.HEADER, ElementType.FOOTER, ElementType.PAGE_NUMBER}
SMALL = {ElementType.CAPTION, ElementType.FOOTNOTE, ElementType.BIBLIOGRAPHY_ENTRY, ElementType.REFERENCE}


# ------------------------------------------------------------------------------- shared helpers (with fallbacks)
def _element_text_fallback(el: Element, variant: str = "faithful") -> str:
    """Local stand-in for ``base.element_text``: cleaned text for ``clean`` when present, else ``content``."""
    if variant == "clean":
        clean = getattr(el, "clean_content", None)
        if isinstance(clean, str) and clean.strip():
            return clean
    return el.content


def _figure_image_path_fallback(doc_dir: Path, el: Element, prefer: str = "clean") -> Path | None:
    """Local stand-in for ``base.figure_image_path``: the best stored figure image for ``prefer``."""
    fig = el.figure
    clean = getattr(fig, "image_clean", None) if fig is not None else None
    svg = getattr(fig, "svg", None) if fig is not None else None
    original = el.source.region_image
    order = {"svg": (svg, clean, original), "original": (original, clean), "clean": (clean, original)}
    base = Path(doc_dir).resolve()
    for rel in order.get(prefer, order["clean"]):
        if not rel:
            continue
        path = (base / rel).resolve()
        if path.is_relative_to(base) and path.is_file():
            return path
    return None


def element_text(el: Element, variant: str) -> str:
    fn: Callable[[Element, str], str] = getattr(_base, "element_text", _element_text_fallback)
    return fn(el, variant)


def figure_image_path(doc_dir: Path, el: Element, prefer: str) -> Path | None:
    fn = getattr(_base, "figure_image_path", _figure_image_path_fallback)
    result = fn(doc_dir, el, prefer)
    if result is None:
        return None
    path = Path(result)
    if not path.is_absolute():
        path = Path(doc_dir) / path
    path = path.resolve()
    return path if path.is_relative_to(Path(doc_dir).resolve()) and path.is_file() else None


# ------------------------------------------------------------------------------------------------- options
@dataclass(frozen=True)
class LayoutOptions:
    text_variant: str
    figures: str
    background: str
    show_boxes: bool
    renderer: str
    pages: list[int] | None
    browser: str | None


def _choice(ctx: ExportContext, name: str, allowed: tuple[str, ...], default: str) -> str:
    value = str(ctx.option(name, default) or default).strip().lower()
    if value not in allowed:
        raise ExportError(f"Option {name!r} must be one of {', '.join(allowed)}; got {value!r}")
    return value


def is_handwritten_document(document: Document) -> bool:
    if document.document_type == DocumentType.HANDWRITTEN_NOTES:
        return True
    texty = [e for e in document.iter_elements() if e.type not in VISUAL_TYPES and e.content.strip()]
    return bool(texty) and sum(e.handwritten for e in texty) * 2 > len(texty)


def parse_options(ctx: ExportContext) -> LayoutOptions:
    default_variant = "clean" if is_handwritten_document(ctx.document) else "faithful"
    variant = _choice(ctx, "text_variant", (*TEXT_VARIANTS, "auto"), default_variant)  # auto: the app's saved choice
    pages = ctx.option("pages")
    selected = None
    if pages not in (None, "", "all"):
        selected = parse_page_range(str(pages), max((p.page_number for p in ctx.document.pages), default=0))
        if not selected:
            raise ExportError(f"Option 'pages' selects no pages: {pages!r}")
    return LayoutOptions(
        text_variant=default_variant if variant == "auto" else variant,
        figures=_choice(ctx, "figures", FIGURE_MODES, "clean"),
        background=_choice(ctx, "background", BACKGROUNDS, "blank"),
        show_boxes=ctx.flag("show_boxes", False),
        renderer=_choice(ctx, "renderer", RENDERERS, "auto"),
        pages=selected,
        browser=ctx.option("browser"),
    )


# ------------------------------------------------------------------------------------------------- planning
@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def w(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def h(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def area(self) -> float:
        return self.w * self.h

    def inter(self, o: Box) -> Box | None:
        b = Box(max(self.x0, o.x0), max(self.y0, o.y0), min(self.x1, o.x1), min(self.y1, o.y1))
        return b if b.w > 0.5 and b.h > 0.5 else None

    def copy(self) -> Box:
        return Box(self.x0, self.y0, self.x1, self.y1)

    def rect(self) -> pymupdf.Rect:
        return pymupdf.Rect(self.x0, self.y0, self.x1, self.y1)


@dataclass
class Item:
    el: Element
    kind: str  # title | heading | text | small | furniture | code | unclear | math | table | figure | placeholder
    group: str  # auto-fit group: body | small | heading | title | math | fixed | own | "" (no text)
    box: Box
    original: Box
    min_size: float = BODY_MIN
    max_size: float = BODY_MAX
    html: str = ""  # inner HTML of the box (Chromium renderer)
    plain: str = ""  # readable text (PyMuPDF renderer, verification)
    expected: str = ""  # words expected inside the box in the output (inline math removed)
    image: Path | None = None
    image_kind: str | None = None  # clean | original | svg | page_crop
    image_box: Box | None = None  # where the image is drawn (its natural size when that matches the box)
    whitened: bool = False  # paper whitened here because no cleaned crop was stored
    over_figure: bool = False
    adjusted: bool = False
    spread: bool = False
    scale: float = 1.0  # target size = the page's body size x scale
    floor: float = 0.75  # smallest size when the text must shrink, as a share of the body size
    grew: str = ""  # "right", "down" or "right+down" when the box was extended into free space
    # results
    status: str = ""  # fit | shrunk | overflow | image | placeholder
    size: float | None = None
    fit: float | None = None
    target: float | None = None
    tex_errors: int = 0
    ink: Box | None = None  # where the text really is after layout (Chromium renderer)

    @property
    def has_text(self) -> bool:
        return self.kind not in ("figure",)


@dataclass
class PagePlan:
    page: Page
    width: float
    height: float
    items: list[Item] = field(default_factory=list)
    unplaced: list[str] = field(default_factory=list)
    overlaps: list[str] = field(default_factory=list)
    body: float | None = None  # the body size the page was set at
    body_scope: str = "page"  # "document" when every page shares one body size


_CROSSED = re.compile(r"\[(?:crossed[ -]out|struck(?: through)?|strikethrough)\s*:\s*([^\]]*)\]", re.I)
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_SPACED_PUNCT = re.compile(r" (?=[:;!?)\]](?:\s|$|-))")
ONE_LINE_MAX_PT = 40.0  # a heading box lower than this holds one line: it is never wrapped
_BULLET = re.compile(r"^\s*(?:[•●▪◦‣∙·\-–*]|\(?\d{1,3}[.)]|\(?[a-z][.)]|\(?[ivx]{1,5}[.)])\s+", re.I)


_LEAD_BULLET = re.compile(r"^(\s*[•●▪◦‣∙·])\s+")
# A word the writer crossed out that the reader could not make out: a short struck gap, not the words "crossed out".
_CROSSED_BARE = re.compile(r"\[(?:crossed[ -]out|struck(?: through)?|strikethrough)\s*\]", re.I)
_GAP = "\ue000"  # private-use mark for that gap, so line handling does not take it for white space
_STRUCK_GAP = f"~~{_GAP}~~"
_LATEX_COMMAND = re.compile(r"\\[a-zA-Z]{2,}")
_DELIMITED = re.compile(r"\\(left|right|begin|end)(?![a-zA-Z])")
_RELATION = re.compile(r"\\Rightarrow(?![a-zA-Z])|\\approx(?![a-zA-Z])|\\quad(?![a-zA-Z])|=")


def _rich_segment(text: str) -> str:
    out = html.escape(text, quote=False)
    out = _SPACED_PUNCT.sub("&nbsp;", out)  # "T_B :" keeps its colon on the same line
    out = _LEAD_BULLET.sub(lambda m: m.group(1) + "&nbsp;", out)  # a bullet never ends a line on its own
    out = _CROSSED.sub(lambda m: f"<s>{m.group(1)}</s>", out)
    return _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", out)


def _plain_segment(text: str) -> str:
    text = _CROSSED.sub(lambda m: m.group(1), text)
    return _BOLD.sub(lambda m: m.group(1), text.replace("~~", "").replace(_GAP, ""))


def _tex_attr(tex: str) -> str:
    return html.escape(tex, quote=True)


def rich_line(line: str) -> tuple[str, str, str]:
    """(html, readable text, words outside math) for one line with inline ``$...$`` math. ``~~`` toggles a
    strike-through, which may enclose inline math; an unclosed one ends with the line."""
    parts_html, parts_plain, parts_words = [], [], []
    struck = False
    segs = list(split_inline_math(line))
    glued: dict[int, tuple[str, str]] = {}  # math index -> (text glued before, text glued after)
    for i, (_seg, is_math) in enumerate(segs):
        if not is_math:
            continue
        before = after = ""
        if i > 0 and not segs[i - 1][1]:
            # the word touching the math, or a lone leading bullet ("• $T = T(x)$")
            m = re.search(r"(?:^\s*[•●▪◦‣∙·]\s+)?[^\s~]*$", segs[i - 1][0])
            if m and m.group(0) and "~~" not in segs[i - 1][0][max(0, m.start() - 1):]:
                before = m.group(0)
                segs[i - 1] = (segs[i - 1][0][:m.start()], False)
        if i + 1 < len(segs) and not segs[i + 1][1]:
            m = re.match(r"[^\s~$]+", segs[i + 1][0])
            if m:
                after = m.group(0)
                segs[i + 1] = (segs[i + 1][0][m.end():], False)
        glued[i] = (before, after)
    for i, (seg, is_math) in enumerate(segs):
        if is_math:
            alt = latex_to_readable(seg)
            before, after = glued.get(i, ("", ""))
            span = f'<span class="im" data-tex="{_tex_attr(seg)}" data-alt="{_tex_attr(alt)}"></span>'
            if before or after:  # punctuation touching the math stays on its line: "(dx)", "T_B."
                span = f'<span class="nw">{_rich_segment(before)}{span}{_rich_segment(after)}</span>'
            # a strike-through does not reach into a formula's boxes: draw the line over it
            parts_html.append(f'<span class="sm">{span}</span>' if struck else span)
            parts_plain.append(_plain_segment(before) + alt + _plain_segment(after))
            if before or after:
                parts_words.append(_plain_segment(before + " " + after))
            continue
        pieces = seg.split("~~")
        for i, piece in enumerate(pieces):
            if i:
                parts_html.append("</s>" if struck else "<s>")
                struck = not struck
            if struck and piece == _GAP:
                parts_html.append('<span class="sm sgap"></span>')
                continue
            parts_html.append(_rich_segment(piece))
        parts_plain.append(_plain_segment(seg))
        parts_words.append(_plain_segment(seg))
    if struck:
        parts_html.append("</s>")
    return "".join(parts_html), "".join(parts_plain), " ".join(parts_words)


def _close_strikes_per_line(text: str) -> str:
    """A strike-through that spans lines (``~~a NEWLINE b~~``) is closed and reopened on each line."""
    def per_line(m: re.Match) -> str:
        return "\n".join(f"~~{ln}~~" if ln.strip() else ln for ln in m.group(1).split("\n"))
    return re.sub(r"~~(.+?)~~", per_line, text, flags=re.S)


def _normalise_strikes(text: str) -> str:
    """Every way a reading marks crossed-out writing becomes ``~~...~~``, which may enclose inline math."""
    text = _CROSSED.sub(lambda m: f"~~{m.group(1).strip()}~~", text)
    return _CROSSED_BARE.sub(_STRUCK_GAP, text)


def rich_text(text: str) -> tuple[str, str, str]:
    lines = [ln.strip() for ln in _close_strikes_per_line(_normalise_strikes(text)).splitlines()]
    lines = [ln for ln in lines if ln]
    done = [rich_line(ln) for ln in lines]
    return "<br>".join(d[0] for d in done), "\n".join(d[1] for d in done), " ".join(d[2] for d in done)


def _question_prefix(el: Element, text: str) -> tuple[str, str]:
    q = el.question
    lead = question_lead(q.number, text) if q else ""
    if not lead:
        return "", ""
    num = lead if lead.endswith((".", ")")) else lead + "."
    return f'<span class="qn">{html.escape(num)}</span>', num + " "


def _marks_suffix(el: Element, text: str) -> tuple[str, str]:
    q = el.question
    if q is None or not q.marks:
        return "", ""
    inner = q.marks.strip().strip("[]()").strip()
    if inner and re.search(rf"[\[(]\s*{re.escape(inner)}\b", text):
        return "", ""  # the marks are already part of the text
    label = marks_label(q.marks)
    return (f'<span class="marks">{html.escape(label)}</span>', " " + label) if label else ("", "")


def _text_item(el: Element, box: Box, variant: str) -> Item:
    t = el.type
    if is_unclear(el):
        msg = f"⟦unclear: {unclear_text(el)}⟧"
        return Item(el, "unclear", "fixed", box, box.copy(), html=html.escape(msg), plain=msg, expected=msg,
                    scale=SCALES["unclear"][0], floor=SCALES["unclear"][1])
    text = element_text(el, variant)
    if t == ElementType.TITLE or (t in HEADING_TYPES and heading_level(el) == 0):
        kind, group, mx, ratio = "title", "title", TITLE_MAX, "title"
    elif t in HEADING_TYPES:
        kind, group, mx = "heading", "heading", HEADING_MAX
        ratio = "subheading" if t == ElementType.SUBHEADING or heading_level(el) >= 2 else "heading"
    elif t in FURNITURE:
        kind, group, mx, ratio = "furniture", "small", BODY_MAX, "furniture"
    elif t in SMALL:
        kind, group, mx, ratio = ("caption" if t == ElementType.CAPTION else "small"), "small", BODY_MAX, "small"
    elif t == ElementType.CODE:
        kind, group, mx, ratio = "code", "code", BODY_MAX, "code"
    else:
        kind, group, mx, ratio = "text", "body", BODY_MAX, "text"
    scale, floor = SCALES[ratio]
    if t == ElementType.CODE:
        body = html.escape(el.content)
        return Item(el, kind, group, box, box.copy(), max_size=mx, html=body, plain=el.content, expected=el.content,
                    scale=scale, floor=floor)
    if el.items:
        rows_html, rows_plain, rows_words = [], [], []
        if text.strip():
            h, p, w = rich_text(text)
            rows_html.append(f"<div>{h}</div>")
            rows_plain.append(p)
            rows_words.append(w)
        lis = []
        bare = all(not _BULLET.match(i) for i in el.items)
        for it in el.items:
            h, p, w = rich_text(it)
            lis.append(f"<li>{h}</li>" if bare else f"<div>{h}</div>")
            rows_plain.append(("• " if bare else "") + p)
            rows_words.append(w)
        rows_html.append(f"<ul>{''.join(lis)}</ul>" if bare else "".join(lis))
        body_html, plain, words = "".join(rows_html), "\n".join(rows_plain), " ".join(rows_words)
    else:
        body_html, plain, words = rich_text(text)
    qh, qp = _question_prefix(el, text)
    mh, mp = _marks_suffix(el, text)
    return Item(el, kind, group, box, box.copy(), max_size=mx, html=mh + qh + body_html, plain=qp + plain + mp,
                expected=qp + words + mp, spread=kind in ("text", "small"), scale=scale, floor=floor)


def is_bare_latex(text: str) -> bool:
    """A whole text that is LaTeX written without ``$``. Prose with a command in it is not."""
    t = text.strip()
    if not t or "$" in t or "~~" in t or not _LATEX_COMMAND.search(t):
        return False
    rest = re.sub(r"[_^]\{[^}]*\}|[_^]\w", " ", re.sub(r"\\[a-zA-Z]+", " ", t))
    return not re.search(r"[A-Za-z]{3,}\s+[A-Za-z]{3,}", rest)


def split_at_relations(tex: str) -> list[str]:
    """Break an equation before its top-level ``=``, ``\\Rightarrow``, ``\\approx`` and ``\\quad``, so a line too long
    for the page can wrap there. Fractions, brackets and environments stay whole. [] when there is nowhere to break."""
    parts: list[str] = []
    start = depth = i = 0
    while i < len(tex):
        c = tex[i]
        if c == "\\":
            m = _DELIMITED.match(tex, i)
            if m:
                depth += 1 if m.group(1) in ("left", "begin") else -1
                i = m.end()
                continue
            if depth == 0 and i > start and _RELATION.match(tex, i):
                parts.append(tex[start:i])
                start = i
            i += 2 if i + 1 < len(tex) and not tex[i + 1].isalpha() else 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "=" and depth == 0 and i > start:
            parts.append(tex[start:i])
            start = i
        i += 1
    parts.append(tex[start:])
    parts = [p.strip() for p in parts if p.strip()]
    return parts if len(parts) > 1 else []


def _math_item(el: Element, box: Box, tex: str | None = None) -> Item:
    tex = tex or math_latex(el)
    if tex is None:  # only a linearised reading exists
        h, p, w = rich_text(el.content)
        return Item(el, "text", "body", box, box.copy(), html=f"<i>{h}</i>" if h else "", plain=p, expected=w)
    alt = el.content.strip() or latex_to_readable(tex)
    parts = split_at_relations(tex)
    wrap = f' data-parts="{_tex_attr(json.dumps(parts))}"' if parts else ""
    body = f'<div class="dm" data-tex="{_tex_attr(tex)}" data-alt="{_tex_attr(alt)}"{wrap}></div>'
    return Item(el, "math", "math", box, box.copy(), min_size=MATH_MIN, max_size=MATH_MAX, html=body,
                plain=latex_to_readable(tex), expected="", scale=SCALES["math"][0], floor=SCALES["math"][1])


def _cell_html(text: str, latex: str | None) -> tuple[str, str]:
    if latex and latex.strip():
        tex = _base.clean_latex(latex)
        return (f'<span class="im" data-tex="{_tex_attr(tex)}" data-alt="{_tex_attr(text or tex)}"></span>',
                latex_to_readable(tex))
    h, p, _ = rich_text(text)
    return h, p


def _table_item(el: Element, box: Box) -> Item:
    table = el.table
    assert table is not None
    lay = layout_table(table)
    rows, plain_rows, words = [], [], []
    for r in range(lay.n_rows):
        cells, plain = [], []
        for c in range(lay.n_cols):
            p = lay.anchor_at(r, c)
            if p is None:
                continue
            h, txt = _cell_html(p.cell.text, p.cell.latex)
            cls = " ".join(x for x in ("h" if (p.cell.is_header or r < lay.header_rows) else "",
                                       "u" if p.cell.uncertain else "") if x)
            span = (f' rowspan="{p.row_span}"' if p.row_span > 1 else "") + (
                f' colspan="{p.col_span}"' if p.col_span > 1 else "")
            cells.append(f'<td{span}{f" class={chr(34)}{cls}{chr(34)}" if cls else ""}>{h}</td>')
            plain.append(txt)
            if not (p.cell.latex and p.cell.latex.strip()):
                words.append(_plain_segment(p.cell.text))
        rows.append(f"<tr>{''.join(cells)}</tr>")
        plain_rows.append(" | ".join(plain))
    cap = ""
    if table.caption or table.label:
        cap_text = " ".join(x for x in (table.label, table.caption) if x)
        cap = f'<div class="cap">{html.escape(cap_text)}</div>'
    body = f"{cap}<table>{''.join(rows)}</table>"
    return Item(el, "table", "own", box, box.copy(), min_size=TABLE_MIN, max_size=TABLE_MAX, html=body,
                plain="\n".join(plain_rows), expected=" ".join(words))


def _page_image(ctx: ExportContext, page: Page) -> Path | None:
    for rel in (page.original_image, page.processed_image):
        if not rel:
            continue
        try:
            path = ctx.resolve(rel)
        except ExportError:
            continue
        if path.is_file():
            return path
    return None


def _crop_from_page(ctx: ExportContext, page: Page, box: Box, dest: Path) -> Path | None:
    src = _page_image(ctx, page)
    if src is None or page.width_pt <= 0 or page.height_pt <= 0:
        return None
    with Image.open(src) as img:
        sx, sy = img.width / page.width_pt, img.height / page.height_pt
        px = (int(box.x0 * sx), int(box.y0 * sy), int(round(box.x1 * sx)), int(round(box.y1 * sy)))
        if px[2] - px[0] < 2 or px[3] - px[1] < 2:
            return None
        crop = img.crop(px)
        if crop.mode not in ("RGB", "L"):
            crop = crop.convert("RGB")
        dest.parent.mkdir(parents=True, exist_ok=True)
        crop.save(dest, format="PNG")
    return dest


def whiten_image(src: Path, dest: Path) -> Path | None:
    """Fallback paper whitening for a photographed crop when no cleaned crop was stored: divide out the
    paper/shadow background and stretch the levels so the paper turns white while every stroke keeps its
    colour. It is deliberately conservative (ruled lines stay, faintly); the pipeline's cleaned crop
    (``figure.image_clean``) removes them properly. Returns None when the image cannot be read."""
    try:
        with Image.open(src) as im:
            rgb = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    except (OSError, ValueError):
        return None
    h, w = rgb.shape[:2]
    if h < 8 or w < 8:
        return None
    k = max(9, (min(h, w) // 12) | 1)
    bg = cv2.dilate(rgb, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    bg = cv2.GaussianBlur(bg, (0, 0), k / 2)
    norm = np.clip(rgb / np.maximum(bg, 1e-3), 0.0, 1.0)
    norm = np.clip((norm - 0.04) / 0.86, 0.0, 1.0) ** 1.6  # paper -> white, strokes kept and firmed up
    dest.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray((norm * 255).astype(np.uint8)).save(dest, format="PNG")
    return dest


def _page_dpi(ctx: ExportContext, page: Page) -> float | None:
    src = _page_image(ctx, page)
    if src is None or page.width_pt <= 0:
        return None
    try:
        with Image.open(src) as im:
            return im.width / page.width_pt * 72.0
    except OSError:
        return None


def image_placement(ctx: ExportContext, page: Page, box: Box, path: Path) -> Box:
    """Draw a crop at its natural size (centred on the box) when it was cut from the page render at the page's
    resolution with a small margin; otherwise fit it inside the box."""
    if path.suffix.lower() == ".svg":
        return box.copy()
    dpi = _page_dpi(ctx, page)
    try:
        with Image.open(path) as im:
            px = im.size
    except OSError:
        return box.copy()
    if not dpi:
        return box.copy()
    w, h = px[0] * 72.0 / dpi, px[1] * 72.0 / dpi
    if box.w * 0.85 <= w <= box.w + NATURAL_SLACK_PT and box.h * 0.85 <= h <= box.h + NATURAL_SLACK_PT:
        cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
        return Box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
    return box.copy()


def _figure_kind(el: Element, path: Path, doc_dir: Path) -> str:
    fig = el.figure
    rel = path.resolve()
    for kind, ref in (("svg", getattr(fig, "svg", None) if fig else None),
                      ("clean", getattr(fig, "image_clean", None) if fig else None),
                      ("original", el.source.region_image)):
        if ref and (Path(doc_dir) / ref).resolve() == rel:
            return kind
    return "svg" if path.suffix.lower() == ".svg" else "original"


def _exact_placement(el: Element, kind: str | None) -> Box | None:
    """The page rectangle stored with a cleaned crop."""
    placed = el.figure.image_clean_box if el.figure else None
    if kind != "clean" or not placed or len(placed) != 4:
        return None
    return Box(*placed)


def _figure_item(ctx: ExportContext, page: Page, el: Element, box: Box, mode: str, assets: Path) -> Item:
    path = figure_image_path(ctx.doc_dir, el, mode)
    whole_pages = {(Path(ctx.doc_dir) / rel).resolve()
                   for rel in (page.original_image, page.processed_image, el.source.page_image) if rel}
    if path is not None and path in whole_pages:
        path = None  # a whole-page image recorded as the crop: never show the whole page as a figure
    kind = _figure_kind(el, path, ctx.doc_dir) if path is not None else None
    if path is None:
        path = _crop_from_page(ctx, page, box, assets / f"{el.element_id}.png")
        kind = "page_crop" if path is not None else None
    if path is not None:
        whitened = False
        if mode == "clean" and kind in ("original", "page_crop") and path.suffix.lower() in (".png", ".jpg", ".jpeg"):
            out = whiten_image(path, assets / f"{el.element_id}.white.png")
            if out is not None:
                path, whitened = out, True
        return Item(el, "figure", "", box, box.copy(), image=path, image_kind=kind, status="image",
                    image_box=_exact_placement(el, kind) or image_placement(ctx, page, box, path),
                    whitened=whitened)
    fig = el.figure
    label = (fig.figure_label if fig else None) or "Figure"
    desc = ((fig.description if fig else "") or el.content or "figure (no image stored)").strip()
    if fig and fig.labels and not desc.endswith((".", "!", "?", ":")):
        desc += "."
    extra = f" Labels: {', '.join(fig.labels)}." if fig and fig.labels else ""
    body = f'<span class="lbl">{html.escape(label)}</span><br>{html.escape(desc + extra)}'
    return Item(el, "placeholder", "own", box, box.copy(), min_size=4.0, max_size=14.0, html=body,
                plain=f"{label}: {desc}{extra}", expected="")


def plan_page(ctx: ExportContext, page: Page, opts: LayoutOptions, assets: Path) -> PagePlan:
    width, height = page.width_pt, page.height_pt
    if width <= 0 or height <= 0:
        entry = next((e for e in ctx.manifest.pages if e.page_number == page.page_number), None)
        if entry is None:
            raise ExportError(f"Page {page.page_number} has no size")
        width, height = entry.width_pt, entry.height_pt
    plan = PagePlan(page, width, height)
    for el in ordered_elements(page):
        if el.bbox is None:
            if el.content.strip() or el.items or el.table or el.latex:
                plan.unplaced.append(el.element_id)
            continue
        b = el.bbox.clamp(width, height)
        box = Box(b.x0, b.y0, b.x1, b.y1)
        if box.w < MIN_BOX_PT or box.h < MIN_BOX_PT:
            plan.unplaced.append(el.element_id)
            continue
        if el.type in VISUAL_TYPES:
            item = _figure_item(ctx, page, el, box, opts.figures, assets)
        elif el.type in MATH_TYPES and not is_unclear(el):
            item = _math_item(el, box)
        elif el.type == ElementType.TABLE and el.table is not None and el.table.rows:
            item = _table_item(el, box)
        elif is_bare_latex(element_text(el, opts.text_variant)) and not (el.items or is_unclear(el)):
            item = _math_item(el, box, _base.clean_latex(element_text(el, opts.text_variant)))
        else:
            item = _text_item(el, box, opts.text_variant)
            if not item.html.strip():
                continue
        plan.items.append(item)
    resolve_overlaps(plan)
    return plan


# ------------------------------------------------------------------------------------------------- overlaps
def _contained(a: Box, b: Box, inter: Box) -> float:
    """Share of the smaller of a, b covered by their intersection."""
    small = min(a.area, b.area)
    return inter.area / small if small > 0 else 1.0


def _trim_out(t: Item, f: Box) -> bool:
    """Trim text box ``t`` so it no longer overlaps ``f``, if that keeps enough of it. True when trimmed.
    Cutting the top or bottom is preferred: lines of text then keep their full width."""
    b = t.box
    vertical, horizontal = [], []
    if b.y0 < f.y0 < b.y1:
        vertical.append(Box(b.x0, b.y0, b.x1, f.y0))
    if b.y0 < f.y1 < b.y1:
        vertical.append(Box(b.x0, f.y1, b.x1, b.y1))
    if b.x0 < f.x0 < b.x1:
        horizontal.append(Box(b.x0, b.y0, f.x0, b.y1))
    if b.x0 < f.x1 < b.x1:
        horizontal.append(Box(f.x1, b.y0, b.x1, b.y1))

    def ok(o: Box) -> bool:
        return (o.inter(f) is None and o.area >= KEEP_AFTER_TRIM * b.area and o.h >= KEEP_AFTER_TRIM * b.h
                and o.w >= KEEP_AFTER_TRIM * b.w)

    options = [o for o in vertical if ok(o)] or [o for o in horizontal if ok(o)]
    if not options:
        return False
    t.box = max(options, key=lambda o: o.area)
    t.adjusted = True
    return True


def _split(a: Item, b: Item, inter: Box) -> None:
    """Split the overlap band between two text boxes: stacked boxes share it vertically, side-by-side ones
    horizontally."""
    if inter.w >= inter.h:  # stacked
        upper, lower = (a, b) if (a.box.y0, a.box.y1) <= (b.box.y0, b.box.y1) else (b, a)
        mid = (inter.y0 + inter.y1) / 2
        if mid - upper.box.y0 >= MIN_BOX_PT and lower.box.y1 - mid >= MIN_BOX_PT:
            upper.box.y1 = mid
            lower.box.y0 = mid
            upper.adjusted = lower.adjusted = True
    else:
        left, right = (a, b) if a.box.x0 <= b.box.x0 else (b, a)
        mid = (inter.x0 + inter.x1) / 2
        if mid - left.box.x0 >= MIN_BOX_PT and right.box.x1 - mid >= MIN_BOX_PT:
            left.box.x1 = mid
            right.box.x0 = mid
            left.adjusted = right.adjusted = True


def _pen_under(fig: Item, region: Box) -> bool:
    """Whether a figure has strokes in this part of the page. Only a cleaned crop can tell (white is paper there);
    any other image counts as covering its whole box."""
    if fig.image_kind != "clean" or fig.image is None or fig.image_box is None:
        return True
    ib = fig.image_box
    try:
        with Image.open(fig.image) as im:
            grey = np.asarray(im.convert("L"))
    except OSError:
        return True
    sx, sy = grey.shape[1] / max(ib.w, 0.01), grey.shape[0] / max(ib.h, 0.01)
    x0, x1 = max(0, int((region.x0 - ib.x0) * sx)), max(0, int((region.x1 - ib.x0) * sx) + 1)
    y0, y1 = max(0, int((region.y0 - ib.y0) * sy)), max(0, int((region.y1 - ib.y0) * sy) + 1)
    part = grey[y0:y1, x0:x1]
    return bool(part.size) and bool((part < 200).any())


def resolve_overlaps(plan: PagePlan) -> None:
    """Keep boxes from overprinting: text boxes that overlap share the overlap band; a text box overlapping a
    figure is trimmed out of it (figures keep their size). A text box nested in another text box is cut out of
    the larger one when it sits at its edge. Otherwise nested boxes are left in place: the inner text gets a
    light backing so it stays legible, and nested text boxes are reported."""
    items = sorted(plan.items, key=lambda i: (i.box.y0, i.box.x0))
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            inter = a.box.inter(b.box)
            if inter is None:
                continue
            fa, fb = a.kind == "figure", b.kind == "figure"
            if fa and fb:
                continue
            if fa or fb:
                t, f = (b, a) if fa else (a, b)
                if not _pen_under(f, inter):
                    continue  # an empty corner of the figure's rectangle: nothing to avoid
                if inter.area >= IN_FIGURE * t.box.area:
                    t.over_figure = True
                elif not _trim_out(t, f.box):
                    t.over_figure = True
                continue
            if _contained(a.box, b.box, inter) >= NESTED:
                big, small = (a, b) if a.box.area >= b.box.area else (b, a)
                if not _trim_out(big, small.box):  # nested in the middle: keep it legible and report it
                    small.over_figure = True
                    plan.overlaps.append(f"{a.el.element_id}~{b.el.element_id}")
                continue
            _split(a, b, inter)


# ------------------------------------------------------------------------------------------------- Chromium
def _box_style(b: Box) -> str:
    return f"left:{b.x0:.2f}pt;top:{b.y0:.2f}pt;width:{b.w:.2f}pt;height:{b.h:.2f}pt"


def _short(eid: str) -> str:
    return eid.rsplit("-", 1)[-1]


def item_html(item: Item, assets: Path) -> str:
    style = _box_style(item.box)
    attrs = f'data-eid="{html.escape(item.el.element_id)}" data-short="{html.escape(_short(item.el.element_id))}"'
    if item.kind == "figure":
        assert item.image is not None
        rel = item.image.as_uri()
        tag = f"{_short(item.el.element_id)} {item.image_kind}" + (" whitened" if item.whitened else "")
        ib = item.image_box or item.box
        img_style = (f"position:absolute;left:{ib.x0 - item.box.x0:.2f}pt;top:{ib.y0 - item.box.y0:.2f}pt;"
                     f"width:{ib.w:.2f}pt;height:{ib.h:.2f}pt")
        return (f'<div class="el k-figure" {attrs} data-status="image" data-kind="{item.image_kind}" '
                f'data-tag="{tag}" style="{style}"><img alt="" src="{html.escape(rel)}" style="{img_style}"></div>')
    one_line = (item.kind in ("title", "heading", "furniture") and item.box.h < ONE_LINE_MAX_PT
                and "<br>" not in item.html)
    cls = (f"el k-{item.kind}" + (" over-figure" if item.over_figure else "")
           + (" one-line" if one_line else ""))
    fit = (f'data-min="{item.min_size}" data-max="{item.max_size}" data-group="{item.group}" '
           f'data-scale="{item.scale}" data-floor="{item.floor}"')
    if item.spread:
        fit += " data-spread"
    lang = f' lang="{html.escape(item.el.language)}"' if item.el.language else ""
    return f'<div class="{cls}" {attrs} {fit}{lang} style="{style}"><div class="c">{item.html}</div></div>'


def render_chromium(plans: list[PagePlan], browser: layout_html.Browser, work: Path, *, title: str,
                    lang: str | None, opts: LayoutOptions) -> tuple[Path, list[str]]:
    pages_html = [("".join(item_html(it, work) for it in plan.items), plan.width, plan.height) for plan in plans]
    dev = any(layout_html.DEVANAGARI.search(it.plain + it.html) for plan in plans for it in plan.items)
    doc, fonts = layout_html.build_document_html(pages_html, title=title, lang=lang,
                                                 paper=opts.background == "paper", show_boxes=opts.show_boxes,
                                                 devanagari=dev)
    src = work / "layout.html"
    src.write_text(doc, encoding="utf-8")
    dom = layout_html.lay_out(browser, src, work)
    results = layout_html.fit_results(dom)
    bodies = layout_html.body_sizes(dom)
    for idx, plan in enumerate(plans):
        if idx < len(bodies["pages"]):
            plan.body = bodies["pages"][idx]
        if idx < len(bodies["scopes"]):
            plan.body_scope = bodies["scopes"][idx]
        for it in plan.items:
            r = results.get(it.el.element_id, {})
            if it.kind == "figure":
                continue
            it.status = r.get("status", "")
            for name in ("size", "fit", "target"):
                try:
                    setattr(it, name, float(r[name]))
                except (KeyError, ValueError):
                    pass
            it.tex_errors = int(r.get("texerr") or 0)
            it.grew = r.get("grew", "")
            if r.get("ink"):
                try:
                    it.ink = Box(*(float(v) for v in r["ink"].split(",")))
                except (TypeError, ValueError):
                    pass
            if r.get("box"):
                try:
                    x0, y0, x1, y1 = (float(v) for v in r["box"].split(","))
                    it.box = Box(x0, y0, x1, y1)
                except ValueError:
                    pass
    fitted = work / "layout.fitted.html"
    fitted.write_text(layout_html.static_dom(dom), encoding="utf-8")
    pdf = work / "chromium.pdf"
    layout_html.print_pdf(browser, fitted, pdf, work)
    return pdf, fonts


# ------------------------------------------------------------------------------------------------- PyMuPDF
_FALLBACK_CSS = ("* {margin:0;padding:0} body {font-family: serif; line-height: 1.22} "
                 "table {border-collapse: collapse} td {border: 0.5pt solid #333; padding: 1pt 2pt} "
                 ".h {font-weight: bold} .k-title, .k-heading {font-weight: bold} .k-caption {font-style: italic} "
                 ".k-unclear {color: #666; font-style: italic} .k-furniture {color: #333}")


def _fallback_html(item: Item, size: float) -> str:
    if item.kind == "table":
        assert item.el.table is not None
        lay = layout_table(item.el.table)
        rows = []
        for r in range(lay.n_rows):
            cells = []
            for c in range(lay.n_cols):
                p = lay.anchor_at(r, c)
                if p is None:
                    continue
                txt = latex_to_readable(p.cell.latex) if p.cell.latex else _plain_segment(p.cell.text)
                span = (f' rowspan="{p.row_span}"' if p.row_span > 1 else "") + (
                    f' colspan="{p.col_span}"' if p.col_span > 1 else "")
                cls = ' class="h"' if (p.cell.is_header or r < lay.header_rows) else ""
                cells.append(f"<td{span}{cls}>{html.escape(txt)}</td>")
            rows.append(f"<tr>{''.join(cells)}</tr>")
        inner = f"<table>{''.join(rows)}</table>"
    else:
        text = html.escape(item.plain).replace("\n", "<br>")
        inner = f"<i>{text}</i>" if item.kind == "math" else text
    return f'<div class="k-{item.kind}" style="font-size:{size:.2f}pt">{inner}</div>'


def _used_height(item: Item, size: float, w: float, h: float) -> float | None:
    """Height the item's text takes at ``size`` in a box ``w`` wide (None when it does not fit in ``h``)."""
    scratch = pymupdf.open()
    try:
        page = scratch.new_page(width=w + 20, height=h + 20)
        spare, _ = page.insert_htmlbox(pymupdf.Rect(0, 0, w, h), _fallback_html(item, size), css=_FALLBACK_CSS,
                                       scale_low=1)
        return None if spare < 0 else h - spare
    finally:
        scratch.close()


def _fit_fallback(item: Item, lo: float | None = None, hi: float | None = None, box: Box | None = None
                  ) -> tuple[float, bool]:
    """Largest size in [lo, hi] (default: the item's limits) at which the item fits ``box`` (default: its box)."""
    lo = item.min_size if lo is None else lo
    hi = max(lo, item.max_size if hi is None else hi)
    b = box or item.box
    scratch = pymupdf.open()
    try:
        page = scratch.new_page(width=b.w + 20, height=b.h + 20)
        spare, scale = page.insert_htmlbox(pymupdf.Rect(0, 0, b.w, b.h), _fallback_html(item, hi), css=_FALLBACK_CSS,
                                           scale_low=lo / hi)
        if spare < 0:
            return lo, False
        return round(hi * scale, 2), True
    finally:
        scratch.close()


def _quantile(values: list[float], q: float) -> float:
    v = sorted(values)
    if not v:
        return 0.0
    i = (len(v) - 1) * q
    lo, hi = int(i), min(len(v) - 1, int(i) + 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def _free_below(r: Box, others: list[Box], height: float) -> float:
    lim = height - FREE_MARGIN_PT
    for o in others:
        if o.x1 > r.x0 + 1 and o.x0 < r.x1 - 1 and o.y0 >= r.y1 - 0.5:
            lim = min(lim, o.y0 - FREE_GAP_PT)
    return max(lim, r.y1)


def _free_right(r: Box, others: list[Box], width: float) -> float:
    lim = width - FREE_MARGIN_PT
    for o in others:
        if o.y1 > r.y0 + 1 and o.y0 < r.y1 - 1 and o.x0 >= r.x1 - 0.5:
            lim = min(lim, o.x0 - FREE_GAP_PT)
    return max(lim, r.x1)


def size_plans_fallback(plans: list[PagePlan]) -> None:
    """The browser's sizing (``layout_html.FIT_JS``) for the PyMuPDF renderer: one body size, fixed ratios, boxes
    extended into free space before the text is shrunk."""
    params = layout_html.FIT_PARAMS
    page_fits: list[list[float]] = []
    for plan in plans:
        fits = []
        for it in plan.items:
            if it.group == "body" and not it.over_figure:
                fit, ok = _fit_fallback(it)
                it.fit = fit
                if ok:
                    fits.append(fit)
        page_fits.append(fits)
    page_bodies = [_quantile(fs, params["bodyQuantile"]) if fs else 0.0 for fs in page_fits]
    known = [b for b in page_bodies if b]
    doc_body = _quantile(known, 0.5) if known else params["defaultBody"]
    doc_body = max(params["bodyMin"], min(doc_body, params["bodyMax"]))
    for plan, own in zip(plans, page_bodies):
        near = not own or abs(own - doc_body) <= params["pageTolerance"] * doc_body
        body = doc_body if near else max(params["bodyMin"], min(own, params["bodyMax"]))
        plan.body, plan.body_scope = round(body, 2), "document" if near else "page"
        boxes = {id(it): it.box for it in plan.items}
        for it in sorted((i for i in plan.items if i.kind != "figure"), key=lambda i: (i.box.y0, i.box.x0)):
            target = max(it.min_size, min(body * it.scale, it.max_size))
            floor = max(it.min_size, min(target, body * it.floor))
            it.target = round(target, 2)
            if it.group == "own" or it.over_figure:
                size, ok = _fit_fallback(it, it.min_size, target)
                it.size, it.fit = size, size
                it.status = "overflow" if not ok else ("shrunk" if size < target - 0.05 else "fit")
                continue
            if _used_height(it, target, it.box.w, it.box.h) is not None:
                it.size, it.status = target, "fit"
                continue
            r, grew = it.box.copy(), []
            others = [b for k, b in boxes.items() if k != id(it) and b.inter(it.box) is None]
            if it.kind == "math" or r.h < target * params["leading"] * 1.8:
                right = _free_right(r, others, plan.width)
                if right > r.x1 + 0.5:
                    r.x1 = right
                    grew.append("right")
            if _used_height(it, target, r.w, r.h) is None:
                below = _free_below(r, others, plan.height)
                used = _used_height(it, target, r.w, below - r.y0) if below > r.y1 + 0.5 else None
                if used is not None:
                    r.y1 = max(r.y1, min(below, r.y0 + used + 1))
                    grew.append("down")
                elif below > r.y1 + 0.5:
                    r.y1 = below
                    grew.append("down")
            if grew:
                it.box, it.grew = r, "+".join(grew)
                boxes[id(it)] = r
            if _used_height(it, target, r.w, r.h) is not None:
                it.size, it.status = target, "moved" if grew else "fit"
            else:
                size, ok = _fit_fallback(it, floor, target, r)
                same = size >= target * params["sameSize"]
                it.size, it.status = (size, ("moved" if grew else "fit") if same else "shrunk") if ok else (floor, "overflow")


def render_pymupdf(plans: list[PagePlan], work: Path, opts: LayoutOptions) -> Path:
    size_plans_fallback(plans)
    out = pymupdf.open()
    try:
        for plan in plans:
            page = out.new_page(width=plan.width, height=plan.height)
            if opts.background == "paper":
                page.draw_rect(page.rect, color=None, fill=(0.984, 0.973, 0.937), overlay=False)
            for it in plan.items:
                if it.kind == "figure":
                    assert it.image is not None
                    if it.image.suffix.lower() == ".svg":
                        svg = pymupdf.open(it.image)
                        try:
                            pdfbytes = svg.convert_to_pdf()
                        finally:
                            svg.close()
                        with pymupdf.open("pdf", pdfbytes) as src:
                            page.show_pdf_page(it.box.rect(), src, 0)
                    else:
                        page.insert_image((it.image_box or it.box).rect(), filename=str(it.image),
                                          keep_proportion=True)
                    continue
                size = it.size or it.min_size
                rect = it.box.rect()
                if it.status == "overflow":
                    rect = pymupdf.Rect(rect.x0, rect.y0, rect.x1, plan.height)  # overflow downward
                if it.kind == "placeholder":
                    page.draw_rect(it.box.rect(), color=(0.55, 0.55, 0.55), width=0.75, dashes="[3 2] 0")
                if it.over_figure:
                    page.draw_rect(it.box.rect(), color=None, fill=(1, 1, 1), fill_opacity=0.82)
                page.insert_htmlbox(rect, _fallback_html(it, size), css=_FALLBACK_CSS, scale_low=0)
            if opts.show_boxes:
                for it in plan.items:
                    color = {"shrunk": (0.86, 0.5, 0), "overflow": (0.86, 0, 0)}.get(it.status, (0.8, 0.16, 0.16))
                    page.draw_rect(it.box.rect(), color=color, width=0.4, dashes="[2 1] 0")
        pdf = work / "pymupdf.pdf"
        out.save(pdf, garbage=3, deflate=True)
        return pdf
    finally:
        out.close()


# ------------------------------------------------------------------------------------------------- export
_WORD = re.compile(r"\w+")


def _tokens(text: str) -> Counter[str]:
    return Counter(_WORD.findall(text.casefold()))


def _finalise(raw_pdf: Path, plans: list[PagePlan], out_path: Path, title: str, warnings: list[str]) -> None:
    """Exact page boxes, metadata and a compact save."""
    with pymupdf.open(raw_pdf) as doc:
        while doc.page_count > len(plans) and not doc[-1].get_text().strip() and not doc[-1].get_images():
            doc.delete_page(-1)  # a trailing blank page from the browser's pagination
        if doc.page_count != len(plans):
            warnings.append(f"Layout PDF has {doc.page_count} pages, expected {len(plans)}")
        for page, plan in zip(doc, plans):
            if abs(page.rect.width - plan.width) > 0.01 or abs(page.rect.height - plan.height) > 0.01:
                # Chromium rounds the page size to its device units (842 pt -> 841.92 pt); restore it exactly
                page.set_mediabox(pymupdf.Rect(0, 0, plan.width, plan.height))
        doc.set_metadata({"title": title, "producer": "ScholarOCR layout-pdf", "creator": "ScholarOCR"})
        doc.save(out_path, garbage=3, deflate=True)


def _glyphs(page: pymupdf.Page) -> list[tuple[float, float, float, float]]:
    """Each printed character's box, narrowed to the middle of its height: a font's line box is far taller than the
    glyph, and a fraction's numerator would otherwise touch its denominator."""
    out = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for ch in span["chars"]:
                    if not ch["c"].strip():
                        continue
                    x0, y0, x1, y1 = ch["bbox"]
                    mid, half = (y0 + y1) / 2, (y1 - y0) * 0.2
                    if x1 - x0 > 0.2 and half > 0:
                        out.append((x0, mid - half, x1, mid + half))
    return out


def _holders(items: list[Item], cx: float, cy: float) -> list[Item]:
    """The text elements whose laid-out text holds this point, smallest first."""
    found = []
    for it in items:
        b = it.ink or it.box
        if b.x0 - 1 <= cx <= b.x1 + 1 and b.y0 - 1 <= cy <= b.y1 + 1:
            found.append(it)
    return sorted(found, key=lambda it: (it.ink or it.box).area)


def _owner(items: list[Item], cx: float, cy: float) -> Item | None:
    """The smallest text element whose laid-out text holds this point."""
    found = _holders(items, cx, cy)
    return found[0] if found else None


def _overprinted(page: pymupdf.Page, plan: PagePlan) -> list[str]:
    """Pairs of elements whose printed characters land on each other, or on the pen strokes of a cropped figure."""
    texts = [it for it in plan.items if it.has_text]  # with text set over a figure: its backing hides nothing fully
    glyphs = _glyphs(page)
    if not glyphs:
        return []
    arr = np.asarray(glyphs)
    holders = [_holders(texts, (g[0] + g[2]) / 2, (g[1] + g[3]) / 2) for g in glyphs]
    owners = [h[0] if h else None for h in holders]
    hits: Counter[tuple[str, str]] = Counter()
    areas = (arr[:, 2] - arr[:, 0]) * (arr[:, 3] - arr[:, 1])
    for i in range(len(glyphs) - 1):
        ix = np.minimum(arr[i, 2], arr[i + 1:, 2]) - np.maximum(arr[i, 0], arr[i + 1:, 0])
        iy = np.minimum(arr[i, 3], arr[i + 1:, 3]) - np.maximum(arr[i, 1], arr[i + 1:, 1])
        shared = np.where((ix > 0) & (iy > 0), ix * iy, 0.0)
        for off in np.flatnonzero(shared >= 0.35 * np.minimum(areas[i], areas[i + 1:])):
            # Characters landing on each other where two elements' text overlap: both may sit in the area of the
            # same smaller element, so it takes every element there to name the pair. Within one element they do not
            # count (a formula's accents).
            there = {it.el.element_id for it in holders[i]} | {it.el.element_id for it in holders[i + 1 + int(off)]}
            if len(there) >= 2:
                hits[tuple(sorted(there)[:2])] += 1  # type: ignore[arg-type]
    for fig in plan.items:
        if fig.kind != "figure" or fig.image is None or fig.image.suffix.lower() == ".svg":
            continue
        ib = fig.image_box or fig.box
        try:
            with Image.open(fig.image) as im:
                grey = np.asarray(im.convert("L"))
        except OSError:
            continue
        sx, sy = grey.shape[1] / max(ib.w, 0.01), grey.shape[0] / max(ib.h, 0.01)
        for g, own in zip(glyphs, owners):
            if own is None:
                continue
            x0, y0 = int((g[0] - ib.x0) * sx), int((g[1] - ib.y0) * sy)
            x1, y1 = int((g[2] - ib.x0) * sx) + 1, int((g[3] - ib.y0) * sy) + 1
            if x1 <= 0 or y1 <= 0 or x0 >= grey.shape[1] or y0 >= grey.shape[0]:
                continue
            region = grey[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
            if region.size and float((region < 170).mean()) >= 0.12:
                hits[tuple(sorted((own.el.element_id, fig.el.element_id)))] += 1  # type: ignore[arg-type]
    return sorted("~".join(k) for k in hits)


def _clipped(plan: PagePlan) -> list[str]:
    """Elements whose laid-out text passes the edge of the page, where the page cuts it off."""
    out = []
    for it in plan.items:
        b = it.ink
        if b is not None and it.has_text and (b.x0 < -0.5 or b.y0 < -0.5 or b.x1 > plan.width + 0.5
                                              or b.y1 > plan.height + 0.5):
            out.append(it.el.element_id)
    return out


def _raw_latex(page: pymupdf.Page, plan: PagePlan) -> list[str]:
    """Elements that printed LaTeX source as text."""
    out = []
    for w in page.get_text("words"):
        if _LATEX_COMMAND.search(w[4]):
            it = _owner(plan.items, (w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
            eid = it.el.element_id if it else f"page {plan.page.page_number}"
            if eid not in out:
                out.append(eid)
    return out


def _verify(out_path: Path, plans: list[PagePlan], renderer: str, warnings: list[str]) -> dict[str, Any]:
    per_page: list[dict[str, Any]] = []
    overprinted: list[str] = []
    raw_latex: list[str] = []
    clipped: list[str] = []
    sizes_ok = True
    with pymupdf.open(out_path) as doc:
        count_ok = doc.page_count == len(plans)
        fonts = sorted({f[3].split("+", 1)[-1] for p in doc for f in p.get_fonts()} - {""})
        matched_all = total_all = 0
        outside: list[str] = []
        for idx, plan in enumerate(plans):
            if idx >= doc.page_count:
                break
            page = doc[idx]
            ok = (abs(page.rect.width - plan.width) <= SIZE_TOLERANCE_PT
                  and abs(page.rect.height - plan.height) <= SIZE_TOLERANCE_PT)
            sizes_ok &= ok
            words = page.get_text("words")
            row: dict[str, Any] = {"page_number": plan.page.page_number, "size_ok": ok,
                                   "width_pt": round(page.rect.width, 2), "height_pt": round(page.rect.height, 2),
                                   "elements": len(plan.items)}
            m_page = t_page = 0
            for it in plan.items:
                exp = _tokens(it.expected)
                total = sum(exp.values())
                if not total:
                    continue
                b = it.box
                bottom = plan.height if it.status == "overflow" else b.y1 + 2
                found = Counter()
                for w in words:
                    cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
                    if b.x0 - 2 <= cx <= b.x1 + 2 and b.y0 - 2 <= cy <= bottom:
                        found.update(_tokens(w[4]))
                m = sum((exp & found).values())
                m_page += m
                t_page += total
                if m / total < RECALL_OK:
                    outside.append(it.el.element_id)
            row["text_in_box_recall"] = round(m_page / t_page, 4) if t_page else None
            overprinted += _overprinted(page, plan)
            raw_latex += _raw_latex(page, plan)
            clipped += _clipped(plan)
            matched_all += m_page
            total_all += t_page
            per_page.append(row)
    items = [it for plan in plans for it in plan.items]
    # flowing text and equations; tables, figure placeholders and labels over a figure are fitted to their box
    texty = [it for it in items if it.has_text and it.group != "own" and not it.over_figure]
    own = [it for it in items if it.has_text and (it.group == "own" or it.over_figure)]
    at_shared = [it for it in texty if it.status in ("fit", "moved")]
    shrunk = [it.el.element_id for it in texty if it.status == "shrunk"]
    overflowed = [it.el.element_id for it in texty if it.status == "overflow"]
    share = round(len(at_shared) / len(texty), 4) if texty else None
    figures = Counter(it.image_kind for it in items if it.kind == "figure")
    figures["placeholder"] = sum(1 for it in items if it.kind == "placeholder")
    figures["whitened_here"] = sum(1 for it in items if it.whitened)
    maths = [it for it in items if it.kind == "math"]
    fidelity = {
        "elements_placed": len(items),
        "text_elements": len(texty),
        # one body size (per document when the pages agree, else per page); every role at a fixed ratio of it
        "body_size": {plan.page.page_number: plan.body for plan in plans},
        "body_size_scope": {plan.page.page_number: plan.body_scope for plan in plans},  # document | page
        "at_shared_size": len(at_shared),  # set at the body size (or its role's ratio), in or grown from its box
        "shared_size_share": share,
        "fit_at_target": len(at_shared),  # older names of the two values above
        "fit_share": share,
        "moved": [it.el.element_id for it in texty if it.grew],  # box extended into free space (right / down)
        "moved_detail": [{"element_id": it.el.element_id, "grew": it.grew,
                          "box": [round(v, 1) for v in (it.box.x0, it.box.y0, it.box.x1, it.box.y1)]}
                         for it in texty if it.grew],
        "shrunk": shrunk,  # set smaller than the shared size (no free space), down to the role's floor
        "shrunk_detail": [{"element_id": it.el.element_id, "size": it.size, "target": it.target}
                          for it in texty if it.status == "shrunk"],
        "overflowed": overflowed,
        # what a reader would see as broken, measured on the finished PDF
        "clipped_by_page": clipped,  # text that passes the page edge and is cut off (Chromium renderer only)
        "overprinted": overprinted,  # "a~b": printed characters of a land on b's characters or pen strokes
        "raw_latex": raw_latex,  # LaTeX source printed as text
        "fitted_to_box": [it.el.element_id for it in own],
        "boxes_adjusted": [it.el.element_id for it in items if it.adjusted],
        "overlaps_unresolved": [o for plan in plans for o in plan.overlaps],
        "unplaced": [e for plan in plans for e in plan.unplaced],
        "text_in_box_recall": round(matched_all / total_all, 4) if total_all else None,
        "text_outside_box": outside,
        "target_sizes": {plan.page.page_number: _targets(plan) for plan in plans},
    }
    if not sizes_ok:
        warnings.append("Some layout-PDF page sizes differ from the original by more than 0.5 pt")
    if outside:
        warnings.append(f"Layout PDF: text of {len(outside)} element(s) was not found inside its box: "
                        + ", ".join(outside[:12]))
    if clipped:
        warnings.append(f"Layout PDF: {len(clipped)} element(s) run past the page edge and are cut off: "
                        + ", ".join(clipped[:12]))
    if overprinted:
        warnings.append(f"Layout PDF: {len(overprinted)} pair(s) of elements print over each other: "
                        + ", ".join(overprinted[:12]))
    if raw_latex:
        warnings.append(f"Layout PDF: {len(raw_latex)} element(s) print LaTeX source as text: "
                        + ", ".join(raw_latex[:12]))
    return {
        "renderer": renderer,
        "page_count": len(plans),
        "page_count_ok": count_ok,
        "page_sizes_match": sizes_ok,
        "fonts_in_pdf": fonts,
        "layout_fidelity": fidelity,
        "figures": {k: v for k, v in figures.items() if v},
        "equations": {"typeset": sum(1 for m in maths if not m.tex_errors) if renderer == "chromium" else 0,
                      "plain_text": len(maths) if renderer != "chromium" else sum(1 for m in maths if m.tex_errors)},
        "pages": per_page,
    }


def _targets(plan: PagePlan) -> dict[str, float]:
    out: dict[str, float] = {}
    for it in plan.items:
        if it.target is not None and it.group not in ("", "own") and it.group not in out:
            out[it.group] = round(it.target, 2)
    return out


def export_layout_pdf(ctx: ExportContext) -> ExportResult:
    opts = parse_options(ctx)
    warnings: list[str] = []
    pages = [p for p in ordered_pages(ctx.document) if opts.pages is None or p.page_number in opts.pages]
    if not pages:
        raise ExportError("Layout PDF: no pages to export")
    browser = None
    if opts.renderer != "pymupdf":
        browser = layout_html.find_browser(opts.browser)
        if browser is None and opts.renderer == "chromium":
            raise ExportError("Layout PDF needs a Chromium-based browser (Microsoft Edge, Google Chrome, Chromium "
                              f"or Brave) and none was found. Install one or set {layout_html.BROWSER_ENV} to its "
                              "executable, or pass renderer='pymupdf' for the basic renderer.")
        if browser is None:
            warnings.append("No Chromium-based browser found (Edge, Chrome, Chromium or Brave; or set "
                            f"{layout_html.BROWSER_ENV}): used the basic PyMuPDF renderer. Equations are shown as "
                            "plain text and fonts are MuPDF's built-in serif.")
    title = ctx.document.title or (ctx.document.original_filenames[0] if ctx.document.original_filenames else "")
    langs = ctx.document.languages
    with tempfile.TemporaryDirectory(prefix="socr-layout-") as tmp:
        work = Path(tmp)
        assets = work / "assets"
        plans = [plan_page(ctx, p, opts, assets) for p in pages]
        fonts_used: list[str] = []
        if browser is not None:
            raw, fonts_used = render_chromium(plans, browser, work, title=title, lang=langs[0] if langs else None,
                                              opts=opts)
            renderer = "chromium"
        else:
            raw = render_pymupdf(plans, work, opts)
            renderer = "pymupdf"
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        out_path = ctx.out_dir / OUTPUT_NAME
        _finalise(raw, plans, out_path, title, warnings)

    items = [it for plan in plans for it in plan.items]
    overflowed = [it.el.element_id for it in items if it.status == "overflow"]
    if overflowed:
        warnings.append(f"Layout PDF: {len(overflowed)} element(s) did not fit their box even at the minimum "
                        f"size and overflow downward: {', '.join(overflowed)}")
    shrunk = [it.el.element_id for it in items if it.status == "shrunk" and it.group != "own" and not it.over_figure]
    if shrunk:
        warnings.append(f"Layout PDF: {len(shrunk)} element(s) had no free space to grow into and are set smaller "
                        f"than the shared size: {', '.join(shrunk)}")
    unplaced = [e for plan in plans for e in plan.unplaced]
    if unplaced:
        warnings.append(f"Layout PDF: {len(unplaced)} element(s) have no usable position and were left out: "
                        + ", ".join(unplaced))
    estimates = sum(1 for it in items if (it.el.bbox_source or "") == "model_estimate")
    if estimates:
        warnings.append(f"Layout PDF: {estimates} of {len(items)} element boxes are model estimates (not refined "
                        "to the ink), so positions are approximate")
    placeholders = [it.el.element_id for it in items if it.kind == "placeholder"]
    if placeholders:
        warnings.append(f"Layout PDF: no image for {len(placeholders)} figure(s); drawn as a box with the "
                        f"description: {', '.join(placeholders)}")
    tex_errors = [it.el.element_id for it in items if it.tex_errors]
    if tex_errors:
        warnings.append(f"Layout PDF: KaTeX could not typeset {len(tex_errors)} element(s); shown as readable "
                        f"text: {', '.join(tex_errors)}")
    if opts.figures == "svg":
        no_svg = [it.el.element_id for it in items if it.kind == "figure" and it.image_kind != "svg"]
        if no_svg:
            warnings.append(f"Layout PDF: {len(no_svg)} figure(s) have no redrawn SVG; used the best crop instead")
    verification = _verify(out_path, plans, renderer, warnings)
    verification.update({"browser": browser.describe() if browser else None, "text_variant": opts.text_variant,
                         "figures_mode": opts.figures, "background": opts.background,
                         "vendored_fonts": fonts_used,
                         "pages_exported": [p.page.page_number for p in plans]})
    return ExportResult(format="layout-pdf", primary_file=str(out_path), files=[str(out_path)],
                        warnings=warnings, verification=verification)
