"""Extraction router: validates, selects the strategy per file type, applies fallbacks and returns a unified result."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from .arabic import assess_arabic
from .ocr import OcrEngine
from .security import detect_file_type, validate_declared_mime
from .types import (EmptyDocumentError, ExtractionError, ExtractionResult, FileValidationError, ReaderConfig,
                    ResourceLimitError)

log = logging.getLogger("eduforge.router")
_TRANSIENT = (OSError, TimeoutError)


def _dispatch(kind: str, path: Path, config: ReaderConfig, ocr: OcrEngine) -> tuple[list, str, dict]:
    if kind == "pdf":
        from .pdf import extract_pdf

        result = extract_pdf(path, config, ocr)
        return result.pages, result.mime, {"__result__": result}
    if kind in {"txt", "md"}:
        from .textual import read_txt

        return read_txt(path, config, kind)
    if kind == "html":
        from .textual import read_html

        return read_html(path, config)
    if kind == "json":
        from .textual import read_json

        return read_json(path, config)
    if kind == "csv":
        from .textual import read_csv

        return read_csv(path, config)
    if kind == "docx":
        from .office import read_docx

        pages, meta = read_docx(path, config)
        return pages, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", meta
    if kind == "doc":
        from .office import read_doc

        pages, meta = read_doc(path, config)
        return pages, "application/msword", meta
    if kind == "xlsx":
        from .office import read_xlsx

        pages, meta = read_xlsx(path, config)
        return pages, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", meta
    if kind == "pptx":
        from .office import read_pptx

        pages, meta = read_pptx(path, config)
        return pages, "application/vnd.openxmlformats-officedocument.presentationml.presentation", meta
    if kind == "image":
        from .images import read_image

        pages, meta = read_image(path, config, ocr)
        return pages, "image/*", meta
    raise FileValidationError(f"No reader for type {kind}", code="UNSUPPORTED_FILE")


def extract_document(path: Path, original_name: str | None, config: ReaderConfig | None = None,
                     ocr: OcrEngine | None = None, declared_mime: str | None = None,
                     max_attempts: int = 2) -> ExtractionResult:
    """Validate -> route -> extract -> verify content. Raises ExtractionError subclasses with stable `code`s.

    Transient OS-level failures are retried in-process (bounded); everything else is deterministic and is reported
    immediately. A document is only returned when it contains real extracted text."""
    config = config or ReaderConfig()
    ocr = ocr or OcrEngine(config)
    path = Path(path)
    started = time.monotonic()

    kind, mime = detect_file_type(path, original_name, config)
    validate_declared_mime(declared_mime, mime)

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            pages, out_mime, meta = _dispatch(kind, path, config, ocr)
            break
        except ExtractionError:
            raise
        except _TRANSIENT as exc:
            last_exc = exc
            log.warning("extraction_transient_error", extra={"attempt": attempt, "error": type(exc).__name__})
            if attempt == max_attempts:
                raise ExtractionError("Temporary failure while reading the document", code="EXTRACTION_TRANSIENT",
                                      retryable=True) from exc
            time.sleep(min(2 * attempt, 5))
    else:  # pragma: no cover
        raise ExtractionError("Extraction failed", retryable=True) from last_exc

    if "__result__" in meta:
        result: ExtractionResult = meta["__result__"]
    else:
        for number, page in enumerate(pages, start=1):
            if page.page is None and kind in {"xlsx", "pptx", "image"}:
                page.page = number
        result = ExtractionResult(kind, out_mime if out_mime != "image/*" else mime, pages,
                                  len(pages), {k: v for k, v in meta.items()})
    result.mime = mime if kind == "image" else result.mime
    result.duration_seconds = round(time.monotonic() - started, 3)

    total_chars = result.character_count
    if total_chars > config.max_chars:
        raise ResourceLimitError("Extracted text exceeds the allowed size", code="TEXT_TOO_LARGE")
    if total_chars == 0:
        reasons = sorted({i for p in result.pages for i in p.issues})
        code = "OCR_REQUIRED_BUT_UNAVAILABLE" if any(i.startswith("ocr_needed_but_unavailable") for i in reasons) \
            or (result.warnings and any("ocr" in w or "tesseract" in w for w in result.warnings)) else "EMPTY_DOCUMENT"
        raise EmptyDocumentError("No valid text could be extracted from the document", code=code,
                                 details={"total_pages": result.total_pages, "failed_pages": result.failed_pages,
                                          "issues": reasons[:20], "warnings": result.warnings[:10]})
    return result


def arabic_quality_summary(result: ExtractionResult) -> dict:
    """Mean Arabic quality over pages that contain Arabic (for metrics)."""
    scores = []
    for page in result.text_pages:
        q = assess_arabic(page.text)
        if q.arabic_chars >= 20:
            scores.append(q.score)
    return {"pages_with_arabic": len(scores), "mean_score": round(sum(scores) / len(scores), 4) if scores else None}
