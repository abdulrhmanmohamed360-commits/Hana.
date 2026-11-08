"""Tesseract OCR wrapper with resource limits, language fallback and confidence estimation."""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any

from .types import OcrUnavailableError, ReaderConfig

log = logging.getLogger("eduforge.ocr")


@dataclass
class OcrOutput:
    text: str
    confidence: float          # 0..1 mean word confidence
    languages: str
    words: int
    issues: list[str]


class OcrEngine:
    """Lazy wrapper. Tests can inject a fake with the same `recognize_image` / `available` interface."""

    def __init__(self, config: ReaderConfig):
        self.config = config
        self._langs: str | None = None
        self._issues: list[str] = []
        self._ok: bool | None = None

    # -- availability ---------------------------------------------------------------------------------------------
    def _probe(self) -> None:
        if self._ok is not None:
            return
        try:
            import pytesseract

            installed = set(pytesseract.get_languages(config=""))
        except Exception as exc:  # binary missing, permissions, etc.
            log.warning("ocr_unavailable", extra={"reason": type(exc).__name__})
            self._ok = False
            self._issues.append("tesseract_not_installed")
            return
        wanted = [code for code in self.config.ocr_languages.split("+") if code]
        usable = [code for code in wanted if code in installed]
        missing = [code for code in wanted if code not in installed]
        for code in missing:
            self._issues.append(f"ocr_language_missing:{code}")
            log.warning("ocr_language_missing", extra={"language": code})
        if not usable:
            self._ok = False
            return
        self._langs = "+".join(usable)
        self._ok = True

    @property
    def available(self) -> bool:
        if not self.config.ocr_enabled:
            return False
        self._probe()
        return bool(self._ok)

    @property
    def issues(self) -> list[str]:
        self._probe()
        return list(self._issues)

    @property
    def languages(self) -> str:
        self._probe()
        return self._langs or ""

    # -- recognition ----------------------------------------------------------------------------------------------
    def recognize_image(self, image: Any, *, timeout: int | None = None) -> OcrOutput:
        """OCR a PIL image. Raises OcrUnavailableError when OCR cannot run; TimeoutError on timeout."""
        if not self.available:
            raise OcrUnavailableError("OCR is not available on this worker", retryable=False,
                                      details={"issues": self.issues})
        import pytesseract
        from PIL import Image, ImageOps

        img = self._prepare(image, Image, ImageOps)
        timeout = timeout or self.config.ocr_page_timeout_seconds
        try:
            data = pytesseract.image_to_data(img, lang=self._langs, config="--psm 3 --oem 1",
                                             output_type=pytesseract.Output.DICT, timeout=timeout)
        except RuntimeError as exc:  # pytesseract raises RuntimeError("Tesseract process timeout")
            if "timeout" in str(exc).lower():
                raise TimeoutError("OCR timed out") from exc
            raise
        return self._assemble(data)

    def _prepare(self, image: Any, Image: Any, ImageOps: Any):
        pixels = image.width * image.height
        if pixels > self.config.ocr_max_image_pixels:
            scale = (self.config.ocr_max_image_pixels / pixels) ** 0.5
            image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))),
                                 Image.Resampling.LANCZOS)
        if image.mode not in ("L", "RGB"):
            image = image.convert("RGB")
        gray = ImageOps.grayscale(image)
        return ImageOps.autocontrast(gray)

    def _assemble(self, data: dict) -> OcrOutput:
        lines: dict[tuple, list[str]] = {}
        order: list[tuple] = []
        confs: list[float] = []
        words = 0
        for i, raw in enumerate(data.get("text", [])):
            word = (raw or "").strip()
            if not word:
                continue
            try:
                conf = float(data["conf"][i])
            except (ValueError, KeyError, TypeError):
                conf = -1.0
            if conf < 0:
                continue
            if conf < self.config.ocr_min_word_confidence and len(word) <= 1:
                continue  # drop isolated low-confidence specks
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            if key not in lines:
                lines[key] = []
                order.append(key)
            lines[key].append(word)
            confs.append(conf)
            words += 1
        text_lines = []
        prev_par = None
        for key in order:
            par = key[:2]
            if prev_par is not None and par != prev_par:
                text_lines.append("")
            text_lines.append(" ".join(lines[key]))
            prev_par = par
        mean = (sum(confs) / len(confs) / 100.0) if confs else 0.0
        return OcrOutput("\n".join(text_lines).strip(), round(mean, 4), self._langs or "", words, self.issues)


def render_to_png_bytes(image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
