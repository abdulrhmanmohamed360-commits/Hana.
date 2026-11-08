import uuid


def _topic(client, editor, domain_id, slug, parent_id=None):
    return client.post("/api/v1/topics", json={"domain_id": domain_id, "slug": slug, "name": slug.title(),
                                                "parent_id": parent_id}, headers=editor)


def test_topic_hierarchy_paths_and_depth(client, make_domain, editor, reader):
    d = make_domain()
    root = _topic(client, editor, d["id"], "algebra").json()
    child = _topic(client, editor, d["id"], "linear-equations", root["id"]).json()
    assert root["path"] == "algebra" and root["depth"] == 0
    assert child["path"] == "algebra/linear-equations" and child["depth"] == 1
    kids = client.get(f"/api/v1/topics?parent_id={root['id']}", headers=reader).json()
    assert [t["slug"] for t in kids["items"]] == ["linear-equations"]
    roots = client.get(f"/api/v1/topics?domain_id={d['id']}&roots_only=true", headers=reader).json()
    assert roots["total"] == 1


def test_same_slug_allowed_under_different_parents_but_not_twice_under_same(client, make_domain, editor):
    d = make_domain()
    a = _topic(client, editor, d["id"], "a").json()
    b = _topic(client, editor, d["id"], "b").json()
    assert _topic(client, editor, d["id"], "intro", a["id"]).status_code == 201
    assert _topic(client, editor, d["id"], "intro", b["id"]).status_code == 201
    assert _topic(client, editor, d["id"], "intro", a["id"]).status_code == 409


def test_parent_must_be_in_same_domain(client, make_domain, editor):
    d1 = make_domain()
    d2 = make_domain(slug="physics", name="Physics")
    p = _topic(client, editor, d1["id"], "algebra").json()
    assert _topic(client, editor, d2["id"], "x", p["id"]).status_code == 409


def test_unknown_domain_or_parent_is_404(client, editor):
    assert _topic(client, editor, str(uuid.uuid4()), "x").status_code == 404


def test_cannot_delete_topic_with_children(client, make_domain, editor):
    d = make_domain()
    root = _topic(client, editor, d["id"], "algebra").json()
    child = _topic(client, editor, d["id"], "linear", root["id"]).json()
    assert client.delete(f"/api/v1/topics/{root['id']}", headers=editor).status_code == 409
    assert client.delete(f"/api/v1/topics/{child['id']}", headers=editor).status_code == 204
    assert client.delete(f"/api/v1/topics/{root['id']}", headers=editor).status_code == 204


def test_depth_limit(client, make_domain, editor):
    d = make_domain()
    parent = None
    for i in range(8):
        r = _topic(client, editor, d["id"], f"t{i}", parent)
        assert r.status_code == 201
        parent = r.json()["id"]
    assert _topic(client, editor, d["id"], "too-deep", parent).status_code == 409
