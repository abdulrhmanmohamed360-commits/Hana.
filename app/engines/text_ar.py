"""Arabic/English text normalization for retrieval and comparison (pure stdlib)."""
from __future__ import annotations

import re
import unicodedata

_TASHKEEL = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_TATWEEL = "\u0640"
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_ALEF = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"})
_TOKEN = re.compile(r"[\w\u0600-\u06ff]+", re.UNICODE)
_ARABIC_CHAR = re.compile(r"[\u0600-\u06ff]")
_LATIN_CHAR = re.compile(r"[A-Za-z]")


def normalize_arabic(text: str, *, fold_ya_ta: bool = True) -> str:
    """Remove diacritics/tatweel, unify alef forms and digits. ya/ta-marbuta folding is for matching only."""
    text = unicodedata.normalize("NFKC", text)
    text = _TASHKEEL.sub("", text).replace(_TATWEEL, "")
    text = text.translate(_AR_DIGITS).translate(_ALEF)
    if fold_ya_ta:
        text = text.replace("ى", "ي").replace("ة", "ه")
    return text


def tokenize(text: str) -> list[str]:
    return [t.casefold() for t in _TOKEN.findall(normalize_arabic(text)) if len(t) > 1 or t.isdigit()]


def detect_language(text: str) -> str:
    ar, la = len(_ARABIC_CHAR.findall(text)), len(_LATIN_CHAR.findall(text))
    if ar == 0 and la == 0:
        return "und"
    if ar and la and min(ar, la) / max(ar, la) > 0.25:
        return "mixed"
    return "ar" if ar >= la else "en"
