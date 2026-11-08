"""EduForge document-reading engine (multi-format, multi-engine, Arabic-aware)."""
from .arabic import (assess_arabic, clean_for_storage, detect_language, normalize_for_search, normalize_unicode)
from .router import arabic_quality_summary, extract_document
from .security import SUPPORTED_EXTENSIONS, detect_file_type, safe_filename, sha256_file
from .types import (Block, CorruptedDocumentError, EmptyDocumentError, ExtractionError, ExtractionResult,
                    FileValidationError, OcrUnavailableError, PageResult, ReaderConfig, ResourceLimitError,
                    UnsupportedFileError)

__all__ = [
    "Block", "CorruptedDocumentError", "EmptyDocumentError", "ExtractionError", "ExtractionResult",
    "FileValidationError", "OcrUnavailableError", "PageResult", "ReaderConfig", "ResourceLimitError",
    "SUPPORTED_EXTENSIONS", "UnsupportedFileError", "arabic_quality_summary", "assess_arabic", "clean_for_storage",
    "detect_file_type", "detect_language", "extract_document", "normalize_for_search", "normalize_unicode",
    "safe_filename", "sha256_file",
]
