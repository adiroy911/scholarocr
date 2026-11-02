"""Table exports: one CSV per table, or one XLSX workbook (index sheet + one sheet per table)."""

from __future__ import annotations

import csv
from dataclasses import dataclass

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill

from ..exceptions import ExportError
from ..schemas import Document, Element, ElementType, TableData
from .base import ExportContext, ExportResult, TableLayout, iter_document, layout_table

UNCERTAIN_FILL = PatternFill(start_color="FFFF99", end_color="FFFF99", fill_type="solid")


@dataclass
class _Table:
    element: Element
    data: TableData
    page: int
    index: int  # 1-based position among the page's tables
    layout: TableLayout

    @property
    def stem(self) -> str:
        return f"table_{self.page}_{self.index}"


def collect_tables(document: Document) -> list[_Table]:
    tables: list[_Table] = []
    per_page: dict[int, int] = {}
    for page, el in iter_document(document):
        if el.type != ElementType.TABLE or el.table is None:
            continue
        per_page[page.page_number] = per_page.get(page.page_number, 0) + 1
        tables.append(_Table(el, el.table, page.page_number, per_page[page.page_number], layout_table(el.table)))
    if not tables:
        raise ExportError("Document contains no tables")
    return tables


def _cell_value(text: str, latex: str | None) -> str:
    return text if text.strip() else (latex or "")


def export_csv(ctx: ExportContext) -> ExportResult:
    tables = collect_tables(ctx.document)
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    checks: list[dict] = []
    warnings: list[str] = []
    for t in tables:
        grid = [[""] * t.layout.n_cols for _ in range(t.layout.n_rows)]
        for p in t.layout.cells:
            grid[p.row][p.col] = _cell_value(p.cell.text, p.cell.latex)
        path = ctx.out_dir / f"{t.stem}.csv"
        with open(path, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(grid)
        files.append(str(path))
        with open(path, encoding="utf-8", newline="") as f:
            back = list(csv.reader(f))
        ok = back == grid
        if not ok:
            warnings.append(f"{path.name}: re-read content differs from the table")
        checks.append({"file": path.name, "element_id": t.element.element_id, "rows": t.layout.n_rows,
                       "cols": t.layout.n_cols, "roundtrip_ok": ok})
    return ExportResult(format="csv", primary_file=files[0], files=files, warnings=warnings,
                        verification={"tables": len(tables), "files": checks,
                                      "all_ok": all(c["roundtrip_ok"] for c in checks)})


def export_xlsx(ctx: ExportContext) -> ExportResult:
    tables = collect_tables(ctx.document)
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    index = wb.active
    index.title = "Index"
    index.append(["Sheet", "Element ID", "Page", "Label", "Caption", "Rows", "Columns", "Uncertainty"])
    for c in index[1]:
        c.font = Font(bold=True)
    expected: dict[str, dict] = {}
    for t in tables:
        name = f"T{t.page}_{t.index}"[:31]
        ws = wb.create_sheet(name)
        data = t.data
        for p in t.layout.cells:
            cell = ws.cell(row=p.row + 1, column=p.col + 1, value=_cell_value(p.cell.text, p.cell.latex) or None)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if p.row < t.layout.header_rows or p.cell.is_header:
                cell.font = Font(bold=True)
            if p.cell.uncertain:
                cell.fill = UNCERTAIN_FILL
                cell.comment = Comment("Uncertain reading: verify against the source", "ScholarOCR")
            if p.row_span > 1 or p.col_span > 1:
                ws.merge_cells(start_row=p.row + 1, start_column=p.col + 1,
                               end_row=p.row + p.row_span, end_column=p.col + p.col_span)
        index.append([name, t.element.element_id, t.page, data.label or "", data.caption or "",
                      t.layout.n_rows, t.layout.n_cols, t.element.uncertainty.value])
        index.cell(row=index.max_row, column=1).hyperlink = f"#'{name}'!A1"
        expected[name] = {
            "rows": t.layout.n_rows, "cols": t.layout.n_cols,
            "merged": sum(1 for p in t.layout.cells if p.row_span > 1 or p.col_span > 1),
            "filled": sum(1 for p in t.layout.cells if _cell_value(p.cell.text, p.cell.latex)),
        }
    out = ctx.out_dir / "tables.xlsx"
    wb.save(out)

    warnings: list[str] = []
    checks: list[dict] = []
    back = load_workbook(out)
    for name, exp in expected.items():
        ws = back[name]
        found = {
            "rows": ws.max_row, "cols": ws.max_column, "merged": len(ws.merged_cells.ranges),
            "filled": sum(1 for row in ws.iter_rows() for c in row if c.value not in (None, "")),
        }
        ok = found == exp
        if not ok:
            warnings.append(f"Sheet {name}: expected {exp}, found {found}")
        checks.append({"sheet": name, **found, "ok": ok})
    verification = {"tables": len(tables), "index_rows": back["Index"].max_row - 1, "sheets": checks,
                    "all_ok": all(c["ok"] for c in checks)}
    return ExportResult(format="xlsx", primary_file=str(out), files=[str(out)], warnings=warnings,
                        verification=verification)
