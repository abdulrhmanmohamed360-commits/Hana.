from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import LearningAsset

API = "/api/v1"
GRADE, SUBJECT, UNIT = "الصف الثالث الإعدادي", "الرياضيات", "الجبر"
L_PRE, L_LIN = "العمليات على الأعداد", "المعادلات الخطية"

def _n(level, name, children=None, **kw):
    return {"level": level, "name": name, "children": children or [], **kw}


_lessons = [
    _n("LESSON", L_PRE, [_n("CONCEPT", "العدد الصحيح")], objectives=["إجراء العمليات الأربع على الأعداد الصحيحة"]),
    _n("LESSON", L_LIN, [_n("CONCEPT", "المجهول"), _n("CONCEPT", "طرفا المعادلة")], prerequisites=[L_PRE],
       objectives=["حل معادلة خطية بمجهول واحد"]),
]
TREE = [_n("STAGE", "المرحلة الإعدادية", [_n("GRADE", GRADE, [_n("TERM", "الترم الأول", [
    _n("SUBJECT", SUBJECT, [_n("UNIT", UNIT, _lessons)])])])])]
TREE_NODE_COUNT = 10


def seed_curriculum(client, headers, name="Egyptian National Curriculum", tree=None):
    r = client.post(f"{API}/curriculum", headers=headers, json={"country": "Egypt", "education_system": "National", "name": name, "language": "ar"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    imp = client.post(f"{API}/curriculum/{cid}/import", headers=headers, json={"items": tree or TREE})
    assert imp.status_code == 200, imp.text
    return cid, imp.json()


def find(tree, name):
    for n in tree:
        if n["name"] == name:
            return n["id"]
        hit = find(n["children"], name)
        if hit:
            return hit
    return None


def node_ids(client, headers, cid, *names):
    tree = client.get(f"{API}/curriculum/{cid}/tree", headers=headers).json()["tree"]
    return [find(tree, n) for n in names]


def add_asset(db, node_id, asset_type, *, status="APPROVED", chars=400, sources=True, answers=False, age_days=5, lesson=L_LIN, items=None):
    meta = {"answers_included": True} if answers else {}
    if items:
        meta["structured"] = {"items": [{"ref": f"{asset_type}-{i}", "data": d} for i, d in enumerate(items)]}
    ts = datetime.now(timezone.utc) - timedelta(days=age_days)
    a = LearningAsset(education_system="Egypt", stage="Preparatory", grade=GRADE, subject=SUBJECT, lesson=lesson, asset_type=asset_type,
                      title=f"{asset_type} {lesson}", content=("ن" * chars), language="ar", status=status, created_by="test",
                      sources=[{"title": "كتاب", "url": "https://textbook.example/p1"}] if sources else [], meta=meta,
                      curriculum_node_id=node_id, reviewed_at=ts if status == "APPROVED" else None)
    db.add(a)
    db.commit()
    if age_days:
        a.created_at = a.updated_at = ts
        db.commit()
    return a


def full_math_lesson(db, node_id, **kw):
    """Everything the 'mathematics' profile requires, approved, sourced and with answers."""
    out = [add_asset(db, node_id, "explanation", chars=400, **kw), add_asset(db, node_id, "definition", chars=80, **kw),
           add_asset(db, node_id, "example", chars=120, **kw), add_asset(db, node_id, "example", chars=120, **kw),
           add_asset(db, node_id, "summary", chars=120, **kw)]
    for _ in range(3):
        out.append(add_asset(db, node_id, "exercise", chars=60, answers=True, **kw))
    out.append(add_asset(db, node_id, "quiz", chars=60, answers=True, **kw))
    return out
