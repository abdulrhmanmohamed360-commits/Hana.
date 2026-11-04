def test_missing_key_is_401_with_consistent_error_shape(client):
    r = client.get("/api/v1/domains")
    assert r.status_code == 401
    body = r.json()["error"]
    assert body["code"] == "unauthorized" and "message" in body and "details" in body


def test_invalid_key_is_401(client):
    assert client.get("/api/v1/domains", headers={"X-API-Key": "nope"}).status_code == 401


def test_reader_can_read_but_not_write(client, reader):
    assert client.get("/api/v1/domains", headers=reader).status_code == 200
    r = client.post("/api/v1/domains", json={"slug": "math", "name": "Math"}, headers=reader)
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"


def test_editor_cannot_delete_domain_but_admin_can(client, make_domain, editor, admin):
    d = make_domain()
    assert client.delete(f"/api/v1/domains/{d['id']}", headers=editor).status_code == 403
    assert client.delete(f"/api/v1/domains/{d['id']}", headers=admin).status_code == 204


def test_unconfigured_role_keys_are_disabled(fastapi_app, client):
    from app.core.config import Settings, get_settings

    fastapi_app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, admin_api_key="only-admin", rate_limit_backend="memory")
    assert client.get("/api/v1/domains", headers={"X-API-Key": ""}).status_code == 401
    assert client.get("/api/v1/domains", headers={"X-API-Key": "only-admin"}).status_code == 200


def test_audit_logs_admin_only(client, editor, admin, make_domain):
    make_domain()
    assert client.get("/api/v1/audit-logs", headers=editor).status_code == 403
    assert client.get("/api/v1/audit-logs", headers=admin).status_code == 200
