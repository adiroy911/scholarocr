"""Markdown export. ``document_to_markdown`` is pure (no I/O) and is reused by the MCP server."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ..schemas import Document, Element, ElementType, Page, PageStatus, TableData
from ..schemas.elements import FURNITURE_TYPES, HEADING_TYPES, MATH_TYPES, VISUAL_TYPES
from .base import (
    ExportContext,
    ExportResult,
    export_variant_context,
    figure_image_path,
    figure_image_rel,
    figure_preference,
    redraw_note,
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
    starts_with_question_number,
    unclear_text,
    with_text_variant,
    write_text,
)

_BLOCK_START = re.compile(r"^(\s*)([#>+\-*=|]|\d+[.)])")
_PAGE_MARKER = re.compile(r"^<!-- page \d+ -->$", re.MULTILINE)
_LINK = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def _esc_line(line: str) -> str:
    """Escape characters that would turn a text line into a different Markdown block."""
    ordered = re.match(r"^(\s*)(\d{1,9})([.)])(\s)", line)
    if ordered:  # CommonMark: escape the delimiter of an ordered-list marker ("1\." not "\1.")
        return f"{ordered.group(1)}{ordered.group(2)}\\{ordered.group(3)}{ordered.group(4)}" + line[ordered.end():]
    return _BLOCK_START.sub(lambda m: m.group(1) + "\\" + m.group(2), line, count=1)


def _esc(text: str) -> str:
    return "\n".join(_esc_line(line) for line in text.split("\n"))


def unclear(text: str) -> str:
    return f"⟦unclear: {' '.join(text.split())}⟧"


def _cell_md(cell_text: str, latex: str | None, uncertain: bool) -> str:
    text = f"${latex.strip()}$" if latex and latex.strip() else " ".join(cell_text.split())
    if uncertain:
        text = unclear(text or "cell")
    return text.replace("|", "\\|")


def table_to_gfm(table: TableData) -> str:
    """GFM table. Spanned positions are left empty; the first row is always the GFM header."""
    layout = layout_table(table)
    if layout.n_rows == 0 or layout.n_cols == 0:
        return unclear("empty table")
    grid = [[""] * layout.n_cols for _ in range(layout.n_rows)]
    for p in layout.cells:
        text = _cell_md(p.cell.text, p.cell.latex, p.cell.uncertain)
        if 0 < p.row < layout.header_rows and text:
            text = f"**{text}**"
        grid[p.row][p.col] = text
    lines = ["| " + " | ".join(grid[0]) + " |", "|" + "---|" * layout.n_cols]
    lines.extend("| " + " | ".join(row) + " |" for row in grid[1:])
    return "\n".join(lines)


def _question_wrap(el: Element, text: str) -> str:
    q = el.question
    if q is None:
        return text
    prefix = f"**Q{q.number}** " if q.number else ""
    if starts_with_question_number(q.number, text):
        prefix = ""  # the transcription already starts with the printed number
    suffix = f" *{marks_label(q.marks)}*" if q.marks and marks_label(q.marks) else ""
    if q.marks and re.search(rf"[\[(]\s*{re.escape(q.marks)}\s*(marks?|M)?\s*[\])]\s*$", text, re.I):
        suffix = ""  # marks already printed at the end
    return f"{prefix}{text}{suffix}"


def _figure_md(page: Page, el: Element, asset_prefix: str | None, figure_prefer: str = "original",
               figure_links: dict[str, str] | None = None) -> str:
    fig = el.figure
    desc = " ".join((fig.description if fig and fig.description else el.content).split())
    parts: list[str] = []
    if figure_links is not None:
        link = figure_links.get(el.element_id)
    else:
        rel = figure_image_rel(el, figure_prefer) if figure_prefer != "original" else region_image(page, el)
        if rel in (page.original_image, page.processed_image, el.source.page_image):
            rel = None
        link = None
        if rel:
            link = f"{asset_prefix.rstrip('/')}/{Path(rel).name}" if asset_prefix is not None else rel
    if link:
        alt = (desc[:120] or el.type.value).replace("[", "(").replace("]", ")")
        parts.append(f"![{alt}]({link})")
    head = el.type.value.capitalize()
    if fig and fig.figure_label:
        head = fig.figure_label
    if fig and fig.kind:
        head += f" ({fig.kind})"
    quote = [f"> **{head}**" + (f": {desc}" if desc else "")]
    if fig and fig.labels:
        quote.append("> Labels: " + ", ".join(" ".join(x.split()) for x in fig.labels))
    if fig and fig.relationships:
        quote.append("> Relationships: " + "; ".join(" ".join(x.split()) for x in fig.relationships))
    parts.append("\n".join(quote))
    return "\n".join(parts)


def element_to_markdown(page: Page, el: Element, asset_prefix: str | None = None, figure_prefer: str = "original",
                        figure_links: dict[str, str] | None = None) -> str:
    """Markdown for one element (no trailing newline)."""
    if el.type == ElementType.UNCERTAIN:
        return unclear(unclear_text(el))
    unresolved = is_unclear(el)
    note = unclear("; ".join(el.uncertainty_reasons) or "reading not verified") + "\n" if unresolved else ""

    def txt(s: str) -> str:
        return unclear(s or unclear_text(el)) if unresolved else _esc(s)

    if el.type in HEADING_TYPES:
        return "#" * min(heading_level(el) + 1, 6) + " " + txt(" ".join(el.content.split()))
    if el.type in MATH_TYPES:
        body = math_latex(el) or " ".join(el.content.split())
        return f"{note}$$\n{body}\n$$"
    if el.type == ElementType.TABLE:
        if el.table is None:
            return txt(plain_text(el))
        caption = el.table.caption
        if caption and any(c.type == ElementType.CAPTION and c.parent_id == el.element_id for c in page.elements):
            caption = None  # rendered by the linked caption element; don't duplicate it
        head = " ".join(x for x in (el.table.label and f"**{el.table.label}**", caption) if x)
        return note + (head + "\n\n" if head else "") + table_to_gfm(el.table)
    if el.type in VISUAL_TYPES:
        return note + _figure_md(page, el, asset_prefix, figure_prefer, figure_links)
    if el.type == ElementType.LIST:
        items = el.items if el.items else plain_text(el).split("\n")
        rendered = "\n".join(f"- {txt(' '.join(i.split()))}" for i in items if i.strip())
        head = _question_wrap(el, "").strip()
        return "\n\n".join(x for x in (head, rendered) if x) or txt("")
    if el.type == ElementType.CODE:
        return f"{note}```\n{el.content.rstrip()}\n```"
    if el.type == ElementType.CAPTION:
        cap = " ".join(el.content.split())
        return f"*{unclear(cap or unclear_text(el)) if unresolved else cap.replace('*', chr(92) + '*')}*"
    if el.type == ElementType.FOOTNOTE:
        return "> *Footnote:* " + txt(" ".join(el.content.split()))
    if el.type in FURNITURE_TYPES:
        return f"*{el.type.value.replace('_', ' ')}: {txt(' '.join(el.content.split()))}*"
    return _question_wrap(el, txt(plain_text(el)))


def _page_note(page: Page) -> str | None:
    if page.elements:
        return None
    if page.status == PageStatus.FAILED:
        return unclear(f"page {page.page_number} could not be extracted" + (f" ({page.error})" if page.error else ""))
    if page.status == PageStatus.BLANK:
        return f"<!-- page {page.page_number}: blank -->"
    if page.status in (PageStatus.PENDING, PageStatus.SKIPPED):
        return f"<!-- page {page.page_number}: not processed -->"
    return None


def document_to_markdown(
    document: Document,
    *,
    include_ids: bool = False,
    asset_prefix: str | None = None,
    element_types: set[ElementType] | None = None,
    page_numbers: set[int] | None = None,
    include_furniture: bool = False,
    text_variant: str | None = None,
    figure_prefer: str = "original",
    figure_links: dict[str, str] | None = None,
) -> str:
    """Render the IR as compact, deterministic Markdown.

    Header/footer/page-number elements are omitted unless ``include_furniture`` is set or
    their type is requested explicitly in ``element_types``. Figure links point to
    ``asset_prefix/<crop file>`` or, when ``asset_prefix`` is None, to the crop's path
    relative to the document directory; ``figure_prefer`` (clean | original | svg) picks which stored image,
    and ``figure_links`` (element id -> link) overrides both. ``text_variant="clean"`` renders the cleaned text
    (``clean_content``) where it exists.
    """
    if text_variant:
        document = with_text_variant(document, text_variant)
    blocks: list[str] = []
    for page in ordered_pages(document):
        if page_numbers is not None and page.page_number not in page_numbers:
            continue
        blocks.append(f"<!-- page {page.page_number} -->")
        if element_types is None and (note := _page_note(page)):
            blocks.append(note)
        for el in ordered_elements(page):
            if element_types is not None:
                if el.type not in element_types:
                    continue
            elif el.type in FURNITURE_TYPES and not include_furniture:
                continue
            block = element_to_markdown(page, el, asset_prefix, figure_prefer, figure_links)
            if include_ids:
                unc = f" uncertainty={el.uncertainty.value}" if el.uncertainty.value != "low" else ""
                block += ("\n" if "\n" in block else " ") + f"<!-- el:{el.element_id}{unc} -->"
            blocks.append(block)
    return "\n\n".join(blocks) + "\n"


def export_markdown(ctx: ExportContext) -> ExportResult:
    warnings: list[str] = []
    ctx, variant = export_variant_context(ctx)
    prefer = figure_preference(ctx)
    include_furniture = ctx.flag("include_furniture", False)
    assets_dir = ctx.out_dir / "assets"
    files: list[str] = []
    links: dict[str, str] = {}
    for page in ordered_pages(ctx.document):
        for el in ordered_elements(page):
            if el.type not in VISUAL_TYPES:
                continue
            src = figure_image_path(ctx.doc_dir, el, prefer)
            if note := redraw_note(el, prefer):
                warnings.append(note)
            if src is None:
                if el.source.region_image or (el.figure and (el.figure.image_clean or el.figure.svg)):
                    warnings.append(f"Figure image missing on disk for {el.element_id}")
                continue
            assets_dir.mkdir(parents=True, exist_ok=True)
            dest = assets_dir / src.name
            shutil.copyfile(src, dest)
            files.append(str(dest))
            links[el.element_id] = f"assets/{dest.name}"
    text = document_to_markdown(
        ctx.document, include_ids=ctx.flag("include_ids", False), asset_prefix="assets",
        include_furniture=include_furniture, figure_links=links,
    )
    out = ctx.out_dir / "document.md"
    write_text(out, text)
    files.insert(0, str(out))

    written = out.read_text(encoding="utf-8")
    links = _LINK.findall(written)
    broken = [lnk for lnk in links if not (ctx.out_dir / lnk).is_file()]
    if broken:
        warnings.append(f"{len(broken)} figure link(s) point to missing crops: {', '.join(broken)}")
    unclear_elements = sum(
        1 for _, el in iter_document(ctx.document)
        if is_unclear(el) and (include_furniture or el.type not in FURNITURE_TYPES)
    )
    verification = {
        "page_markers": len(_PAGE_MARKER.findall(written)),
        "pages": len(ctx.document.pages),
        "images_linked": len(links),
        "broken_image_links": broken,
        "unclear_markers": written.count("⟦unclear:"),
        "unclear_elements": unclear_elements,
        "text_variant": variant,
        "figure_variant": prefer,
    }
    return ExportResult(format="markdown", primary_file=str(out), files=files, warnings=warnings,
                        verification=verification)
