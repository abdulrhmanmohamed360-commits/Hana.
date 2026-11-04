import pytest

from app.core.url_policy import UrlPolicyError, validate_url


def fake(mapping):
    return lambda host: mapping[host]


@pytest.mark.parametrize("url", [
    "ftp://example.org/x", "file:///etc/passwd", "gopher://x", "javascript:alert(1)", "http://", "not a url",
    "http://user:pass@example.org/", "http://127.0.0.1/", "http://[::1]/", "http://10.1.2.3/", "http://192.168.0.1/",
    "http://172.16.0.9/", "http://169.254.169.254/", "http://0.0.0.0/", "http://[::ffff:127.0.0.1]/",
    "http://100.64.0.1/", "http://localhost/", "http://foo.localhost/", "http://db.internal/",
])
def test_rejected_without_dns(url):
    with pytest.raises(UrlPolicyError):
        validate_url(url, resolve=False)


def test_hostname_resolving_to_private_address_is_rejected():
    with pytest.raises(UrlPolicyError):
        validate_url("https://evil.example/", resolver=fake({"evil.example": ["10.0.0.7"]}))


def test_any_private_address_among_many_rejects_dns_rebinding_style():
    with pytest.raises(UrlPolicyError):
        validate_url("https://mixed.example/", resolver=fake({"mixed.example": ["93.184.216.34", "127.0.0.1"]}))


def test_public_hostname_accepted_and_normalized():
    v = validate_url("HTTPS://Example.ORG./path?q=1", resolver=fake({"example.org": ["93.184.216.34"]}))
    assert v.host == "example.org" and v.scheme == "https"


def test_private_allowed_only_when_explicit():
    assert validate_url("http://127.0.0.1:8080/", allow_private=True, resolve=False).port == 8080


def test_unresolvable_host_rejected():
    with pytest.raises(UrlPolicyError):
        validate_url("https://x.example/", resolver=fake({"x.example": []}))
