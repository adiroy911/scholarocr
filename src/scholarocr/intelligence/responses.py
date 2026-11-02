"""Schemas for what Gemini returns. Kept deliberately flatter than the IR (shallow nesting,
few optional branches) so the JSON Schema stays well inside Gemini's structured-output limits.
The pipeline converts these into IR elements; nothing here is stored directly.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ExtractedType = Literal[
    "title", "heading", "subheading", "paragraph", "text", "handwriting", "equation", "chemical_equation",
    "table", "figure", "diagram", "image", "caption", "footnote", "header", "footer", "page_number", "list",
    "code", "bibliography_entry", "reference", "uncertain",
]
ExtractedRole = Literal[
    "none", "chapter", "section", "question", "subquestion", "instruction", "exercise", "definition", "theorem",
    "lemma", "example", "proof", "solution", "answer", "abstract", "authors", "affiliation", "keywords",
    "marginal_note",
]
UncertaintyLabel = Literal["low", "medium", "high", "unresolved"]


class XCell(BaseModel):
    text: str = Field(description="Cell text exactly as printed; empty string if the cell is empty.")
    latex: str | None = Field(default=None, description="LaTeX if the cell contains math.")
    row_span: int = 1
    col_span: int = 1
    is_header: bool = False
    uncertain: bool = Field(default=False, description="True if any character in the cell is not clearly legible.")


class XTable(BaseModel):
    rows: list[list[XCell]] = Field(description="Rows top to bottom; cells left to right. Merged cells appear once with spans.")
    header_rows: int = 0
    caption: str | None = None
    label: str | None = Field(default=None, description='Printed label such as "Table 2.1".')


class XFigure(BaseModel):
    description: str = Field(description="Factual description of what the figure shows. Do not invent details.")
    labels: list[str] = Field(default_factory=list, description="All text that appears inside the figure.")
    relationships: list[str] = Field(default_factory=list, description='Arrows/connections, e.g. "arrow from A to B".')
    figure_label: str | None = Field(default=None, description='Printed label such as "Fig. 3".')
    kind: str | None = Field(default=None, description="graph, circuit, geometry, chemical_structure, flowchart, photo, other")


class XQuestion(BaseModel):
    number: str | None = Field(default=None, description='Question number exactly as printed, e.g. "3", "3(a)", "iv".')
    parent_number: str | None = None
    marks: str | None = Field(default=None, description='Marks exactly as printed, e.g. "5" or "2+3".')
    section: str | None = None


class XElement(BaseModel):
    type: ExtractedType
    role: ExtractedRole = "none"
    text: str = Field(default="", description="Transcribed text in reading order. For math, a readable linear form.")
    latex: str | None = Field(default=None, description="LaTeX for equation/chemical_equation (use \\ce{} for chemistry).")
    level: int | None = Field(default=None, description="Heading level, 1 = top.")
    items: list[str] | None = Field(default=None, description="List items, for type=list.")
    table: XTable | None = None
    figure: XFigure | None = None
    question: XQuestion | None = None
    box_2d: list[int] | None = Field(
        default=None, description="[ymin, xmin, ymax, xmax] normalised to 0-1000 relative to the image."
    )
    uncertainty: UncertaintyLabel = Field(
        default="low",
        description="low = clearly legible; medium = minor doubt; high = substantial doubt; unresolved = cannot be read.",
    )
    uncertainty_reasons: list[str] = Field(default_factory=list)
    handwritten: bool = False
    language: str | None = Field(default=None, description="BCP-47 code, e.g. en, hi, fr.")
    caption_of: int | None = Field(default=None, description="For captions: index (0-based) of the element captioned.")
    parent_index: int | None = Field(default=None, description="For subquestions: index of the parent question element.")


class PageExtractionResponse(BaseModel):
    elements: list[XElement]
    page_language: str | None = None
    page_label: str | None = Field(default=None, description="Printed page number, if visible.")
    rotation_detected: int = Field(default=0, description="Clockwise degrees (0/90/180/270) the image appears rotated.")
    legibility: UncertaintyLabel = Field(default="low", description="Overall legibility of the page.")
    notes: list[str] = Field(default_factory=list, description="Problems such as cut-off text or missing regions.")


class ReviewResponse(BaseModel):
    verdict: Literal["confirmed", "corrected", "unreadable"]
    text: str = ""
    latex: str | None = None
    table: XTable | None = None
    uncertainty: UncertaintyLabel = "low"
    notes: list[str] = Field(default_factory=list)


class ClassificationResponse(BaseModel):
    document_type: Literal[
        "exam_paper", "previous_year_paper", "question_bank", "answer_key", "marking_scheme", "textbook",
        "lecture_notes", "handwritten_notes", "assignment", "worksheet", "lab_record", "research_paper", "thesis",
        "scientific_document", "presentation", "mixed", "unknown",
    ]
    subjects: list[str] = Field(default_factory=list)
    level: str | None = Field(default=None, description="e.g. Class 11, undergraduate, graduate")
    languages: list[str] = Field(default_factory=list)
    chapters_or_topics: list[str] = Field(default_factory=list)
    certainty: UncertaintyLabel = "medium"
    rationale: str = ""
    hint_consistent: bool | None = Field(default=None, description="Whether the caller's hint matches the evidence.")
    evidence_pages: list[int] = Field(default_factory=list)
