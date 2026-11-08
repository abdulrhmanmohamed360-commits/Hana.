"""Deterministic generator of linear-equation teaching items (solved examples, exercises, multiple choice).

The generator is NOT trusted: every item it emits is passed through the same verification service as any other
content. It exists so that exercises and model answers never depend on a language model's arithmetic.
Supported lessons: linear equations in one unknown (three difficulty bands). Anything else returns None.
"""
from __future__ import annotations

import hashlib
import random
from fractions import Fraction

from app.engines.text_ar import normalize_arabic

DIFFICULTIES = ("beginner", "intermediate", "advanced")


def supports_lesson(subject: str, lesson: str) -> bool:
    s = normalize_arabic(f"{subject} {lesson}").casefold()
    if "linear" in s and "equation" in s:
        return True
    return "معادل" in s and any(k in s for k in ("خطي", "الاولي", "اولي", "بمجهول واحد"))


def _lin(a: int, b: int, v: str) -> str:
    parts: list[str] = []
    if a != 0:
        parts.append(("" if a == 1 else "-" if a == -1 else str(a)) + v)
    if b != 0:
        parts.append(f"{'+' if b > 0 else '-'} {abs(b)}" if parts else str(b))
    return " ".join(parts) or "0"


def _num(x: Fraction | int) -> str:
    x = Fraction(x)
    return str(x.numerator) if x.denominator == 1 else f"{x.numerator}/{x.denominator}"


def _build(rng: random.Random, difficulty: str, v: str, ar: bool) -> dict:
    x0 = rng.choice([n for n in range(-9, 10) if n != 0])
    steps: list[dict] = []
    if difficulty == "beginner":
        a, b = rng.randint(2, 9), rng.choice([n for n in range(-15, 16) if n != 0])
        c = a * x0 + b
        problem = f"{_lin(a, b, v)} = {c}"
        move = (f"نطرح {b} من الطرفين" if b > 0 else f"نضيف {abs(b)} إلى الطرفين") if ar else \
               (f"Subtract {b} from both sides" if b > 0 else f"Add {abs(b)} to both sides")
        steps = [{"equation": f"{_lin(a, 0, v)} = {c - b}", "explanation": move},
                 {"equation": f"{v} = {x0}", "explanation": f"نقسم الطرفين على {a}" if ar else f"Divide both sides by {a}"}]
    elif difficulty == "intermediate":
        a = rng.randint(2, 9)
        c = rng.choice([n for n in range(-5, 9) if n != a])
        b = rng.choice([n for n in range(-12, 13) if n != 0])
        d = (a - c) * x0 + b
        problem = f"{_lin(a, b, v)} = {_lin(c, d, v)}"
        steps = [{"equation": f"{_lin(a - c, 0, v)} = {d - b}",
                  "explanation": "ننقل حدود المجهول إلى طرف والأعداد إلى الطرف الآخر" if ar else
                                 "Collect the unknown terms on one side and the numbers on the other"},
                 {"equation": f"{v} = {x0}",
                  "explanation": f"نقسم الطرفين على {a - c}" if ar else f"Divide both sides by {a - c}"}]
    else:
        p = rng.randint(2, 6)
        q = rng.choice([n for n in range(-6, 7) if n != 0])
        r = rng.choice([n for n in range(-4, 9) if n != p])
        s = p * (x0 + q) - r * x0
        problem = f"{p}({_lin(1, q, v)}) = {_lin(r, s, v)}"
        steps = [{"equation": f"{_lin(p, p * q, v)} = {_lin(r, s, v)}",
                  "explanation": "نفك الأقواس بالتوزيع" if ar else "Expand the brackets"},
                 {"equation": f"{_lin(p - r, 0, v)} = {s - p * q}",
                  "explanation": "نجمع الحدود المتشابهة" if ar else "Collect like terms"},
                 {"equation": f"{v} = {x0}",
                  "explanation": f"نقسم الطرفين على {p - r}" if ar else f"Divide both sides by {p - r}"}]
    return {"problem": problem, "variable": v, "steps": steps, "answer": str(x0), "_x0": x0, "_form": difficulty}


def _distractors(rng: random.Random, x0: int) -> list[str]:
    pool = {-x0, x0 + 1, x0 - 1, 2 * x0, x0 + 2, x0 - 2, 0}
    pool.discard(x0)
    return [str(n) for n in rng.sample(sorted(pool), 3)]


def generate(seed: str, difficulty: str, language: str, *, examples: int = 2, exercises: int = 5, mcq: int = 3) -> dict:
    """Return {'solved_examples': [...], 'exercises': [...], 'questions': [...]} of plain dicts."""
    difficulty = difficulty if difficulty in DIFFICULTIES else "beginner"
    ar = language.startswith("ar")
    v = "س" if ar else "x"
    rng = random.Random(int.from_bytes(hashlib.sha256(f"{seed}|{difficulty}|{language}".encode()).digest()[:8], "big"))
    seen: set[str] = set()

    def fresh() -> dict:
        for _ in range(50):
            item = _build(rng, difficulty, v, ar)
            if item["problem"] not in seen:
                seen.add(item["problem"])
                return item
        return item

    out: dict = {"solved_examples": [], "exercises": [], "questions": []}
    for i in range(examples):
        it = fresh()
        out["solved_examples"].append({"title": (f"مثال {i + 1}" if ar else f"Example {i + 1}"),
                                       "problem": it["problem"], "variable": v, "steps": it["steps"],
                                       "answer": it["answer"]})
    for i in range(exercises):
        it = fresh()
        out["exercises"].append({"prompt": ("حل المعادلة التالية:" if ar else "Solve the following equation:"),
                                 "equation": it["problem"], "variable": v, "answer": it["answer"],
                                 "steps": it["steps"]})
    labels = ["أ", "ب", "ج", "د"] if ar else ["A", "B", "C", "D"]
    for i in range(mcq):
        it = fresh()
        values = [it["answer"]] + _distractors(rng, it["_x0"])
        rng.shuffle(values)
        out["questions"].append({
            "prompt": (f"ما قيمة {v} التي تحقق المعادلة: {it['problem']} ؟" if ar else
                       f"Which value of {v} satisfies: {it['problem']} ?"),
            "equation": it["problem"], "variable": v,
            "options": [{"label": l, "value": val} for l, val in zip(labels, values)],
            "correct_label": labels[values.index(it["answer"])]})
    return out


def render_solved_example(d: dict, ar: bool) -> str:
    lines = [f"**{d['title']}**", ("حل المعادلة: " if ar else "Solve: ") + d["problem"], ""]
    for n, s in enumerate(d["steps"], 1):
        lines.append(f"{n}. {s['explanation']}: {s['equation']}")
    lines.append(("الإجابة: " if ar else "Answer: ") + f"{d['variable']} = {d['answer']}")
    return "\n".join(lines)


def render_exercise(d: dict, ar: bool, with_answer: bool = False) -> str:
    text = f"{d['prompt']} {d['equation']}"
    return text + ((f"\n" + ("الإجابة النموذجية: " if ar else "Model answer: ") + f"{d['variable']} = {d['answer']}")
                   if with_answer else "")


def render_mcq(d: dict, ar: bool) -> str:
    opts = "\n".join(f"{o['label']}) {o['value']}" for o in d["options"])
    return f"{d['prompt']}\n{opts}"
