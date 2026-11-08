from app.engines.conflicts import Claim, find_conflicts
from app.engines.fusion import cosine, reciprocal_rank_fusion
from app.engines.math_verify import (MathInputError, parse_expression, verify_equality, verify_solution,
                                     verify_solution_steps)
from app.engines.text_ar import detect_language, normalize_arabic, tokenize


# ---- Arabic / English text
def test_normalize_strips_diacritics_and_unifies_forms():
    assert normalize_arabic("المُعَادَلَةُ") == "المعادله"
    assert normalize_arabic("أحمد إبراهيم") == "احمد ابراهيم"


def test_arabic_indic_digits_become_ascii():
    assert normalize_arabic("٢س + ٣ = ٧") == "2س + 3 = 7"


def test_tokenize_matches_diacritized_and_plain_forms():
    assert tokenize("المعادلة") == tokenize("اَلْمُعَادَلَة")


def test_detect_language():
    assert detect_language("المعادلة الخطية") == "ar"
    assert detect_language("linear equation") == "en"
    assert detect_language("حل المعادلة linear equation") == "mixed"


# ---- deterministic math
def test_equality_correct_and_incorrect():
    assert verify_equality("2*(x+3)", "2x+6").status == "CORRECT"
    assert verify_equality("2*(x+3)", "2x+3").status == "INCORRECT"


def test_solution_checks():
    assert verify_solution("2x + 3 = 7", "x", "2").status == "CORRECT"
    assert verify_solution("2x + 3 = 7", "x", "3").status == "INCORRECT"


def test_quadratic_partial_solution_is_rejected():
    v = verify_solution("x^2 = 4", "x", "2")
    assert v.status == "INCORRECT" and "not the only" in v.detail


def test_arabic_digits_in_math():
    assert verify_solution("٢x + ٣ = ٧", "x", "٢").status == "CORRECT"


def test_steps_detect_bad_step():
    good = verify_solution_steps(["2x + 3 = 7", "2x = 4", "x = 2"])
    assert [v.status for v in good] == ["CORRECT"] * 3
    bad = verify_solution_steps(["2x + 3 = 7", "2x = 10", "x = 5"])
    assert bad[1].status == "INCORRECT"


def test_unsafe_input_is_rejected_not_evaluated():
    for payload in ["__import__('os').system('id')", "x; import os", "open('/etc/passwd')"]:
        try:
            parse_expression(payload)
        except MathInputError:
            continue
        raise AssertionError(f"unsafe input accepted: {payload}")
    assert verify_equality("1+1", "__import__('os')").status == "UNPARSEABLE"


# ---- fusion
def test_cosine():
    assert abs(cosine([1, 0], [1, 0]) - 1) < 1e-9
    assert abs(cosine([1, 0], [0, 1])) < 1e-9


def test_rrf_rewards_agreement_across_signals():
    fused = reciprocal_rank_fusion({"lexical": ["a", "b", "c"], "vector": ["b", "a", "d"], "graph": ["b"]})
    assert fused[0][0] == "b"
    assert fused[0][2] == {"lexical": 2, "vector": 1, "graph": 1}


# ---- conflicts
def test_numeric_conflict_between_sources():
    claims = [Claim("1", "The speed of light in vacuum is 299792 km per second", "A"),
              Claim("2", "The speed of light in vacuum is 150000 km per second", "B")]
    found = find_conflicts(claims)
    assert len(found) == 1 and found[0].kind == "NUMERIC"


def test_same_values_and_same_source_do_not_conflict():
    same = [Claim("1", "Water boils at 100 degrees Celsius at sea level", "A"),
            Claim("2", "At sea level water boils at 100 degrees Celsius", "B")]
    assert find_conflicts(same) == []
    one_source = [Claim("1", "Water boils at 100 degrees at sea level", "A"),
                  Claim("2", "Water boils at 90 degrees at sea level", "A")]
    assert find_conflicts(one_source) == []


def test_arabic_numeric_conflict():
    claims = [Claim("1", "مجموع زوايا المثلث يساوي 180 درجة", "A"),
              Claim("2", "مجموع زوايا المثلث يساوي 360 درجة", "B")]
    assert [c.kind for c in find_conflicts(claims)] == ["NUMERIC"]


def test_arabic_variable_letter_is_supported():
    assert verify_solution("٢س + ٣ = ٧", "x", "2").status == "CORRECT"
    assert verify_equality("2*(س+3)", "2س+6").status == "CORRECT"


def test_multi_letter_identifiers_are_rejected():
    assert verify_equality("sqrt(4)", "2").status == "UNPARSEABLE"


# ---- claims + generator integration (pure)
from app.engines import math_gen
from app.engines.claims import (candidates_for_item, extract_text_claims, verify_math_candidate)


def test_generator_output_passes_independent_verification_all_difficulties():
    for diff in math_gen.DIFFICULTIES:
        for lang in ("ar", "en"):
            g = math_gen.generate("seed-1", diff, lang)
            assert len(g["solved_examples"]) == 2 and len(g["exercises"]) == 5 and len(g["questions"]) == 3
            for ct, items in g.items():
                for n, data in enumerate(items):
                    cands = candidates_for_item(ct, data, "", f"{ct}-{n}")
                    assert cands
                    for c in cands:
                        r = verify_math_candidate(c)
                        assert r is not None and r.status == "MATH_VERIFIED", (diff, lang, ct, c.text, r)


def test_generator_is_deterministic_and_seed_sensitive():
    assert math_gen.generate("a", "beginner", "ar") == math_gen.generate("a", "beginner", "ar")
    assert math_gen.generate("a", "beginner", "ar") != math_gen.generate("b", "beginner", "ar")


def test_wrong_answer_and_wrong_step_are_rejected():
    ex = {"problem": "2س + 3 = 7", "variable": "س", "answer": "3",
          "steps": [{"equation": "2س = 4", "explanation": "x"}, {"equation": "س = 3", "explanation": "y"}]}
    results = [verify_math_candidate(c).status for c in candidates_for_item("solved_examples", ex, "", "e")]
    assert results == ["REJECTED", "REJECTED"]


def test_ambiguous_mcq_distractor_is_rejected():
    q = {"prompt": "p", "equation": "x^2 = 4", "variable": "x", "correct_label": "A",
         "options": [{"label": "A", "value": "2"}, {"label": "B", "value": "-2"}]}
    statuses = [verify_math_candidate(c).status for c in candidates_for_item("questions", q, "", "q")]
    assert "REJECTED" in statuses  # 2 is not the only solution and -2 is also correct


def test_text_claims_and_numeric_identities():
    claims = extract_text_claims("المعادلة تعبير رياضي يحتوي على علامة المساواة. ولدينا 2 + 3 = 6 دائما.", "e1")
    assert [c.kind for c in claims] == ["text", "math_equality"]
    assert verify_math_candidate(claims[1]).status == "REJECTED"
    assert verify_math_candidate(claims[0]) is None


def test_supports_lesson():
    assert math_gen.supports_lesson("Mathematics", "Linear Equations")
    assert math_gen.supports_lesson("الرياضيات", "المعادلات الخطية")
    assert not math_gen.supports_lesson("Physics", "Newton's laws")
