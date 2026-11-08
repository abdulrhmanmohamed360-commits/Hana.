"""Upload validation: size, filename, magic-bytes type detection, archive-bomb limits. Never trusts name or MIME alone."""
from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path

from .types import FileValidationError, ReaderConfig, ResourceLimitError, UnsupportedFileError

# extension -> (logical type, canonical MIME)
SUPPORTED: dict[str, tuple[str, str]] = {
    ".pdf": ("pdf", "application/pdf"),
    ".docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".doc": ("doc", "application/msword"),
    ".txt": ("txt", "text/plain"),
    ".md": ("md", "text/markdown"),
    ".markdown": ("md", "text/markdown"),
    ".html": ("html", "text/html"),
    ".htm": ("html", "text/html"),
    ".json": ("json", "application/json"),
    ".csv": ("csv", "text/csv"),
    ".xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".pptx": ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".tif": ("image", "image/tiff"),
    ".tiff": ("image", "image/tiff"),
}
SUPPORTED_EXTENSIONS = frozenset(SUPPORTED)

_TEXT_TYPES = {"txt", "md", "html", "json", "csv"}
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06")
_OOXML_ROOT = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}
_BAD_NAME_CHARS = re.compile(r"[^\w.\- \u0600-\u06ff]", re.UNICODE)


def safe_filename(name: str | None, default: str = "document") -> str:
    """Strip directories, control chars and unsafe characters; bound the length; keep Arabic letters."""
    base = (name or "").replace("\\", "/").split("/")[-1].strip().strip(".")
    base = _BAD_NAME_CHARS.sub("_", base)
    base = re.sub(r"\s+", " ", base).strip(" ._")
    if not base:
        base = default
    stem, dot, ext = base.rpartition(".")
    if dot and len(ext) <= 8:
        return f"{stem[:120] or default}.{ext.lower()}"
    return base[:120]


def extension_of(name: str | None) -> str:
    return Path(safe_filename(name)).suffix.lower()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def _head(path: Path, n: int = 4096) -> bytes:
    with path.open("rb") as handle:
        return handle.read(n)


def check_zip_safety(path: Path, config: ReaderConfig) -> list[str]:
    """Reject ZIP-bombs and path-traversal members in OOXML containers. Returns member names."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > config.office_max_members:
                raise ResourceLimitError("Archive has too many members", code="ARCHIVE_TOO_MANY_MEMBERS")
            total = 0
            names: list[str] = []
            compressed_total = max(1, path.stat().st_size)
            for info in infos:
                name = info.filename
                if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                    raise FileValidationError("Archive contains unsafe member paths", code="ARCHIVE_UNSAFE_PATH")
                if info.flag_bits & 0x1:
                    raise FileValidationError("Encrypted archives are not supported", code="ARCHIVE_ENCRYPTED")
                total += info.file_size
                if total > config.office_max_uncompressed_bytes:
                    raise ResourceLimitError("Archive expands beyond the allowed size", code="ARCHIVE_TOO_LARGE")
                if info.compress_size and info.file_size / max(1, info.compress_size) > config.office_max_compression_ratio \
                        and info.file_size > 1024 * 1024:
                    raise ResourceLimitError("Archive member has a suspicious compression ratio", code="ARCHIVE_BOMB")
                names.append(name)
            if total / compressed_total > config.office_max_compression_ratio and total > 10 * 1024 * 1024:
                raise ResourceLimitError("Archive has a suspicious overall compression ratio", code="ARCHIVE_BOMB")
            return names
    except zipfile.BadZipFile as exc:
        raise FileValidationError("File is not a valid ZIP/Office container", code="BAD_ARCHIVE") from exc


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 BOM
        return True
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        # A multi-byte sequence cut at the 4 KiB boundary is fine.
        return exc.start >= len(head) - 4 or _printable_ratio(head) > 0.9


def _printable_ratio(data: bytes) -> float:
    if not data:
        return 1.0
    ok = sum(1 for b in data if b in (9, 10, 13) or 32 <= b < 127 or b >= 128)
    return ok / len(data)


def detect_file_type(path: Path, original_name: str | None, config: ReaderConfig | None = None) -> tuple[str, str]:
    """Return (logical_type, mime). Raises when the signature disagrees with the extension."""
    config = config or ReaderConfig()
    ext = extension_of(original_name)
    if ext not in SUPPORTED:
        raise UnsupportedFileError(f"Unsupported file type: {ext or 'no extension'}")
    expected, mime = SUPPORTED[ext]
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise FileValidationError("File is not readable", code="FILE_UNREADABLE") from exc
    if size == 0:
        raise FileValidationError("File is empty", code="FILE_EMPTY")
    if size > config.max_file_bytes:
        raise ResourceLimitError(f"File exceeds the {config.max_file_bytes}-byte limit", code="FILE_TOO_LARGE")
    head = _head(path)

    if expected == "pdf":
        if b"%PDF-" not in head[:1024]:
            raise FileValidationError("File content is not a PDF", code="SIGNATURE_MISMATCH")
    elif expected in _OOXML_ROOT:
        if not head.startswith(_ZIP_MAGICS):
            raise FileValidationError(f"File content is not a valid .{expected} container", code="SIGNATURE_MISMATCH")
        names = check_zip_safety(path, config)
        if _OOXML_ROOT[expected] not in names:
            raise FileValidationError(f"ZIP container is not a .{expected} document", code="SIGNATURE_MISMATCH")
    elif expected == "doc":
        if not head.startswith(_OLE_MAGIC):
            raise FileValidationError("File content is not a legacy Word document", code="SIGNATURE_MISMATCH")
    elif expected == "image":
        png = head.startswith(b"\x89PNG\r\n\x1a\n")
        jpg = head.startswith(b"\xff\xd8\xff")
        tif = head.startswith((b"II*\x00", b"MM\x00*"))
        wanted = {"image/png": png, "image/jpeg": jpg, "image/tiff": tif}[mime]
        if not wanted:
            raise FileValidationError("Image content does not match its extension", code="SIGNATURE_MISMATCH")
    elif expected in _TEXT_TYPES:
        if head.startswith(_ZIP_MAGICS + (b"%PDF", _OLE_MAGIC, b"\x89PNG", b"MZ", b"\x7fELF")) or not _looks_like_text(head):
            raise FileValidationError("File content is not text", code="SIGNATURE_MISMATCH")
    return expected, mime


def validate_declared_mime(declared: str | None, mime: str) -> None:
    """Optional extra check: an explicit, specific client MIME that contradicts the detected type is rejected.
    Generic values are ignored because browsers frequently send them."""
    if not declared:
        return
    declared = declared.split(";")[0].strip().lower()
    generic = {"application/octet-stream", "binary/octet-stream", "application/x-download", ""}
    if declared in generic:
        return
    families = {
        "text/plain": {"text/plain", "text/markdown", "text/csv", "application/json", "text/html"},
        "application/zip": {m for m in (v[1] for v in SUPPORTED.values()) if "openxml" in m},
    }
    if declared == mime or mime in families.get(declared, set()):
        return
    if declared in {"application/vnd.ms-excel", "text/x-markdown", "application/csv", "application/x-pdf", "image/jpg"}:
        return
    raise FileValidationError("Declared content type does not match the file content", code="MIME_MISMATCH")
