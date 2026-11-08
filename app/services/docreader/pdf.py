"""Multi-engine PDF extraction with per-page quality evaluation, pypdf fallback and selective OCR."""
from __future__ import annotations

import logging
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .arabic import (assess_arabic, best_arabic_variant, clean_for_storage, detect_language, normalize_for_search)
from .ocr import OcrEngine
from .quality import PageQuality, evaluate_text
from .structure import analyze_text, margin_key, strip_repeating_margins, table_to_markdown
from .types import (Block, CorruptedDocumentError, EmptyDocumentError, ExtractionResult, PageResult, ReaderConfig,
                    ResourceLimitError)

log = logging.getLogger("eduforge.pdf")

try:  # PyMuPDF is the primary engine; the module still imports without it so pypdf-only mode keeps working.
    import fitz  # type: ignore
except Exception:  # pragma: no cover - depends on deployment
    fitz = None


# --------------------------------------------------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------------------------------------------------
def merge_texts(base: str, extra: str, *, threshold: float = 0.88) -> str:
    """Append lines of `extra` that are not already represented in `base` (exact-normalized or near-duplicate).
    Used to add OCR text for image-only regions of a page whose text layer is only partially present."""
    base_lines = [ln for ln in base.split("\n") if ln.strip()]
    keys = {normalize_for_search(ln) for ln in base_lines}
    key_list = [k for k in keys if k]
    additions: list[str] = []
    for line in extra.split("\n"):
        if not line.strip():
            continue
        key = normalize_for_search(line)
        if not key or key in keys:
            continue
        duplicate = False
        for other in key_list:
            if abs(len(other) - len(key)) > max(6, len(key) * 0.3):
                continue
            if SequenceMatcher(None, key, other).quick_ratio() >= threshold and \
                    SequenceMatcher(None, key, other).ratio() >= threshold:
                duplicate = True
                break
        if not duplicate:
            additions.append(line.strip())
            keys.add(key)
            key_list.append(key)
    if not additions:
        return base
    return (base.rstrip() + "\n" + "\n".join(additions)).strip()


# --------------------------------------------------------------------------------------------------------------------
# engines
# --------------------------------------------------------------------------------------------------------------------
def _pymupdf_page(page: Any, want_tables: bool) -> tuple[str, list[Block], bool, list[str]]:
    """Return (text, table_blocks, has_images, issues)."""
    issues: list[str] = []
    table_blocks: list[Block] = []
    has_images = False
    try:
        has_images = bool(page.get_images(full=False))
    except Exception:
        pass
    table_rects = []
    if want_tables and hasattr(page, "find_tables"):
        try:
            finder = page.find_tables()
            for table in getattr(finder, "tables", []):
                rows = table.extract()
                md = table_to_markdown(rows)
                if md:
                    table_blocks.append(Block("table", md, None, 0, 0.8,
                                              {"rows": len(rows), "cols": max((len(r) for r in rows), default=0),
                                               "engine": "pymupdf.find_tables", "bbox": [round(v, 1) for v in table.bbox]}))
                    table_rects.append(fitz.Rect(table.bbox))
        except Exception as exc:
            issues.append(f"table_detection_failed:{type(exc).__name__}")
    if table_rects:
        parts = []
        for block in page.get_text("blocks", sort=True):
            if block[6] != 0:  # image block
                continue
            rect = fitz.Rect(block[:4])
            area = rect.get_area() or 1.0
            if any((rect & tr).get_area() / area > 0.6 for tr in table_rects):
                continue
            parts.append(block[4].strip())
        text = "\n".join(p for p in parts if p)
    else:
        text = page.get_text("text", sort=True) or ""
    return text, table_blocks, has_images, issues


def _pypdf_page_text(reader: Any, index: int) -> str:
    try:
        return reader.pages[index].extract_text() or ""
    except Exception as exc:
        log.info("pypdf_page_failed", extra={"page": index + 1, "error": type(exc).__name__})
        return ""


def _render_page_image(doc: Any, index: int, dpi: int, max_pixels: int):
    from PIL import Image

    page = doc[index]
    zoom = dpi / 72.0
    rect = page.rect
    est = rect.width * zoom * rect.height * zoom
    if est > max_pixels:
        zoom *= (max_pixels / est) ** 0.5
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)


def _pypdf_embedded_images(reader: Any, index: int, limit: int = 4):
    """OCR source when PyMuPDF is unavailable: embedded raster images of the page (best effort)."""
    from PIL import Image
    import io

    images = []
    try:
        for img in list(reader.pages[index].images)[:limit]:
            images.append(Image.open(io.BytesIO(img.data)))
    except Exception:
        pass
    return images


# --------------------------------------------------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------------------------------------------------
def extract_pdf(path: Path, config: ReaderConfig, ocr: OcrEngine) -> ExtractionResult:
    started = time.monotonic()
    warnings: list[str] = []
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except Exception as exc:  # pragma: no cover
        raise CorruptedDocumentError("pypdf is not installed", code="ENGINE_MISSING") from exc

    doc = None
    metadata: dict[str, Any] = {}
    reader = None
    total_pages = 0

    # 1. inspect metadata and page count (PyMuPDF first, pypdf second)
    if fitz is not None:
        try:
            doc = fitz.open(str(path))
            if doc.needs_pass:
                if not doc.authenticate(""):
                    raise CorruptedDocumentError("PDF is password protected", code="PDF_ENCRYPTED")
            total_pages = doc.page_count
            metadata = {k: v for k, v in (doc.metadata or {}).items() if v}
        except CorruptedDocumentError:
            raise
        except Exception as exc:
            warnings.append(f"pymupdf_open_failed:{type(exc).__name__}")
            doc = None
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise CorruptedDocumentError("PDF is password protected", code="PDF_ENCRYPTED")
            except CorruptedDocumentError:
                raise
            except Exception as exc:
                raise CorruptedDocumentError("PDF is encrypted and cannot be opened", code="PDF_ENCRYPTED") from exc
        if not total_pages:
            total_pages = len(reader.pages)
        if not metadata:
            metadata = {str(k).lstrip("/"): str(v) for k, v in (reader.metadata or {}).items() if v}
    except CorruptedDocumentError:
        raise
    except (PdfReadError, Exception) as exc:
        if doc is None:
            raise CorruptedDocumentError("PDF is malformed and could not be opened by any engine",
                                         code="PDF_MALFORMED", details={"error": type(exc).__name__}) from exc
        warnings.append(f"pypdf_open_failed:{type(exc).__name__}")
        reader = None

    if total_pages <= 0:
        raise EmptyDocumentError("PDF has no pages", code="PDF_NO_PAGES")
    if total_pages > config.max_pages:
        raise ResourceLimitError(f"PDF has {total_pages} pages; the limit is {config.max_pages}", code="TOO_MANY_PAGES")

    pages: list[PageResult] = []
    ocr_budget = config.ocr_max_pages
    ocr_notes_added = False

    for index in range(total_pages):
        if config.time_budget_seconds and time.monotonic() - started > config.time_budget_seconds:
            raise ResourceLimitError("Document processing time budget exceeded", code="TIME_BUDGET", retryable=True)
        number = index + 1
        issues: list[str] = []
        engine_scores: dict[str, float] = {}
        candidates: list[tuple[str, str, PageQuality]] = []   # (method, text, quality)
        table_blocks: list[Block] = []
        has_images: bool | None = None

        try:
            # 2. PyMuPDF
            if doc is not None:
                try:
                    text, table_blocks, has_images, page_issues = _pymupdf_page(doc[index], want_tables=True)
                    issues += page_issues
                    q = evaluate_text(text, min_chars=config.min_page_chars, min_quality=config.min_page_quality)
                    candidates.append(("pymupdf", text, q))
                    engine_scores["pymupdf"] = q.score
                except Exception as exc:
                    issues.append(f"pymupdf_page_failed:{type(exc).__name__}")
            # 3. pypdf fallback only when the first engine is missing or weak
            first_ok = bool(candidates) and not candidates[0][2].needs_better
            if not first_ok and reader is not None:
                text = _pypdf_page_text(reader, index)
                q = evaluate_text(text, min_chars=config.min_page_chars, min_quality=config.min_page_quality)
                candidates.append(("pypdf", text, q))
                engine_scores["pypdf"] = q.score

            best_method, best_text, best_q = max(candidates, key=lambda c: c[2].score) if candidates else ("none", "", evaluate_text(""))

            # Arabic repair on the winning candidate (original kept in meta when changed)
            repair_note = None
            original_text = best_text
            if best_text:
                fixed, ar_q, repair_note = best_arabic_variant(best_text)
                if repair_note:
                    best_text = fixed
                    best_q = evaluate_text(best_text, min_chars=config.min_page_chars, min_quality=config.min_page_quality)
                    issues.append(repair_note)

            # 6. OCR only where needed. Blank pages (no text, no images) are not OCR'd.
            ocr_used = False
            method = best_method
            final_text = best_text
            confidence = best_q.score
            truly_blank = (best_q.char_count == 0 and has_images is False and not table_blocks)
            needs_ocr = best_q.needs_better and not truly_blank
            if needs_ocr:
                if not config.ocr_enabled or not ocr.available:
                    issues.append("ocr_needed_but_unavailable")
                    if not ocr_notes_added:
                        warnings.extend(ocr.issues or ["ocr_disabled"])
                        ocr_notes_added = True
                elif ocr_budget <= 0:
                    issues.append("ocr_page_budget_exhausted")
                else:
                    ocr_budget -= 1
                    ocr_out = _ocr_pdf_page(doc, reader, index, config, ocr, issues)
                    if ocr_out is not None and ocr_out.text.strip():
                        ocr_used = True
                        ocr_conf = round(min(1.0, ocr_out.confidence), 4)
                        engine_scores["ocr"] = ocr_conf
                        if best_q.char_count >= config.min_page_chars and best_q.score >= 0.35:
                            final_text = merge_texts(best_text, ocr_out.text)   # 7. merge without duplicating
                            method = f"{best_method}+ocr"
                            confidence = round(max(best_q.score, (best_q.score + ocr_conf) / 2), 4)
                        else:
                            final_text = ocr_out.text
                            method = "ocr"
                            confidence = ocr_conf
                        fixed, _, note = best_arabic_variant(final_text)
                        if note:
                            final_text = fixed
                            issues.append(note)
            if not final_text.strip() and not table_blocks:
                pages.append(PageResult(number, "", "blank" if truly_blank else "failed", 0.0,
                                        blank=truly_blank, failed=not truly_blank,
                                        issues=issues + (["blank_page"] if truly_blank else ["no_text_recovered"]),
                                        engine_scores=engine_scores))
                continue

            stored = clean_for_storage(final_text)
            blocks = analyze_text(stored, number, base_confidence=confidence)
            for tb in table_blocks:
                tb.page = number
                tb.confidence = round(min(tb.confidence, max(confidence, 0.5)), 4)
                blocks.append(tb)
            if repair_note:
                for b in blocks:
                    b.meta.setdefault("original_text_available", True)
            ar = assess_arabic(stored)
            if ar.arabic_chars >= 20:
                engine_scores["arabic_quality"] = ar.score
                if ar.score < 0.8:
                    issues += [f"arabic:{i}" for i in ar.issues]
            page_result = PageResult(number, stored, method, round(confidence, 4), detect_language(stored), ocr_used,
                                     False, False, issues, blocks, engine_scores)
            if repair_note:
                page_result.issues.append("original_pre_repair_text_length:%d" % len(original_text))
            pages.append(page_result)
        except Exception as exc:  # one bad page must not sink the document
            log.warning("pdf_page_failed", extra={"page": number, "error": type(exc).__name__})
            pages.append(PageResult(number, "", "failed", 0.0, failed=True,
                                    issues=issues + [f"page_exception:{type(exc).__name__}"]))

    if doc is not None:
        doc.close()

    _drop_repeating_margin_blocks(pages)
    return ExtractionResult("pdf", "application/pdf", pages, total_pages, metadata, warnings,
                            round(time.monotonic() - started, 3))


def _ocr_pdf_page(doc, reader, index, config, ocr, issues):
    """OCR one page with a single lower-DPI retry on timeout. Returns OcrOutput or None."""
    attempts = [config.ocr_dpi, max(150, config.ocr_dpi // 2)]
    for attempt, dpi in enumerate(attempts):
        try:
            if doc is not None and fitz is not None:
                image = _render_page_image(doc, index, dpi, config.ocr_max_image_pixels)
                return ocr.recognize_image(image)
            images = _pypdf_embedded_images(reader, index) if reader is not None else []
            if not images:
                issues.append("ocr_no_renderable_image")
                return None
            outputs = [ocr.recognize_image(img) for img in images]
            text = "\n".join(o.text for o in outputs if o.text)
            conf = sum(o.confidence for o in outputs) / len(outputs)
            from .ocr import OcrOutput
            return OcrOutput(text, conf, outputs[0].languages, sum(o.words for o in outputs), outputs[0].issues)
        except TimeoutError:
            issues.append(f"ocr_timeout_dpi_{dpi}")
            continue
        except Exception as exc:
            issues.append(f"ocr_failed:{type(exc).__name__}")
            return None
    return None


def _drop_repeating_margin_blocks(pages: list[PageResult]) -> None:
    """Remove running headers/footers/page-numbers from structured blocks (original page.text keeps them)."""
    lines = [[ln for ln in p.text.split("\n")] for p in pages if p.has_text]
    repeated = strip_repeating_margins(lines)
    if not repeated:
        return
    for p in pages:
        for block in p.blocks:
            if block.kind in {"paragraph", "list"}:
                kept = [ln for ln in block.text.split("\n") if margin_key(ln) not in repeated]
                if len(kept) != len(block.text.split("\n")):
                    block.text = "\n".join(kept).strip()
                    block.meta["margin_lines_removed"] = True
        p.blocks = [b for b in p.blocks if b.text.strip()]
