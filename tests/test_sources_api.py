PUBLIC_IP_URL = "http://93.184.216.34/"  # literal public IP: no DNS needed in tests


def _register(client, editor, url=PUBLIC_IP_URL, name="Example"):
    return client.post("/api/v1/sources", json={"name": name, "base_url": url}, headers=editor)


def test_new_source_starts_pending(client, editor):
    r = _register(client, editor)
    assert r.status_code == 201
    assert r.json()["policy"] == "PENDING" and r.json()["host"] == "93.184.216.34"


def test_policy_cannot_be_set_at_creation(client, editor):
    r = client.post("/api/v1/sources", json={"name": "x", "base_url": PUBLIC_IP_URL, "policy": "ALLOWED"}, headers=editor)
    assert r.status_code == 201 and r.json()["policy"] == "PENDING"


def test_duplicate_host_conflict(client, editor):
    _register(client, editor)
    assert _register(client, editor, "http://93.184.216.34/other").status_code == 409


def test_private_and_loopback_sources_rejected(client, editor):
    for url in ("http://127.0.0.1/", "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data", "http://localhost/",
                "ftp://example.org/", "http://user:pw@93.184.216.34/"):
        assert _register(client, editor, url).status_code == 422, url


def test_only_admin_changes_policy(client, editor, admin):
    sid = _register(client, editor).json()["id"]
    r = client.patch(f"/api/v1/sources/{sid}", json={"policy": "ALLOWED"}, headers=editor)
    assert r.status_code == 403
    assert client.patch(f"/api/v1/sources/{sid}", json={"authority_score": 0.9}, headers=editor).status_code == 200
    r = client.patch(f"/api/v1/sources/{sid}", json={"policy": "ALLOWED"}, headers=admin)
    assert r.status_code == 200 and r.json()["policy"] == "ALLOWED"


def test_authority_score_validation(client, editor):
    r = client.post("/api/v1/sources", json={"name": "x", "base_url": PUBLIC_IP_URL, "authority_score": 2}, headers=editor)
    assert r.status_code == 422


def test_check_url_lifecycle(client, editor, admin):
    chk = lambda u: client.post("/api/v1/sources/check-url", json={"url": u}, headers=editor).json()  # noqa: E731
    assert chk("http://127.0.0.1/admin")["reason"].startswith("ssrf_policy")
    assert chk("http://93.184.216.34/page")["reason"] == "host_not_registered"
    sid = _register(client, editor).json()["id"]
    assert chk("http://93.184.216.34/page")["reason"] == "source_pending_approval"
    client.patch(f"/api/v1/sources/{sid}", json={"policy": "ALLOWED"}, headers=admin)
    assert chk("http://93.184.216.34/page") == {"allowed": True, "host": "93.184.216.34", "reason": "allowed"}
    client.patch(f"/api/v1/sources/{sid}", json={"policy": "DENIED"}, headers=admin)
    assert chk("http://93.184.216.34/page")["reason"] == "source_denied"


def test_list_filter_by_policy_and_policy_change_audited(client, editor, admin):
    sid = _register(client, editor).json()["id"]
    client.patch(f"/api/v1/sources/{sid}", json={"policy": "ALLOWED"}, headers=admin)
    assert client.get("/api/v1/sources?policy=ALLOWED", headers=editor).json()["total"] == 1
    assert client.get("/api/v1/sources?policy=DENIED", headers=editor).json()["total"] == 0
    logs = client.get("/api/v1/audit-logs?action=source.update", headers=admin).json()["items"]
    assert logs[0]["details"]["policy_change"] == {"from": "PENDING", "to": "ALLOWED"}
