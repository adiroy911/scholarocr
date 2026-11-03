"""Document-, page- and manifest-level schemas of the IR."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .elements import Element


class DocumentType(str, Enum):
    EXAM_PAPER = "exam_paper"
    PREVIOUS_YEAR_PAPER = "previous_year_paper"
    QUESTION_BANK = "question_bank"
    ANSWER_KEY = "answer_key"
    MARKING_SCHEME = "marking_scheme"
    TEXTBOOK = "textbook"
    LECTURE_NOTES = "lecture_notes"
    HANDWRITTEN_NOTES = "handwritten_notes"
    ASSIGNMENT = "assignment"
    WORKSHEET = "worksheet"
    LAB_RECORD = "lab_record"
    RESEARCH_PAPER = "research_paper"
    THESIS = "thesis"
    SCIENTIFIC_DOCUMENT = "scientific_document"
    PRESENTATION = "presentation"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class PageStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"
    BLANK = "blank"  # detected blank; no extraction needed
    FAILED = "failed"
    SKIPPED = "skipped"  # outside requested page range
    # Host mode: deterministic work is done and the page image is waiting for the MCP client's model
    # (get_page_image -> submit_page_content).
    AWAITING_TRANSCRIPTION = "awaiting_transcription"


class Route(str, Enum):
    """How a page is (or was) extracted."""

    BLANK = "blank"
    EMBEDDED_TEXT = "embedded_text"  # deterministic, no Gemini call
    VISUAL_GENERAL = "visual_general"
    VISUAL_MATH = "visual_math"
    VISUAL_HANDWRITING = "visual_handwriting"
    VISUAL_TABLE = "visual_table"
    DOCX_TEXT = "docx_text"


class QualityAssessment(BaseModel):
    """Deterministic page-image metrics (OpenCV). Thresholds documented in QUALITY_ASSURANCE.md."""

    sharpness: float | None = None  # variance of Laplacian
    contrast: float | None = None  # paper (90th pct) minus ink (10th pct of dark pixels), 0-255
    brightness: float | None = None  # mean grey level (0-255)
    ink_ratio: float | None = None  # fraction of dark pixels
    noise: float | None = None  # residual after median filter
    skew_degrees: float | None = None
    orientation_degrees: int | None = None  # suggested clockwise rotation (0/90/180/270), if detected
    effective_dpi: float | None = None
    glyph_height_px: float | None = None  # median character height in original-image pixels
    light_on_dark: bool = False  # light writing on a dark page (metrics computed on the inverse)
    is_blank: bool = False
    grade: str = "unknown"  # good | fair | poor | unknown
    issues: list[str] = Field(default_factory=list)


class Transform(BaseModel):
    """A preprocessing step applied to produce the processed page image."""

    step: str
    params: dict[str, Any] = Field(default_factory=dict)


class PageProfile(BaseModel):
    """Signals used to route a page. Computed deterministically during ingestion."""

    has_text_layer: bool = False
    text_chars: int = 0
    text_quality: float = 0.0  # fraction of "clean" characters in the text layer (0..1)
    image_coverage: float = 0.0  # fraction of page area covered by raster images
    math_font_signals: int = 0  # spans in TeX/Symbol math fonts
    vector_drawings: int = 0
    is_scanned: bool = False
    is_blank: bool = False


class ManifestEntry(BaseModel):
    page_number: int  # 1-based position in the logical document (never reordered)
    page_id: str
    source_file: str
    source_file_index: int
    source_page_index: int  # 0-based index in that source file
    width_pt: float
    height_pt: float
    rotation: int = 0  # /Rotate already applied by the PDF viewer
    image_dpi: float | None = None
    content_hash: str  # sha256 of rendered pixels (low-res) or of the image file
    dhash: str | None = None  # perceptual hash for near-duplicate detection
    text_hash: str | None = None
    profile: PageProfile = Field(default_factory=PageProfile)
    duplicate_of: int | None = None  # page_number of the earlier identical/near-identical page
    notes: list[str] = Field(default_factory=list)


class SourceFile(BaseModel):
    index: int
    original_path: str  # as provided by the caller (for traceability only)
    filename: str
    stored_path: str  # relative to document dir; preserved copy
    sha256: str
    size_bytes: int
    media_type: str
    page_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class Manifest(BaseModel):
    document_id: str
    sources: list[SourceFile]
    pages: list[ManifestEntry]
    warnings: list[str] = Field(default_factory=list)


class Page(BaseModel):
    page_id: str
    document_id: str
    page_number: int
    width_pt: float
    height_pt: float
    status: PageStatus = PageStatus.PENDING
    route: Route | None = None
    orientation: int = 0
    quality: QualityAssessment = Field(default_factory=QualityAssessment)
    transforms: list[Transform] = Field(default_factory=list)
    original_image: str | None = None  # relative path
    processed_image: str | None = None  # relative path (what Gemini saw)
    processed_image_size: tuple[int, int] | None = None  # (width_px, height_px)
    elements: list[Element] = Field(default_factory=list)
    language: str | None = None
    page_label: str | None = None  # printed page number, if found
    cache_key: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    processed_at: str | None = None
    layout_refined_at: str | None = None  # when boxes were last snapped to the ink (pipeline/layout_refine)

    @property
    def reading_order(self) -> list[str]:
        return [e.element_id for e in sorted(self.elements, key=lambda e: e.reading_order)]


class ProcessingOptions(BaseModel):
    document_type: DocumentType | None = None
    language_hint: str | None = None
    context_hint: str | None = None  # free text from the calling agent; used as a hint, not fact
    pages: str | None = None  # "1-5,8"
    detail: str = "standard"  # "standard" | "high" (high = always run targeted review)
    force_visual: bool = False  # ignore text layer, always run visual OCR
    auto_crop: bool = True  # crop images to a detected document region (screenshots, photos)
    review: bool = True  # targeted second-pass review of flagged elements
    reprocess: bool = False  # ignore cache

    def fingerprint(self) -> dict[str, Any]:
        """Fields that change extraction output (part of the cache key)."""
        return {
            "document_type": self.document_type.value if self.document_type else None,
            "language_hint": self.language_hint,
            "context_hint": self.context_hint,
            "detail": self.detail,
            "force_visual": self.force_visual,
            "auto_crop": self.auto_crop,
            "review": self.review,
        }


class Document(BaseModel):
    document_id: str
    title: str | None = None
    original_filenames: list[str]
    document_type: DocumentType = DocumentType.UNKNOWN
    languages: list[str] = Field(default_factory=list)
    page_count: int
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    processing: dict[str, Any] = Field(default_factory=dict)
    status: str = "ingested"
    created_at: str
    updated_at: str | None = None
    extraction_version: str
    schema_version: str
    pages: list[Page] = Field(default_factory=list)

    def iter_elements(self):
        for p in self.pages:
            yield from sorted(p.elements, key=lambda e: e.reading_order)
