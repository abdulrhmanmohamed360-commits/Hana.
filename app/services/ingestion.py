"""Document intake, safe extraction, normalization and content-addressed deduplication."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pypdf import PdfReader

# Legacy in-process extractor supports only these; the worker pipeline (docreader) supports ALLOWED_EXTENSIONS.
LEGACY_EXTENSIONS = {".pdf", ".html", ".htm", ".txt", ".md", ".json"}
from app.services.docreader.security import SUPPORTED_EXTENSIONS as ALLOWED_EXTENSIONS  # noqa: E402


class IngestionError(ValueError):
    pass


def normalize_text(value: str) -> str:
    value = value.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    value = "\n".join(line.strip() for line in value.split("\n"))
    value = re.sub(r"[\t\f\v ]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def extract_segments(path: Path, original_name: str | None = None) -> tuple[list[tuple[str, int | None]], str]:
    """Return ordered text segments with 1-based PDF page provenance."""
    suffix = Path(original_name or path.name).suffix.lower()
    if suffix not in LEGACY_EXTENSIONS:
        raise IngestionError(f"Unsupported file type: {suffix or 'no extension'}")
    try:
        if suffix == ".pdf":
            reader = PdfReader(str(path))
            segments = [(page.extract_text() or "", index) for index, page in enumerate(reader.pages, start=1)]
            mime = "application/pdf"
        elif suffix in {".html", ".htm"}:
            raw = path.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(raw, "html.parser")
            for node in soup(["script", "style", "noscript", "svg"]):
                node.decompose()
            segments = [(soup.get_text("\n"), None)]
            mime = "text/html"
        elif suffix == ".json":
            raw = path.read_text(encoding="utf-8-sig")
            parsed: Any = json.loads(raw)
            segments = [(json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True), None)]
            mime = "application/json"
        else:
            raw = path.read_text(encoding="utf-8-sig", errors="replace")
            segments = [(raw, None)]
            mime = "text/markdown" if suffix == ".md" else "text/plain"
    except (OSError, ValueError) as exc:
        raise IngestionError(f"Could not extract document: {exc}") from exc
    segments = [(normalize_text(text), page) for text, page in segments if normalize_text(text)]
    if not segments:
        raise IngestionError("The document contains no extractable text")
    return segments, mime


def extract_text(path: Path, original_name: str | None = None) -> tuple[str, str]:
    """Compatibility helper returning the full normalized text and MIME type."""
    segments, mime = extract_segments(path, original_name)
    return normalize_text("\n\n".join(text for text, _ in segments)), mime


def store_upload(data: bytes, original_name: str, storage_dir: str, max_bytes: int) -> tuple[str, str]:
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise IngestionError(f"Unsupported file type: {suffix or 'no extension'}")
    if not data:
        raise IngestionError("Uploaded file is empty")
    if len(data) > max_bytes:
        raise IngestionError(f"File exceeds the {max_bytes}-byte upload limit")
    root = Path(storage_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{suffix}"
    target = (root / name).resolve()
    if root not in target.parents:
        raise IngestionError("Invalid storage path")
    target.write_bytes(data)
    return str(target), hashlib.sha256(data).hexdigest()
