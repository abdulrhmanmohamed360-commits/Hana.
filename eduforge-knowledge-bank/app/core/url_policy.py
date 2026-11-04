"""SSRF protection for outbound fetching. Pure stdlib so it is easy to test and audit.

validate_url() enforces: http/https only, no embedded credentials, a real hostname, and (when resolve=True)
that *every* resolved address is publicly routable. Private, loopback, link-local, multicast, reserved and
cloud-metadata ranges are rejected unless allow_private=True (development only).
"""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

Resolver = Callable[[str], list[str]]


class UrlPolicyError(ValueError):
    """Raised when a URL is not allowed to be fetched."""


@dataclass(frozen=True)
class ValidatedUrl:
    url: str
    scheme: str
    host: str
    port: int | None


def system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UrlPolicyError(f"Host '{host}' could not be resolved") from exc
    return sorted({info[4][0] for info in infos})


def _check_ip(raw: str, allow_private: bool) -> None:
    ip = ipaddress.ip_address(raw.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if allow_private:
        return
    if not ip.is_global or ip.is_multicast:
        raise UrlPolicyError(f"Address {ip} is not a public address")


def normalize_host(host: str) -> str:
    host = host.strip().rstrip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UrlPolicyError("Invalid hostname") from exc


def validate_url(url: str, *, allow_private: bool = False, resolve: bool = True,
                 resolver: Resolver = system_resolver) -> ValidatedUrl:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise UrlPolicyError("Malformed URL") from exc
    if parts.scheme not in ("http", "https"):
        raise UrlPolicyError("Only http and https URLs are allowed")
    if parts.username or parts.password:
        raise UrlPolicyError("URLs with embedded credentials are not allowed")
    if not parts.hostname:
        raise UrlPolicyError("URL has no hostname")
    host = normalize_host(parts.hostname)

    try:  # literal IP address: check directly, no DNS involved
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False

    if is_literal:
        _check_ip(host, allow_private)
    elif not allow_private and (host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
        raise UrlPolicyError("Local hostnames are not allowed")
    elif resolve:
        addresses = resolver(host)
        if not addresses:
            raise UrlPolicyError(f"Host '{host}' did not resolve")
        for address in addresses:
            _check_ip(address, allow_private)
    return ValidatedUrl(url=url.strip(), scheme=parts.scheme, host=host, port=port)
