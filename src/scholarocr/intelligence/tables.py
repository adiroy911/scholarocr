"""Deterministic structural checks for extracted tables."""

from __future__ import annotations

from ..schemas import TableData


def table_issues(table: TableData | None) -> list[str]:
    if table is None:
        return ["table element without table data"]
    if not table.rows:
        return ["empty table"]
    issues: list[str] = []
    # Effective width per row, accounting for row spans from rows above.
    carry: dict[int, int] = {}  # column index -> remaining rows covered by a rowspan
    widths: list[int] = []
    for row in table.rows:
        col = 0
        width = 0
        covered_now = {c for c, n in carry.items() if n > 0}
        for cell in row:
            while col in covered_now:
                col += 1
                width += 1
            for k in range(cell.col_span):
                if cell.row_span > 1:
                    carry[col + k] = cell.row_span
            col += cell.col_span
            width += cell.col_span
        width += len([c for c in covered_now if c >= col])
        widths.append(width)
        carry = {c: n - 1 for c, n in carry.items() if n - 1 > 0}
    if len(set(widths)) > 1:
        issues.append(f"ragged rows (column counts {sorted(set(widths))})")
    if max(widths) == 1 and len(table.rows) > 1:
        issues.append("single-column table (possibly not a table)")
    cells = [c for r in table.rows for c in r]
    if cells and sum(1 for c in cells if not c.text.strip()) / len(cells) > 0.6:
        issues.append("mostly empty cells")
    if any(c.uncertain for c in cells):
        issues.append(f"{sum(c.uncertain for c in cells)} uncertain cell(s)")
    if table.header_rows > len(table.rows):
        issues.append("header_rows exceeds row count")
    return issues
