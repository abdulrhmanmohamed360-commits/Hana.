"""Image documents (PNG/JPG/TIFF): OCR only."""
from __future__ import annotations

from pathlib import Path

from .arabic import best_arabic_variant, clean_for_storage, detect_language
from .ocr import OcrEngine
from .structure import analyze_text
from .types import CorruptedDocumentError, PageResult, ReaderConfig, ResourceLimitError


def read_image(path: Path, config: ReaderConfig, ocr: OcrEngine):
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = config.ocr_max_image_pixels * 2  # PIL raises DecompressionBombError beyond 2x warn limit
    pages: list[PageResult] = []
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            frames = getattr(image, "n_frames", 1)
            if frames > config.max_pages:
                raise ResourceLimitError("Image has too many frames", code="TOO_MANY_PAGES")
            for index in range(frames):
                image.seek(index)
                frame = image.copy()
                number = index + 1
                issues: list[str] = []
                try:
                    out = ocr.recognize_image(frame)
                except TimeoutError:
                    pages.append(PageResult(number, "", "failed", 0.0, failed=True, issues=["ocr_timeout"]))
                    continue
                text, _, note = best_arabic_variant(out.text)
                if note:
                    issues.append(note)
                text = clean_for_storage(text)
                if not text:
                    pages.append(PageResult(number, "", "blank", 0.0, blank=True, issues=["no_text_recognized"]))
                    continue
                pages.append(PageResult(number, text, "ocr", round(out.confidence, 4), detect_language(text), True,
                                        issues=issues + out.issues, blocks=analyze_text(text, number, base_confidence=out.confidence),
                                        engine_scores={"ocr": out.confidence}))
    except (ResourceLimitError, CorruptedDocumentError):
        raise
    except Image.DecompressionBombError as exc:
        raise ResourceLimitError("Image is too large to process safely", code="IMAGE_TOO_LARGE") from exc
    except OSError as exc:
        raise CorruptedDocumentError("Image could not be decoded", code="IMAGE_MALFORMED") from exc
    return pages, {"frames": len(pages)}
