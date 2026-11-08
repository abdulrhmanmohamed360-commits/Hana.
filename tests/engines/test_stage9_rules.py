from app.engines import coverage_rules as cr
from app.engines import graph_rules as gr
from app.engines import quality_rules as qr
from app.engines.curriculum_rules import HierarchyError, check_placement, match_name, name_key, term_number


# ---------------- curriculum rules
def test_hierarchy_accepts_valid_and_rejects_invalid_placement():
    check_placement(None, "STAGE"); check_placement("STAGE", "GRADE"); check_placement("GRADE", "TERM")
    check_placement("TERM", "SUBJECT"); check_placement("SUBJECT", "LESSON"); check_placement("LESSON", "CONCEPT")
    for parent, level in [(None, "LESSON"), ("GRADE", "LESSON"), ("SUBJECT", "CONCEPT"), ("CONCEPT", "LESSON"), ("UNIT", "SUBJECT")]:
        try:
            check_placement(parent, level)
        except HierarchyError:
            continue
        raise AssertionError((parent, level))


def test_name_key_detects_duplicates_across_arabic_spelling_variants():
    assert name_key("المعادلات  الخطية!") == name_key("المُعادلات الخطيّة")
    assert name_key("أحمد") == name_key("احمد")
    assert name_key("Linear Equations") == name_key("linear   equations.")
    assert name_key("الجبر") != name_key("الهندسة")


def test_term_number_and_match_name():
    assert term_number("الترم الأول") == 1 and term_number("الفصل الدراسي الثاني") == 2 and term_number("Term 2") == 2
    assert term_number("الصيف") is None
    assert match_name("المعادلات الخطية", "المعادلات الخطية في متغير واحد")


# ---------------- graph rules
def test_edge_validation_rules():
    gr.validate_edge("HAS_EXAMPLE", "LESSON", "EXAMPLE", False)
    gr.validate_edge("EXPLAINS", "EXPLANATION", "CONCEPT", False)
    gr.validate_edge("SUPPORTED_BY", "CLAIM", "SOURCE", False)
    for rel, a, b, same in [("HAS_EXAMPLE", "EXAMPLE", "LESSON", False), ("EXPLAINS", "CLAIM", "LESSON", False),
                            ("PREREQUISITE_OF", "LESSON", "LESSON", True), ("NOPE", "LESSON", "LESSON", False),
                            ("SUPPORTED_BY", "SOURCE", "SOURCE", False)]:
        try:
            gr.validate_edge(rel, a, b, same)
        except gr.GraphRuleError:
            continue
        raise AssertionError((rel, a, b))


def test_symmetric_relations_are_canonicalised_so_duplicates_collide():
    assert gr.canonical("RELATED_TO", "b", "a") == gr.canonical("RELATED_TO", "a", "b") == ("a", "b")
    assert gr.canonical("PREREQUISITE_OF", "b", "a") == ("b", "a")


def test_cycle_detection_and_depth_limited_reachability():
    adj = {"a": {"b"}, "b": {"c"}}
    assert gr.would_create_cycle(adj, "c", "a") and not gr.would_create_cycle(adj, "a", "c")
    assert gr.reachable(adj, "a") == {"b": 1, "c": 2}
    assert gr.reachable(adj, "a", max_depth=1) == {"b": 1}


def test_learning_path_orders_prerequisites_and_finds_gaps():
    needs = {"quadratics": {"linear", "factoring"}, "linear": {"arithmetic"}, "factoring": {"arithmetic"}}
    path = gr.learning_path(needs, "quadratics")
    assert path.index("arithmetic") < path.index("linear") and path.index("arithmetic") < path.index("factoring")
    assert "quadratics" not in path and len(path) == 3
    assert gr.missing_in_chain(path, {"arithmetic", "linear"}) == ["factoring"]
    assert gr.learning_path({"a": {"b"}, "b": {"a"}}, "a")   # cycle: terminates


# ---------------- coverage rules
def A(id, t, status="APPROVED", chars=500, src=True, items=0, ans=0, age=10.0, unv=0, rej=0):
    return cr.AssetFacts(id, t, status, chars, src, items, ans, age, unv, rej)


def full_math_lesson():
    return [A("e", "explanation"), A("d", "definition"), A("x1", "example"), A("x2", "example"),
            A("ex", "exercise", items=5, ans=5), A("ex2", "exercise", items=5, ans=5), A("ex3", "exercise", items=2, ans=2),
            A("q", "quiz", items=3, ans=3), A("s", "summary")]


def test_complete_lesson_needs_every_requirement_met_by_approved_content():
    cov = cr.evaluate_lesson(cr.PROFILES["mathematics"], full_math_lesson())
    assert cov.status == "COMPLETE" and cov.score == 1.0 and not cov.unmet


def test_title_only_or_short_text_is_not_coverage():
    cov = cr.evaluate_lesson(cr.PROFILES["mathematics"], [A("e", "explanation", chars=12)])
    assert cov.status == "INCOMPLETE" and cov.score == 0.0
    assert any("too short" in u["reason"] for u in cov.unmet if u["asset_type"] == "explanation")


def test_no_assets_is_missing_and_unapproved_never_counts_as_complete():
    assert cr.evaluate_lesson(cr.PROFILES["default"], []).status == "MISSING"
    drafts = [A(a.id, a.asset_type, status="DRAFT", chars=a.chars, src=True, items=a.items, ans=a.items_with_answers)
              for a in full_math_lesson()]
    cov = cr.evaluate_lesson(cr.PROFILES["mathematics"], drafts)
    assert cov.status == "PENDING_REVIEW" and cov.score == 0.0 and cov.pending_reviews > 0


def test_exercises_without_answers_or_sources_are_flagged():
    lesson = [a for a in full_math_lesson() if a.id not in ("ex2", "ex3")]
    lesson = [A("ex", "exercise", items=5, ans=3) if a.id == "ex" else a for a in lesson]
    lesson[0] = A("e", "explanation", src=False)
    cov = cr.evaluate_lesson(cr.PROFILES["mathematics"], lesson)
    reasons = " | ".join(u["reason"] for u in cov.unmet)
    assert cov.status == "INCOMPLETE" and "no answer" in reasons and "no sources" in reasons


def test_outdated_and_failed_claims():
    old = [a if a.id != "e" else A("e", "explanation", age=2000) for a in full_math_lesson()]
    assert cr.evaluate_lesson(cr.PROFILES["mathematics"], old).status == "OUTDATED"
    bad = [a if a.id != "x1" else A("x1", "example", rej=1) for a in full_math_lesson()]
    cov = cr.evaluate_lesson(cr.PROFILES["mathematics"], bad)
    assert cov.status == "INCOMPLETE" and "example" in [u["asset_type"] for u in cov.unmet]


def test_profiles_are_selected_by_subject_and_summary_math():
    assert cr.pick_profile_key("الرياضيات") == "mathematics" and cr.pick_profile_key("Physics") == "default"
    assert cr.pick_profile_key("التاريخ") == "humanities"
    s = cr.summarize(["COMPLETE", "MISSING", "INCOMPLETE", "PENDING_REVIEW"], [1.0, 0, 0.5, 0])
    assert s["total_lessons"] == 4 and s["coverage_percentage"] == 25.0 and s["missing_lessons"] == 1


# ---------------- quality rules
def test_quality_separates_support_from_correctness():
    ev = [qr.EvidenceFact(0.9, True, "g1"), qr.EvidenceFact(0.8, True, "g2")]
    claims = [qr.ClaimFact("SOURCE_SUPPORTED", ev), qr.ClaimFact("SOURCE_SUPPORTED", ev)]
    a = qr.assess(qr.QualityInput("APPROVED", claims, [0.9, 0.8], 2, 1.0, 0, 30))
    assert a.support_score > 0.7
    assert a.correctness == "UNKNOWN", "source support must never be reported as verified correct"
    assert "not the probability" in a.note


def test_math_verified_content_is_deterministically_verified_and_every_dimension_has_a_reason():
    a = qr.assess(qr.QualityInput("DRAFT", [qr.ClaimFact("MATH_VERIFIED")] * 3, [], 0, None, 0, 5))
    assert a.correctness == "DETERMINISTICALLY_VERIFIED" and a.needs_human_review is True   # still a draft
    assert all(dim.reason for dim in a.dimensions.values())
    assert "NO_SOURCES" in a.flags and "NOT_APPROVED" in a.flags


def test_rejected_or_conflicted_claims_dominate():
    a = qr.assess(qr.QualityInput("APPROVED", [qr.ClaimFact("MATH_VERIFIED"), qr.ClaimFact("REJECTED")], [0.9], 1))
    assert a.correctness == "REJECTED" and a.dimensions["verification"].score == 0.0
    b = qr.assess(qr.QualityInput("APPROVED", [qr.ClaimFact("CONFLICTED")], [0.9], 1))
    assert b.correctness == "CONFLICTED" and "CONFLICTS" in b.flags
    c = qr.assess(qr.QualityInput("APPROVED", [qr.ClaimFact("MATH_VERIFIED")], [0.9], 1, contradiction_edges=1))
    assert c.correctness == "CONFLICTED"


def test_freshness_window_and_unknown_age():
    fresh = qr.assess(qr.QualityInput("APPROVED", [], [], 0, age_days=100, freshness_days=365))
    old = qr.assess(qr.QualityInput("APPROVED", [], [], 0, age_days=800, freshness_days=365))
    assert fresh.dimensions["freshness"].score == 1.0 and "OUTDATED" not in fresh.flags
    assert old.dimensions["freshness"].score < 0.5 and "OUTDATED" in old.flags
    assert qr.assess(qr.QualityInput("APPROVED")).dimensions["freshness"].score is None


def test_unregistered_sources_are_weak_and_flagged():
    a = qr.assess(qr.QualityInput("APPROVED", [], [], cited_sources=2))
    assert a.dimensions["source_reliability"].score == 0.2 and "SOURCES_NOT_REGISTERED" in a.flags


def test_structured_assets_count_by_item_not_by_asset():
    lesson = [A("e", "explanation"), A("d", "definition"), A("x", "example", items=2, ans=0), A("ex", "exercise", items=5, ans=5),
              A("q", "quiz", items=3, ans=3), A("s", "summary")]
    assert cr.evaluate_lesson(cr.PROFILES["mathematics"], lesson).status == "COMPLETE"
    short = [a if a.id != "ex" else A("ex", "exercise", items=2, ans=2) for a in lesson]
    cov = cr.evaluate_lesson(cr.PROFILES["mathematics"], short)
    assert cov.status == "INCOMPLETE" and "needs 3, has 2" in cov.unmet[0]["reason"]
