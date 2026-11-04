"""Deterministic math checking with SymPy. No language model is involved in any verdict.

Input is restricted by a character whitelist before parsing, because SymPy's parser uses eval().
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import sympy as sp
from sympy.parsing.sympy_parser import (convert_xor, implicit_multiplication_application, parse_expr,
                                        standard_transformations)

from app.engines.text_ar import normalize_arabic

_ALLOWED = re.compile(r"^[0-9A-Za-z+\-*/^().=\s]+$")
_MULTI_LETTER = re.compile(r"[A-Za-z]{2,}")
_ARABIC_VARS = str.maketrans({"س": "x", "ص": "y"})  # common Arabic textbook unknowns
_FORBIDDEN_WORDS = re.compile(r"(__|import|lambda|exec|eval|open|os|sys)", re.I)
_TRANSFORMS = standard_transformations + (implicit_multiplication_application, convert_xor)
MAX_LEN = 200


class MathInputError(ValueError):
    pass


@dataclass(frozen=True)
class Verdict:
    status: str  # CORRECT | INCORRECT | UNPARSEABLE
    detail: str
    expected: str | None = None


def _clean(text: str) -> str:
    text = normalize_arabic(text, fold_ya_ta=False)
    text = text.replace("×", "*").replace("÷", "/").replace("−", "-").replace("٫", ".").replace("،", ",")
    text = text.translate(_ARABIC_VARS)
    if (len(text) > MAX_LEN or not _ALLOWED.match(text) or _FORBIDDEN_WORDS.search(text)
            or _MULTI_LETTER.search(text)):
        raise MathInputError("expression contains unsupported characters or is too long")
    return text.strip()


def parse_expression(text: str) -> sp.Expr:
    try:
        cleaned = _clean(text)
        local = {c: sp.Symbol(c) for c in set(re.findall(r"[A-Za-z]", cleaned))}  # no sympy constants like E, I, S
        return parse_expr(cleaned, local_dict=local, transformations=_TRANSFORMS, evaluate=True)
    except MathInputError:
        raise
    except Exception as exc:  # sympy raises many types
        raise MathInputError(f"cannot parse expression: {type(exc).__name__}") from exc


def parse_equation(text: str) -> sp.Eq:
    cleaned = _clean(text)
    if cleaned.count("=") != 1:
        raise MathInputError("an equation needs exactly one '='")
    lhs, rhs = cleaned.split("=")
    return sp.Eq(parse_expression(lhs), parse_expression(rhs))


def verify_equality(lhs: str, rhs: str) -> Verdict:
    """Is lhs mathematically identical to rhs (e.g. '2*(x+3)' vs '2*x+6')?"""
    try:
        diff = sp.simplify(parse_expression(lhs) - parse_expression(rhs))
    except MathInputError as exc:
        return Verdict("UNPARSEABLE", str(exc))
    return Verdict("CORRECT", "expressions are identical") if diff == 0 else Verdict("INCORRECT", f"difference is {diff}")


def verify_solution(equation: str, variable: str, claimed: str) -> Verdict:
    """Does `variable = claimed` satisfy the equation, and is it the complete solution set (real numbers)?"""
    try:
        eq = parse_equation(equation)
        value = parse_expression(claimed)
        sym = sp.Symbol(variable.strip().translate(_ARABIC_VARS))
        if not re.fullmatch(r"[A-Za-z]", sym.name):
            raise MathInputError("variable must be a single letter")
    except MathInputError as exc:
        return Verdict("UNPARSEABLE", str(exc))
    solutions = sp.solveset(eq, sym, domain=sp.S.Reals)
    expected = str(solutions)
    satisfied = sp.simplify(eq.lhs.subs(sym, value) - eq.rhs.subs(sym, value)) == 0
    if not satisfied:
        return Verdict("INCORRECT", f"{variable} = {claimed} does not satisfy the equation", expected)
    if solutions.is_FiniteSet and len(solutions) > 1:
        return Verdict("INCORRECT", "value satisfies the equation but is not the only solution", expected)
    return Verdict("CORRECT", "value satisfies the equation", expected)


def verify_solution_steps(steps: list[str]) -> list[Verdict]:
    """Each step must be an equation equivalent to the previous one (same solution set)."""
    verdicts: list[Verdict] = []
    prev: sp.Eq | None = None
    for i, step in enumerate(steps):
        try:
            cur = parse_equation(step)
        except MathInputError as exc:
            verdicts.append(Verdict("UNPARSEABLE", f"step {i + 1}: {exc}"))
            prev = None
            continue
        if prev is not None:
            free = sorted((prev.free_symbols | cur.free_symbols), key=str)
            if len(free) == 1:
                a, b = sp.solveset(prev, free[0], sp.S.Reals), sp.solveset(cur, free[0], sp.S.Reals)
                ok = a == b
                verdicts.append(Verdict("CORRECT" if ok else "INCORRECT",
                                        f"step {i + 1} {'preserves' if ok else 'changes'} the solution set",
                                        f"{a} vs {b}" if not ok else None))
            else:
                verdicts.append(Verdict("UNPARSEABLE", f"step {i + 1}: only single-variable steps are checked"))
        else:
            verdicts.append(Verdict("CORRECT", f"step {i + 1}: first parsed step"))
        prev = cur
    return verdicts
