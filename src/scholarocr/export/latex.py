"""LaTeX export for XeLaTeX, with an optional compilation check.

The main font is only set when the caller asks for one (option ``main_font``); otherwise
the document uses XeLaTeX's default Latin Modern. After compiling, the fonts actually
embedded in the PDF and any "Missing character" glyphs from the log are reported, so the
verification never claims a font or glyph coverage that was not achieved.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

import pymupdf

from ..latex_check import latex_issues
from ..exceptions import ExportError
from ..schemas import Element, ElementType, Page, TableData
from ..schemas.elements import FURNITURE_TYPES, MATH_TYPES, VISUAL_TYPES
from .base import (
    ExportContext,
    ExportResult,
    ce_to_math,
    copy_asset,
    heading_level,
    is_unclear,
    iter_document,
    layout_table,
    marks_label,
    math_latex,
    ordered_elements,
    ordered_pages,
    plain_text,
    region_image,
    unclear_text,
    write_text,
)

COMPILE_TIMEOUT_S = 120
LOG_TAIL_LINES = 30
LONGTABLE_MIN_ROWS = 25
WRAP_CELL_CHARS = 25
MAX_FIGURE_WIDTH_PT = 340.0
DEFAULT_FONT = "Latin Modern Roman"
DEFAULT_FONT_TOKEN = "lmroman"
SECTION_CMDS = {1: "section", 2: "subsection", 3: "subsubsection", 4: "paragraph"}
_DISPLAY_ENVS = re.compile(r"^\\begin\{(equation|align|gather|multline|flalign|eqnarray)\*?\}")
_MISSING = re.compile(r"Missing character: There is no (\S+)")
_TEX_ESC = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "#": r"\#", "$": r"\$", "%": r"\%", "&": r"\&",
    "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def tex_escape(text: str) -> str:
    """Escape LaTeX special characters; drops control characters other than newline/tab."""
    out = []
    for ch in text:
        if ch in _TEX_ESC:
            out.append(_TEX_ESC[ch])
        elif ch in "\n\t" or unicodedata.category(ch) != "Cc":
            out.append(ch)
    return "".join(out)


def _inline(text: str) -> str:
    return tex_escape(" ".join(text.split()))


def _unclear(text: str) -> str:
    return r"\textbf{[unclear: " + _inline(text) + "]}"


def kpsewhich_has(name: str) -> bool:
    exe = shutil.which("kpsewhich")
    if exe is None:
        return False
    try:
        proc = subprocess.run([exe, name], capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


class _LatexWriter:
    def __init__(self, ctx: ExportContext, warnings: list[str]):
        self.ctx = ctx
        self.warnings = warnings
        self.include_furniture = ctx.flag("include_furniture", False)
        self.assets: list[Path] = []
        self.bib: list[Element] = []
        self.uses_longtable = False
        self.uses_multirow = False
        self.needs_mhchem = False
        self.has_mhchem = False
        self.multirow_available = kpsewhich_has("multirow.sty")
        self.captions: dict[str, list[Element]] = {}
        self.parents: set[str] = set()
        # hyperref's XeTeX driver loads the ZapfDingbats metric (pzdr.tfm); minimal TeX
        # installations lack it and then every document fails to compile.
        self.hyperref = kpsewhich_has("hyperref.sty") and kpsewhich_has("pzdr.tfm")
        if not self.hyperref:
            warnings.append("hyperref omitted: hyperref.sty or pzdr.tfm (texlive-fonts-recommended) is not "
                            "installed; the PDF has no title metadata or link support")

    # -- pre-pass
    def prepare(self) -> None:
        ids = {}
        for _, el in iter_document(self.ctx.document):
            ids[el.element_id] = el
            if el.type in MATH_TYPES and el.latex and r"\ce" in el.latex:
                self.needs_mhchem = True
        for _, el in iter_document(self.ctx.document):
            parent = ids.get(el.parent_id or "")
            if el.type == ElementType.CAPTION and parent is not None and (
                    parent.type in VISUAL_TYPES or parent.type == ElementType.TABLE):
                self.captions.setdefault(parent.element_id, []).append(el)
                self.parents.add(el.element_id)
        if self.needs_mhchem:
            self.has_mhchem = kpsewhich_has("mhchem.sty")
            if not self.has_mhchem:
                self.warnings.append(r"mhchem.sty not installed: \ce{...} chemistry rendered as plain \mathrm math")

    def caption_for(self, el: Element) -> str:
        return " ".join(_inline(c.content) if not is_unclear(c) else _unclear(unclear_text(c))
                        for c in self.captions.get(el.element_id, []))

    # -- elements
    def element(self, page: Page, el: Element) -> str:
        t = el.type
        if el.element_id in self.parents:
            return ""
        if t in FURNITURE_TYPES:
            return r"{\footnotesize [" + t.value.replace("_", " ") + "] " + _inline(el.content) + r"\par}" \
                if self.include_furniture else ""
        if t == ElementType.BIBLIOGRAPHY_ENTRY:
            self.bib.append(el)
            return ""
        if t == ElementType.UNCERTAIN:
            return _unclear(unclear_text(el)) + r"\par"
        note = _unclear("; ".join(el.uncertainty_reasons) or "reading not verified") + "\\par\n" \
            if is_unclear(el) else ""
        if t in (ElementType.TITLE, ElementType.HEADING, ElementType.SUBHEADING):
            text = _unclear(el.content or unclear_text(el)) if note else _inline(el.content)
            level = heading_level(el)
            if level == 0:
                return r"\begin{center}{\LARGE\bfseries " + text + r"\par}\end{center}"
            cmd = SECTION_CMDS.get(level, "subparagraph")
            return rf"\{cmd}*{{{text}}}"
        if t in MATH_TYPES:
            return note + self.math(el)
        if t == ElementType.TABLE:
            return note + self.table(el)
        if t in VISUAL_TYPES:
            return note + self.figure(page, el)
        if t == ElementType.LIST:
            items = [i for i in (el.items if el.items else plain_text(el).split("\n")) if i.strip()]
            if not items:
                return note.strip()
            body = "\n".join(r"\item " + (_unclear(i) if note else _inline(i)) for i in items)
            return self.question_wrap(el, "") + "\\begin{itemize}\n" + body + "\n\\end{itemize}"
        if t == ElementType.CODE:
            code = el.content.rstrip()
            if r"\end{verbatim}" in code:
                return note + r"{\ttfamily " + tex_escape(code).replace("\n", "\\\\\n") + "}\\par"
            return note + "\\begin{verbatim}\n" + code + "\n\\end{verbatim}"
        text = _unclear(plain_text(el) or unclear_text(el)) if note else tex_escape(plain_text(el))
        if t == ElementType.CAPTION:
            return r"\begin{center}\small\emph{" + text + r"}\end{center}"
        if t == ElementType.FOOTNOTE:
            return r"{\footnotesize " + text + r"\par}"
        return self.question_wrap(el, text)

    def question_wrap(self, el: Element, text: str) -> str:
        q = el.question
        if q is None:
            return text
        prefix = r"\noindent\textbf{Q" + _inline(q.number) + "}~" if q.number else ""
        suffix = r"\hfill\emph{" + _inline(marks_label(q.marks)) + "}" if q.marks and marks_label(q.marks) else ""
        if not text:
            return (prefix + suffix + "\\par\n") if (prefix or suffix) else ""
        return prefix + text + suffix

    def math(self, el: Element) -> str:
        latex = math_latex(el)
        if latex is None:
            return r"\begin{center}" + _inline(el.content) + r"\end{center}"
        issues = latex_issues(latex)
        if issues:
            # Isolate invalid math: one malformed expression must not break the whole document.
            self.warnings.append(f"{el.element_id}: invalid LaTeX ({'; '.join(issues)}); shown verbatim")
            return (r"\begin{center}\textbf{[invalid LaTeX, shown verbatim]}\\" + "\n"
                    + r"\texttt{" + tex_escape(latex) + "}" + r"\end{center}")
        if r"\ce" in latex and not self.has_mhchem:
            latex = ce_to_math(latex)
        if _DISPLAY_ENVS.match(latex):
            return latex
        return "\\[\n" + latex + "\n\\]"

    def _cell(self, text: str, latex: str | None, uncertain: bool, bold: bool) -> str:
        if uncertain:
            return _unclear(text or "cell")
        if not text.strip() and latex and latex.strip() and latex_issues(latex):
            body = r"\texttt{" + tex_escape(latex) + "}"
        else:
            body = _inline(text) if text.strip() else (f"${latex.strip()}$" if latex and latex.strip() else "")
        return rf"\textbf{{{body}}}" if bold and body else body

    def table(self, el: Element) -> str:
        if el.table is None:
            return self.question_wrap(el, tex_escape(plain_text(el)))
        tbl: TableData = el.table
        layout = layout_table(tbl)
        if layout.n_rows == 0 or layout.n_cols == 0:
            return _unclear("empty table")
        n = layout.n_cols
        wrap = n > 1 and any(len(p.cell.text) > WRAP_CELL_CHARS for p in layout.cells)
        col = rf"p{{\dimexpr 0.95\linewidth/{n} - 2\tabcolsep\relax}}" if wrap else "l"
        spec = col * n
        rows: list[str] = []
        for r in range(layout.n_rows):
            cells: list[str] = []
            c = 0
            while c < n:
                owner = layout.owner[r][c]
                if owner is None:
                    cells.append("")
                    c += 1
                    continue
                cs = owner.col_span
                if owner.row == r and owner.col == c:
                    body = self._cell(owner.cell.text, owner.cell.latex, owner.cell.uncertain,
                                      r < layout.header_rows or owner.cell.is_header)
                    if owner.row_span > 1 and self.multirow_available:
                        self.uses_multirow = True
                        body = rf"\multirow{{{owner.row_span}}}{{*}}{{{body}}}"
                    cells.append(rf"\multicolumn{{{cs}}}{{l}}{{{body}}}" if cs > 1 else body)
                else:  # covered by a row span from above
                    cells.append(rf"\multicolumn{{{cs}}}{{l}}{{}}" if cs > 1 else "")
                c += cs
            rows.append(" & ".join(cells) + r" \\")
            if r == layout.header_rows - 1 and r < layout.n_rows - 1:
                rows.append(r"\midrule")
        caption = " ".join(x for x in (
            _inline(tbl.label) if tbl.label else "", _inline(tbl.caption) if tbl.caption else "",
            self.caption_for(el)) if x)
        body = "\n".join(rows)
        if layout.n_rows > LONGTABLE_MIN_ROWS:
            self.uses_longtable = True
            cap = rf"\caption*{{{caption}}}\\" + "\n" if caption else ""
            return f"\\begin{{longtable}}{{{spec}}}\n{cap}\\toprule\n{body}\n\\bottomrule\n\\end{{longtable}}"
        cap = rf"\caption*{{{caption}}}" + "\n" if caption else ""
        return (f"\\begin{{table}}[htbp]\n\\centering\n{cap}\\begin{{tabular}}{{{spec}}}\n\\toprule\n{body}\n"
                f"\\bottomrule\n\\end{{tabular}}\n\\end{{table}}")

    def figure(self, page: Page, el: Element) -> str:
        fig = el.figure
        desc = " ".join((fig.description if fig and fig.description else el.content).split())
        label = fig.figure_label if fig and fig.figure_label else ""
        caption = self.caption_for(el) or _inline(" ".join(x for x in (label, desc) if x))
        rel = region_image(page, el)
        copied = None
        if rel:
            name = f"fig{len(self.assets) + 1:03d}{Path(rel).suffix.lower() or '.png'}"
            copied = copy_asset(self.ctx, rel, self.ctx.out_dir / "assets", self.warnings, name=name)
        if copied is None:
            return r"\begin{center}\emph{[" + _inline(el.type.value) + (": " + _inline(desc) if desc else "") \
                + r"]}\end{center}"
        self.assets.append(copied)
        width = min(el.bbox.width, MAX_FIGURE_WIDTH_PT) if el.bbox is not None and el.bbox.width > 0 \
            else MAX_FIGURE_WIDTH_PT
        lines = [r"\begin{figure}[htbp]", r"\centering",
                 rf"\includegraphics[width={width:.1f}pt,height=0.45\textheight,keepaspectratio]{{assets/{copied.name}}}"]
        if caption:
            lines.append(rf"\caption*{{{caption}}}")
        if self.captions.get(el.element_id) and desc:
            lines.append(r"{\small\emph{" + _inline(desc) + r"}\par}")
        lines.append(r"\end{figure}")
        return "\n".join(lines)

    # -- document
    def bibliography(self) -> str:
        if not self.bib:
            return ""
        items = "\n".join(
            rf"\bibitem{{ref{i}}} " + (_unclear(unclear_text(e)) if is_unclear(e) else _inline(e.content))
            for i, e in enumerate(self.bib, 1))
        return "\\begin{thebibliography}{99}\n" + items + "\n\\end{thebibliography}"

    def preamble(self, main_font: str | None, title: str | None) -> str:
        lines = [r"\documentclass[11pt]{article}", r"\usepackage{fontspec}"]
        if main_font:
            lines.append(rf"\setmainfont{{{main_font}}}")
        lines += [r"\usepackage{amsmath}", r"\usepackage{amssymb}", r"\usepackage{graphicx}",
                  r"\usepackage{booktabs}", r"\usepackage{caption}"]
        if self.uses_longtable:
            lines.append(r"\usepackage{longtable}")
        if self.uses_multirow:
            lines.append(r"\usepackage{multirow}")
        if self.needs_mhchem and self.has_mhchem:
            lines.append(r"\usepackage[version=4]{mhchem}")
        if self.hyperref:
            lines.append(r"\usepackage[hidelinks]{hyperref}")
            if title:
                lines.append(rf"\hypersetup{{pdftitle={{{_inline(title)}}}}}")
        return "\n".join(lines)


def _pdf_fonts(pdf: Path) -> list[str]:
    names: set[str] = set()
    with pymupdf.open(pdf) as doc:
        for page in doc:
            for f in page.get_fonts():
                base = f[3].split("+", 1)[-1]
                if base:
                    names.add(base)
    return sorted(names)


def compile_latex(tex_path: Path, warnings: list[str]) -> dict:
    """Run XeLaTeX once in the .tex file's directory (no shell escape, restricted file access)."""
    engine = shutil.which("xelatex")
    if engine is None:
        warnings.append("xelatex not found on PATH: the .tex file was not compile-checked")
        return {"engine": None, "compiled": None}
    workdir = tex_path.parent
    pdf = tex_path.with_suffix(".pdf")
    log = tex_path.with_suffix(".log")
    pdf.unlink(missing_ok=True)
    env = {**os.environ, "openin_any": "p", "openout_any": "p"}
    cmd = [engine, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", tex_path.name]
    result: dict = {"engine": "xelatex", "pdf": str(pdf)}
    try:
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=COMPILE_TIMEOUT_S,
                              env=env, errors="replace", check=False)
        ok = proc.returncode == 0 and pdf.is_file()
    except subprocess.TimeoutExpired:
        ok = False
        warnings.append(f"xelatex timed out after {COMPILE_TIMEOUT_S}s")
    log_text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    result["compiled"] = ok
    for ext in (".aux", ".out"):
        tex_path.with_suffix(ext).unlink(missing_ok=True)
    (workdir / "missfont.log").unlink(missing_ok=True)
    missing = sorted(set(_MISSING.findall(log_text)))
    result["missing_characters"] = missing
    if missing:
        warnings.append(f"The PDF font lacks {len(missing)} character(s) ({''.join(missing[:20])}); "
                        "set option main_font to a font that covers them")
    if not ok:
        result["log_tail"] = "\n".join(log_text.splitlines()[-LOG_TAIL_LINES:])
        result["pdf"] = None
        warnings.append("xelatex compilation failed; see verification.log_tail")
    return result


def export_latex(ctx: ExportContext) -> ExportResult:
    warnings: list[str] = []
    main_font = ctx.option("main_font")
    if main_font is not None:
        main_font = str(main_font).strip()
        if not main_font or any(ch in main_font for ch in "{}\\%#$&^~\n"):
            raise ExportError(f"Invalid main_font option: {main_font!r}")
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    writer = _LatexWriter(ctx, warnings)
    writer.prepare()
    body: list[str] = []
    for page in ordered_pages(ctx.document):
        body.append(f"% ---- source page {page.page_number} ----")
        for el in ordered_elements(page):
            block = writer.element(page, el)
            if block:
                body.append(block)
    if bib := writer.bibliography():
        body.append(bib)
    tex = (writer.preamble(main_font, ctx.document.title) + "\n\\begin{document}\n\n" + "\n\n".join(body)
           + "\n\n\\end{document}\n")
    tex_path = ctx.out_dir / "document.tex"
    write_text(tex_path, tex)
    files = [str(tex_path)] + [str(a) for a in writer.assets]

    verification: dict = {"main_font_requested": main_font, "main_font": main_font or DEFAULT_FONT}
    if ctx.flag("compile", True):
        verification.update(compile_latex(tex_path, warnings))
        log = tex_path.with_suffix(".log")
        if log.is_file():
            files.append(str(log))
        if verification.get("compiled"):
            files.append(verification["pdf"])
            fonts = _pdf_fonts(Path(verification["pdf"]))
            token = (main_font or DEFAULT_FONT_TOKEN).replace(" ", "").lower()
            verification["fonts_in_pdf"] = fonts
            verification["main_font_verified"] = any(token in f.replace(" ", "").lower() for f in fonts)
            if not verification["main_font_verified"]:
                warnings.append(f"Main font {verification['main_font']!r} not found among the PDF's fonts: {fonts}")
    else:
        verification.update({"engine": None, "compiled": None, "compile_skipped": True})
    return ExportResult(format="latex", primary_file=str(tex_path), files=files, warnings=warnings,
                        verification=verification)
