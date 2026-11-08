"""Arabic-aware text handling. Every function is non-destructive: callers keep the original string."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_TASHKEEL = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_TATWEEL = "\u0640"
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_ALEF = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"})
_PUNCT = str.maketrans({"،": ",", "؛": ";", "؟": "?", "٪": "%", "٫": ".", "٬": ",", "«": '"', "»": '"',
                        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-"})
# Direction/format controls that PDFs inject; they never carry meaning for search.
_BIDI_AND_ZW = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff\u061c]")
_ARABIC = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")
_LATIN = re.compile(r"[A-Za-z]")
_PRESENTATION = re.compile(r"[\ufb50-\ufdff\ufe70-\ufeff]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CID = re.compile(r"\(cid:\d+\)")
_PUA = re.compile(r"[\ue000-\uf8ff]")

# Very frequent Arabic words, in logical order. Their reversed forms signal visual-order extraction.
_COMMON_AR = ("في", "من", "على", "التي", "الذي", "هذا", "هذه", "عن", "إلى", "الى", "أن", "ان", "كان", "ما", "لا",
              "الله", "العربية", "التعليم", "الدرس", "الوحدة", "الفصل", "ذلك", "بين", "كل", "مع", "أو", "او", "قد")
_COMMON_AR_REV = tuple(w[::-1] for w in _COMMON_AR if len(w) >= 2)
_TOKEN_SPLIT = re.compile(r"[^\w\u0600-\u06ff]+", re.UNICODE)


def strip_controls(text: str) -> str:
    """Remove NUL / C0 controls only (safe for storage). Keeps newlines, tabs and all Arabic marks."""
    return _CONTROL.sub(" ", text)


def normalize_unicode(text: str) -> str:
    """NFKC: folds Arabic presentation forms and ligatures (e.g. U+FEFB -> 'لا', U+FDF2 -> 'الله') to base letters."""
    return unicodedata.normalize("NFKC", text)


def normalize_for_search(text: str, *, fold_ya_ta: bool = True) -> str:
    """Searchable copy: Unicode/ligature folding, no diacritics/tatweel/bidi controls, unified alef and digits,
    Arabic punctuation mapped to ASCII, whitespace collapsed. Never used to overwrite the original."""
    text = normalize_unicode(strip_controls(text))
    text = _BIDI_AND_ZW.sub("", text)
    text = _TASHKEEL.sub("", text).replace(_TATWEEL, "")
    text = text.translate(_AR_DIGITS).translate(_ALEF).translate(_PUNCT)
    if fold_ya_ta:
        text = text.replace("ى", "ي").replace("ة", "ه")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip().casefold()


def clean_for_storage(text: str) -> str:
    """Conservative cleanup of the ORIGINAL text: remove NULs/controls and normalise line endings only.
    Arabic letters, diacritics, bidi marks (RTL preservation) and ligature code points are left untouched."""
    text = strip_controls(text.replace("\r\n", "\n").replace("\r", "\n"))
    text = re.sub(r"[ \t\f\v]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def script_counts(text: str) -> tuple[int, int]:
    return len(_ARABIC.findall(text)), len(_LATIN.findall(text))


def detect_language(text: str) -> str:
    ar, la = script_counts(text)
    if ar == 0 and la == 0:
        return "und"
    if ar and la and min(ar, la) / max(ar, la) > 0.2:
        return "mixed"
    return "ar" if ar >= la else "en"


def reverse_visual_order(text: str) -> str:
    """Reverse each line's Arabic runs (repair for visual-order extraction). Latin/digit runs keep their order."""
    out_lines = []
    for line in text.split("\n"):
        runs = re.split(r"([A-Za-z0-9][A-Za-z0-9 .,:/\-+=*%()]*[A-Za-z0-9)]|[A-Za-z0-9])", line)
        pieces = []
        for run in runs:
            if run and _ARABIC.search(run):
                pieces.append(run[::-1])
            else:
                pieces.append(run)
        out_lines.append("".join(reversed(pieces)) if _ARABIC.search(line) else line)
    return "\n".join(out_lines)


@dataclass
class ArabicQuality:
    arabic_chars: int = 0
    score: float = 1.0                 # 1.0 = looks healthy; applies only when Arabic is present
    issues: list[str] = field(default_factory=list)
    likely_reversed: bool = False

    @property
    def is_broken(self) -> bool:
        return self.arabic_chars >= 20 and self.score < 0.5


def assess_arabic(text: str) -> ArabicQuality:
    """Detect broken Arabic extraction: reversed order, unmapped glyphs, isolated letters, presentation-form soup."""
    ar = len(_ARABIC.findall(text))
    quality = ArabicQuality(arabic_chars=ar)
    if ar < 20:
        return quality
    penalty = 0.0
    total_chars = max(1, len(text))

    pua = len(_PUA.findall(text)) + 3 * len(_CID.findall(text))
    if pua / total_chars > 0.02:
        quality.issues.append("unmapped_glyphs")
        penalty += min(0.6, pua / total_chars * 8)

    repl = text.count("\ufffd")
    if repl / total_chars > 0.01:
        quality.issues.append("replacement_characters")
        penalty += min(0.5, repl / total_chars * 10)

    pres = len(_PRESENTATION.findall(text))
    if pres / ar > 0.5:
        quality.issues.append("presentation_forms_dominant")  # visual-order glyph extraction; NFKC folds them
        penalty += 0.15

    folded = normalize_unicode(text)
    tokens = [t for t in _TOKEN_SPLIT.split(folded) if t]
    ar_tokens = [t for t in tokens if _ARABIC.search(t)]
    if ar_tokens:
        singles = sum(1 for t in ar_tokens if len(t) == 1)
        if len(ar_tokens) >= 15 and singles / len(ar_tokens) > 0.45:
            quality.issues.append("isolated_letters")  # letters split apart, typical of broken cmap / shaping
            penalty += 0.4
        fwd = sum(1 for t in ar_tokens if t in _COMMON_AR)
        rev = sum(1 for t in ar_tokens if t in _COMMON_AR_REV and t not in _COMMON_AR)
        if rev >= 3 and rev > fwd * 1.5:
            quality.likely_reversed = True
            quality.issues.append("reversed_order")
            penalty += 0.5
    quality.score = round(max(0.0, 1.0 - penalty), 4)
    return quality


def best_arabic_variant(text: str) -> tuple[str, ArabicQuality, str | None]:
    """Return (text, quality, repair_note). Only repairs when reversal is detected AND the repair scores better.
    The caller must keep the pre-repair text as the source of truth in its own metadata when repair happened."""
    quality = assess_arabic(text)
    if quality.likely_reversed:
        candidate = reverse_visual_order(text)
        cand_quality = assess_arabic(candidate)
        if cand_quality.score > quality.score and not cand_quality.likely_reversed:
            return candidate, cand_quality, "reversed_visual_order_repaired"
    return text, quality, None
