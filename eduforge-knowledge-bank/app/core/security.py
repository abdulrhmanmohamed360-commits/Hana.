"""API-key authentication and role-based access control (RBAC).

Roles are ordered: READER < EDITOR < ADMIN. Keys come from environment variables only.
If no key is configured for a role, that role is disabled (secure by default).
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable

from fastapi import Depends, Header

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, UnauthorizedError


class Role(IntEnum):
    READER = 10
    EDITOR = 20
    ADMIN = 30


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role

    @property
    def actor(self) -> str:
        return f"{self.role.name.lower()}"


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def authenticate_key(presented: str, settings: Settings) -> Principal | None:
    """Constant-time comparison against every configured key."""
    presented_digest = _digest(presented)
    found: Principal | None = None
    for role, secret in (
        (Role.ADMIN, settings.admin_api_key),
        (Role.EDITOR, settings.editor_api_key),
        (Role.READER, settings.reader_api_key),
    ):
        if secret is None or not secret.get_secret_value():
            continue
        if hmac.compare_digest(presented_digest, _digest(secret.get_secret_value())):
            found = Principal(name=role.name.lower(), role=role)
    return found


def get_principal(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> Principal:
    if not x_api_key:
        raise UnauthorizedError("Missing X-API-Key header", headers={"WWW-Authenticate": "ApiKey"})
    principal = authenticate_key(x_api_key, settings)
    if principal is None:
        raise UnauthorizedError("Invalid API key", headers={"WWW-Authenticate": "ApiKey"})
    return principal


def require_role(minimum: Role) -> Callable[..., Principal]:
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.role < minimum:
            raise ForbiddenError(f"Requires role {minimum.name.lower()} or higher")
        return principal

    return dependency
