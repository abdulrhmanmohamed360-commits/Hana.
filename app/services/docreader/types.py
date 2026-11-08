"""Plain data types shared by the document-reading engine (no framework dependencies)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ExtractionError(Exception):
    """Base class. `code` is stable and safe to expose; `retryable` tells the worker whether a retry can help."""

    code = "EXTRACTION_ERROR"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None, retryable: bool | None = None,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        if code:
            self.code = code
        if retryable is not None:
            self.retryable = retryable
        self.details = details or {}


class UnsupportedFileError(ExtractionError):
    code = "UNSUPPORTED_FILE"


class FileValidationError(ExtractionError):
    code = "INVALID_FILE"


class CorruptedDocumentError(ExtractionError):
    code = "CORRUPTED_DOCUMENT"


class EmptyDocumentError(ExtractionError):
    code = "EMPTY_DOCUMENT"


class ResourceLimitError(ExtractionError):
    code = "RESOURCE_LIMIT"


class OcrUnavailableError(ExtractionError):
    code = "OCR_UNAVAILABLE"


@dataclass
class ReaderConfig:
    """Tunable limits and thresholds. Defaults are safe for a small worker container."""

    max_file_bytes: int = 25 * 1024 * 1024
    max_pages: int = 1500
    max_chars: int = 20_000_000
    # PDF quality gate
    min_page_chars: int = 25
    min_page_quality: float = 0.55
    # OCR
    ocr_enabled: bool = True
    ocr_languages: str = "ara+eng"
    ocr_dpi: int = 250
    ocr_max_pages: int = 400
    ocr_page_timeout_seconds: int = 90
    ocr_max_image_pixels: int = 60_000_000
    ocr_min_word_confidence: float = 30.0
    # Office / archive protection
    office_max_uncompressed_bytes: int = 200 * 1024 * 1024
    office_max_compression_ratio: int = 100
    office_max_members: int = 5000
    # Tabular
    max_sheet_rows: int = 100_000
    max_table_cells_per_chunk: int = 400
    # Optional hard wall-clock budget for one document (seconds); 0 disables.
    time_budget_seconds: int = 0


@dataclass
class Block:
    """A structural unit of a page: heading, paragraph, table, equation, caption, list, question..."""

    kind: str
    text: str
    page: int | None = None
    level: int = 0
    confidence: float = 1.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PageResult:
    """One page (or sheet/slide/section) of extracted content. `text` is ORIGINAL extracted text, never normalized."""

    page: int | None
    text: str
    method: str                      # pymupdf | pypdf | ocr | pymupdf+ocr | pypdf+ocr | native | blank | failed
    confidence: float
    language: str = "und"
    ocr_used: bool = False
    blank: bool = False
    failed: bool = False
    issues: list[str] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    engine_scores: dict[str, float] = field(default_factory=dict)
    label: str | None = None         # sheet name / slide title / section label for non-paged formats

    @property
    def has_text(self) -> bool:
        return bool(self.text and self.text.strip())


@dataclass
class ExtractionResult:
    file_type: str
    mime: str
    pages: list[PageResult]
    total_pages: int
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def text_pages(self) -> list[PageResult]:
        return [p for p in self.pages if p.has_text and not p.failed]

    @property
    def extracted_pages(self) -> int:
        return len(self.text_pages)

    @property
    def ocr_pages(self) -> int:
        return sum(1 for p in self.pages if p.ocr_used)

    @property
    def failed_pages(self) -> list[int]:
        return [p.page for p in self.pages if p.failed and p.page is not None]

    @property
    def character_count(self) -> int:
        return sum(len(p.text) for p in self.text_pages)

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text.strip() for p in self.text_pages)

    @property
    def methods_used(self) -> list[str]:
        return sorted({p.method for p in self.pages if p.method not in {"blank", "failed"}})

    @property
    def primary_method(self) -> str:
        counts: dict[str, int] = {}
        for p in self.text_pages:
            counts[p.method] = counts.get(p.method, 0) + len(p.text)
        return max(counts, key=counts.get) if counts else "none"

    @property
    def mean_confidence(self) -> float:
        pages = self.text_pages
        total = sum(len(p.text) for p in pages)
        if not total:
            return 0.0
        return round(sum(p.confidence * len(p.text) for p in pages) / total, 4)
