"""Curriculum hierarchy rules (pure). Country and education system are columns of `Curriculum`; everything below is a node.

Country -> Education System -> [Curriculum]
  Stage -> Grade -> (Academic Year) -> (Term) -> Subject -> (Unit) -> Lesson -> (Topic) -> Concept
Parenthesised levels are optional so that systems without terms/units can still be represented.
"""
from __future__ import annotations

import re

from app.engines.text_ar import normalize_arabic

LEVELS = ["STAGE", "GRADE", "ACADEMIC_YEAR", "TERM", "SUBJECT", "UNIT", "LESSON", "TOPIC", "CONCEPT"]
ROOT_LEVELS = {"STAGE", "GRADE"}
ALLOWED_CHILDREN: dict[str, set[str]] = {
    "STAGE": {"GRADE"},
    "GRADE": {"ACADEMIC_YEAR", "TERM", "SUBJECT"},
    "ACADEMIC_YEAR": {"TERM", "SUBJECT"},
    "TERM": {"SUBJECT"},
    "SUBJECT": {"UNIT", "LESSON"},
    "UNIT": {"LESSON"},
    "LESSON": {"TOPIC", "CONCEPT"},
    "TOPIC": {"CONCEPT"},
    "CONCEPT": set(),
}
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def name_key(name: str) -> str:
    """Duplicate-detection key: Arabic-normalized, case-folded, punctuation and spacing removed."""
    return _SPACES.sub(" ", _PUNCT.sub(" ", normalize_arabic(name).casefold())).strip()


class HierarchyError(ValueError):
    pass


def check_placement(parent_level: str | None, level: str) -> None:
    if level not in LEVELS:
        raise HierarchyError(f"unknown level {level!r}; expected one of {LEVELS}")
    if parent_level is None:
        if level not in ROOT_LEVELS:
            raise HierarchyError(f"a {level} cannot be a root node; roots must be one of {sorted(ROOT_LEVELS)}")
    elif level not in ALLOWED_CHILDREN[parent_level]:
        raise HierarchyError(f"a {level} cannot be placed under a {parent_level}; allowed: "
                             f"{sorted(ALLOWED_CHILDREN[parent_level])}")


def term_number(name: str) -> int | None:
    """Recognise 'Term 1', 'الترم الأول', 'الفصل الدراسي الثاني', '1' ... -> 1/2/3."""
    n = normalize_arabic(name).casefold()
    words = {"الاول": 1, "اول": 1, "first": 1, "1": 1, "الثاني": 2, "الثانيه": 2, "ثاني": 2, "second": 2, "2": 2,
             "الثالث": 3, "third": 3, "3": 3}
    for token in re.findall(r"[\w]+", n):
        if token in words:
            return words[token]
    return None


def match_name(a: str, b: str) -> bool:
    ka, kb = name_key(a), name_key(b)
    return bool(ka) and bool(kb) and (ka == kb or (min(len(ka), len(kb)) >= 4 and (ka in kb or kb in ka)))
