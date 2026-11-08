import uuid


def test_create_and_get_domain(client, make_domain, reader):
    d = make_domain(slug="arabic-language", name="Arabic Language", default_language="ar")
    got = client.get(f"/api/v1/domains/{d['id']}", headers=reader).json()
    assert got["slug"] == "arabic-language" and got["default_language"] == "ar" and got["is_active"] is True


def test_duplicate_slug_conflict(client, make_domain, editor):
    make_domain()
    r = client.post("/api/v1/domains", json={"slug": "mathematics", "name": "Again"}, headers=editor)
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"


def test_invalid_slug_is_validation_error(client, editor):
    r = client.post("/api/v1/domains", json={"slug": "Bad Slug!", "name": "x"}, headers=editor)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_pagination(client, make_domain, reader):
    for i in range(5):
        make_domain(slug=f"domain-{i}", name=f"D{i}")
    r = client.get("/api/v1/domains?page=2&page_size=2", headers=reader).json()
    assert r["total"] == 5 and r["page"] == 2 and r["page_size"] == 2 and len(r["items"]) == 2
    assert client.get("/api/v1/domains?page=0", headers=reader).status_code == 422


def test_page_size_capped(client, make_domain, reader):
    make_domain()
    r = client.get("/api/v1/domains?page_size=100000", headers=reader).json()
    assert r["page_size"] == 100


def test_patch_domain(client, make_domain, editor):
    d = make_domain()
    r = client.patch(f"/api/v1/domains/{d['id']}", json={"name": "Maths", "is_active": False}, headers=editor)
    assert r.status_code == 200 and r.json()["name"] == "Maths" and r.json()["is_active"] is False


def test_not_found(client, reader):
    r = client.get(f"/api/v1/domains/{uuid.uuid4()}", headers=reader)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_soft_delete_hides_domain_and_frees_slug(client, make_domain, admin, reader):
    d = make_domain()
    assert client.delete(f"/api/v1/domains/{d['id']}", headers=admin).status_code == 204
    assert client.get(f"/api/v1/domains/{d['id']}", headers=reader).status_code == 404
    assert client.get("/api/v1/domains", headers=reader).json()["total"] == 0
    make_domain()  # same slug can be registered again


def test_cannot_delete_domain_with_topics(client, make_domain, admin, editor):
    d = make_domain()
    client.post("/api/v1/topics", json={"domain_id": d["id"], "slug": "algebra", "name": "Algebra"}, headers=editor)
    assert client.delete(f"/api/v1/domains/{d['id']}", headers=admin).status_code == 409


def test_every_write_is_audited(client, make_domain, editor, admin):
    d = make_domain()
    client.patch(f"/api/v1/domains/{d['id']}", json={"name": "N"}, headers=editor)
    logs = client.get("/api/v1/audit-logs?entity_type=knowledge_domain", headers=admin).json()
    assert {i["action"] for i in logs["items"]} == {"domain.create", "domain.update"}
    assert all(i["actor"] == "editor" for i in logs["items"])


def test_request_id_header_is_returned(client, reader):
    r = client.get("/api/v1/domains", headers={**reader, "X-Request-ID": "abc123"})
    assert r.headers["X-Request-ID"] == "abc123"
