"""Content elements of the universal intermediate representation (IR).

Coordinates: every bounding box is in PDF points (1/72 inch) on the original page *as
displayed* (i.e. after any PDF /Rotate is applied, matching the rendered page image), origin
at the top-left, x to the right, y downwards. Preprocessing transforms (deskew, rotation
correction, upscaling) are inverted before boxes are stored, so boxes never refer to the
processed image. ``bbox_source`` says where it came
from: ``pdf_text_layer`` boxes are exact; ``model_estimate`` boxes come from a vision model and
are approximate; ``refined`` boxes are model estimates snapped deterministically to the ink on the
page image (``pipeline/layout_refine.py``), with the model's own box kept in ``bbox_model``.

Uncertainty is an ordinal label, never a probability. See docs/QUALITY_ASSURANCE.md for
exactly how labels are assigned.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ElementType(str, Enum):
    TITLE = "title"
    HEADING = "heading"
    SUBHEADING = "subheading"
    PARAGRAPH = "paragraph"
    TEXT = "text"
    HANDWRITING = "handwriting"
    EQUATION = "equation"
    CHEMICAL_EQUATION = "chemical_equation"
    TABLE = "table"
    FIGURE = "figure"
    DIAGRAM = "diagram"
    IMAGE = "image"
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    LIST = "list"
    CODE = "code"
    BIBLIOGRAPHY_ENTRY = "bibliography_entry"
    REFERENCE = "reference"
    UNCERTAIN = "uncertain"  # region the engine could not read: content is NOT invented


VISUAL_TYPES = {ElementType.FIGURE, ElementType.DIAGRAM, ElementType.IMAGE}
MATH_TYPES = {ElementType.EQUATION, ElementType.CHEMICAL_EQUATION}
HEADING_TYPES = {ElementType.TITLE, ElementType.HEADING, ElementType.SUBHEADING}
FURNITURE_TYPES = {ElementType.HEADER, ElementType.FOOTER, ElementType.PAGE_NUMBER}
# Element.extraction_method values written by a vision model: "gemini" (any server-side key provider,
# Gemini or OpenRouter; historical name), "host_model" (submitted by the MCP client's model in host
# mode) and "sampling" (the client's model reached through MCP sampling).
MODEL_METHODS = frozenset({"gemini", "host_model", "sampling"})


class Role(str, Enum):
    """Academic/semantic role, orthogonal to the visual element type."""

    NONE = "none"
    CHAPTER = "chapter"
    SECTION = "section"
    QUESTION = "question"
    SUBQUESTION = "subquestion"
    INSTRUCTION = "instruction"
    EXERCISE = "exercise"
    DEFINITION = "definition"
    THEOREM = "theorem"
    LEMMA = "lemma"
    EXAMPLE = "example"
    PROOF = "proof"
    SOLUTION = "solution"
    ANSWER = "answer"
    ABSTRACT = "abstract"
    AUTHORS = "authors"
    AFFILIATION = "affiliation"
    KEYWORDS = "keywords"
    MARGINAL_NOTE = "marginal_note"


class Uncertainty(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNRESOLVED = "unresolved"


UNCERTAINTY_RANK = {Uncertainty.LOW: 0, Uncertainty.MEDIUM: 1, Uncertainty.HIGH: 2, Uncertainty.UNRESOLVED: 3}


def max_uncertainty(a: Uncertainty, b: Uncertainty) -> Uncertainty:
    return a if UNCERTAINTY_RANK[a] >= UNCERTAINTY_RANK[b] else b


class BBox(BaseModel):
    model_config = ConfigDict(frozen=True)
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    def as_xywh(self) -> dict[str, float]:
        return {"x": self.x0, "y": self.y0, "width": self.width, "height": self.height}

    def clamp(self, width: float, height: float) -> "BBox":
        return BBox(
            x0=min(max(0.0, self.x0), width),
            y0=min(max(0.0, self.y0), height),
            x1=min(max(0.0, self.x1), width),
            y1=min(max(0.0, self.y1), height),
        )


class TableCell(BaseModel):
    text: str = ""
    latex: str | None = None
    row_span: int = 1
    col_span: int = 1
    is_header: bool = False
    uncertain: bool = False

    @field_validator("text")
    @classmethod
    def _no_escaped_line_breaks(cls, v: str) -> str:
        """Models sometimes send a line break in a cell as the two characters backslash and n."""
        return re.sub(r"\\n(?![A-Za-z])", " ", v)


class TableData(BaseModel):
    """Rows of cells using the HTML convention for spans: a cell with ``row_span=n`` / ``col_span=m``
    appears once (in its top-left position); the positions it covers in later rows are omitted."""

    rows: list[list[TableCell]] = Field(default_factory=list)
    header_rows: int = 0
    caption: str | None = None
    label: str | None = None  # e.g. "Table 2.1"

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.as_grid()), default=0)

    def as_grid(self) -> list[list[str]]:
        """Rectangular text grid. A spanned cell's text is placed at its top-left position and the
        positions it covers are empty strings."""
        grid: list[list[str]] = []
        covered: dict[tuple[int, int], bool] = {}
        for ri, r in enumerate(self.rows):
            row: list[str] = []
            col = 0
            for c in r:
                while covered.get((ri, col)):
                    row.append("")
                    col += 1
                row.append(c.text)
                row.extend([""] * (c.col_span - 1))
                for dr in range(c.row_span):
                    for dc in range(c.col_span):
                        if dr or dc:
                            covered[(ri + dr, col + dc)] = True
                col += c.col_span
            while covered.get((ri, col)):
                row.append("")
                col += 1
            grid.append(row)
        width = max((len(r) for r in grid), default=0)
        return [r + [""] * (width - len(r)) for r in grid]


class FigureData(BaseModel):
    description: str = ""  # structured description written from the source; not a replacement
    labels: list[str] = Field(default_factory=list)  # text found inside the figure
    relationships: list[str] = Field(default_factory=list)  # e.g. "arrow from A to B"
    figure_label: str | None = None  # e.g. "Fig. 3"
    kind: str | None = None  # graph, circuit, geometry, chemical_structure, photo, ...
    # Paths relative to the document dir. The full-resolution crop itself is ``Element.source.region_image``.
    image_clean: str | None = None  # cleaned crop: paper/shadows whitened, ruled lines removed, trimmed
    # Where the cleaned crop sits on the page [x0, y0, x1, y1] in points, so exports can place it exactly: a stroke
    # split between two figures lines up again.
    image_clean_box: list[float] | None = None
    svg: str | None = None  # optional vector redraw (opt-in, model-made, sanitised); never replaces the crop
    # Deterministic check of ``svg`` against the crop it was drawn from (``pipeline/svg_fidelity.py``): score,
    # recall (crop ink the redraw covers), precision (redraw ink the crop supports), missing labels, passed.
    # Exports and the app use the SVG only when ``passed`` is true.
    svg_fidelity: dict[str, Any] | None = None
    redraw_attempts: int = 0  # failed checks in the current redraw (one retry is allowed)


class QuestionInfo(BaseModel):
    number: str | None = None  # as printed: "3", "3(a)", "iv"
    parent_number: str | None = None
    marks: str | None = None  # as printed: "5", "[2+3]"
    section: str | None = None  # "Section B"


class SourceRef(BaseModel):
    """Where an element came from. Paths are relative to the document's storage directory."""

    document_id: str
    page_number: int  # 1-based position in the document manifest
    source_file: str  # original filename
    source_page_index: int  # 0-based page index inside source_file
    page_image: str | None = None  # relative path to the original page render
    region_image: str | None = None  # relative path to the cropped region, when saved


class Revision(BaseModel):
    """Record of a change made by verification/review. The previous value is kept."""

    at: str
    by: str  # "review:<model>" or "rule:<name>"
    field: str
    previous: Any = None
    note: str | None = None


class Element(BaseModel):
    element_id: str
    type: ElementType
    role: Role = Role.NONE
    page_id: str
    page_number: int
    reading_order: int
    content: str = ""  # human-readable text exactly as written; for math, a readable linearisation
    # ``content`` with casing, punctuation and spacing normalised and obvious slips of the pen fixed (never other
    # words, numbers, symbols or math). None = not produced (exports then use ``content``).
    clean_content: str | None = None
    clean_content_source: str | None = None  # "model" (same call, passed the guard) | "rules" (deterministic)
    latex: str | None = None  # math/chemistry in LaTeX (mhchem \ce{} for chemistry)
    level: int | None = None  # heading level (1 = top)
    items: list[str] | None = None  # list items
    language: str | None = None
    table: TableData | None = None
    figure: FigureData | None = None
    question: QuestionInfo | None = None
    parent_id: str | None = None  # caption->figure, subquestion->question
    handwritten: bool = False
    bbox: BBox | None = None
    bbox_source: str | None = None  # "pdf_text_layer" | "model_estimate" | "refined"
    bbox_model: BBox | None = None  # the model's own estimate, kept when ``bbox`` was refined to the ink
    uncertainty: Uncertainty = Uncertainty.LOW
    uncertainty_reasons: list[str] = Field(default_factory=list)
    formatting: dict[str, Any] = Field(default_factory=dict)  # bold, italic, font, size, display
    extraction_method: str = "unknown"  # "embedded_text" | "docx" | "gemini" | "host_model" | "sampling"
    model: str | None = None
    source: SourceRef
    revisions: list[Revision] = Field(default_factory=list)

    def text_for_search(self) -> str:
        parts = [self.content]
        if self.latex and self.latex not in self.content:
            parts.append(self.latex)
        # content usually repeats the list items and table cells already: adding them would double the text
        shown = " ".join(self.content.split())
        if self.items and not all(" ".join(i.split()) in shown for i in self.items):
            parts.extend(self.items)
        if self.table:
            grid = self.table.as_grid()
            if not all(" ".join(c.split()) in shown for r in grid for c in r):
                parts.extend(" ".join(r) for r in grid)
        if self.figure:
            parts.extend(self.figure.labels)
        return "\n".join(p for p in parts if p)
