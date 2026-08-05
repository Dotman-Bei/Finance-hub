"""RBAC for the reporting gateway (build.md Sec. 12, Sec. 3.4.1).

    "Three roles - Finance Manager, Auditor, System Administrator - each with a
     scoped view. Enforce at the API layer with JWT + role claims."

The API layer is the enforcement point. The dashboard also hides controls a
role cannot use, but that is presentation: a request that reaches this service
is authorised here or not at all.

The `X-FinanceHub-Role` header the frontend sends is **not** trusted for
authorisation. It is a display hint only - accepting it as a permission grant
would let anyone become an administrator by editing a request header. The role
comes from the signed JWT.

`REQUIRE_AUTH=false` is available for local development against a stack with no
identity provider. It is loud about it at startup and must never be set in a
deployment - a fail-open auth layer that is quiet about being open is how
access control silently disappears.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from enum import Enum
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from shared.config import settings

logger = logging.getLogger(__name__)

REQUIRE_AUTH = os.getenv("REQUIRE_AUTH", "true").lower() not in ("false", "0", "no")

bearer_scheme = HTTPBearer(auto_error=False)


class Role(str, Enum):
    FINANCE_MANAGER = "FINANCE_MANAGER"
    AUDITOR = "AUDITOR"
    SYSTEM_ADMINISTRATOR = "SYSTEM_ADMINISTRATOR"


class Permission(str, Enum):
    VIEW_METRICS = "view_metrics"
    VIEW_EXCEPTIONS = "view_exceptions"
    RESOLVE_EXCEPTIONS = "resolve_exceptions"
    GENERATE_REPORTS = "generate_reports"
    VIEW_AUDIT_TRAIL = "view_audit_trail"
    RUN_RECONCILIATION = "run_reconciliation"


#: Sec. 3.4.1's three scoped views.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.FINANCE_MANAGER: frozenset(
        {
            Permission.VIEW_METRICS,
            Permission.VIEW_EXCEPTIONS,
            Permission.RESOLVE_EXCEPTIONS,
            Permission.GENERATE_REPORTS,
            Permission.RUN_RECONCILIATION,
        }
    ),
    # Read-only by design: an auditor who can alter the records they audit is
    # not an auditor.
    Role.AUDITOR: frozenset(
        {
            Permission.VIEW_METRICS,
            Permission.VIEW_EXCEPTIONS,
            Permission.GENERATE_REPORTS,
            Permission.VIEW_AUDIT_TRAIL,
        }
    ),
    Role.SYSTEM_ADMINISTRATOR: frozenset(Permission),
}


class Principal:
    """The authenticated caller."""

    def __init__(self, subject: str, role: Role, claims: dict[str, Any] | None = None):
        self.subject = subject
        self.role = role
        self.claims = claims or {}

    @property
    def permissions(self) -> frozenset[Permission]:
        return ROLE_PERMISSIONS[self.role]

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "role": self.role.value,
            "permissions": sorted(p.value for p in self.permissions),
        }


# ── Token issue and verification ─────────────────────────────────────────


def issue_token(subject: str, role: Role, expires_minutes: int | None = None) -> str:
    """Mint a signed JWT. The role travels as a claim, never as a header."""
    now = dt.datetime.now(dt.timezone.utc)
    minutes = expires_minutes or settings.jwt_expire_minutes
    payload = {
        "sub": subject,
        "role": role.value,
        "iat": now,
        "exp": now + dt.timedelta(minutes=minutes),
        "iss": "financehub",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> Principal:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer="financehub",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired"
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {exc}"
        )

    raw_role = claims.get("role")
    try:
        role = Role(raw_role)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Token carries an unknown role: {raw_role!r}",
        )

    return Principal(subject=claims.get("sub", "unknown"), role=role, claims=claims)


# ── Dependencies ─────────────────────────────────────────────────────────


def get_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> Principal:
    if not REQUIRE_AUTH:
        # Development only. The startup banner says so loudly.
        return Principal(subject="dev@localhost", role=Role.SYSTEM_ADMINISTRATOR)

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return decode_token(credentials.credentials)


def requires(permission: Permission):
    """Dependency factory: `Depends(requires(Permission.GENERATE_REPORTS))`."""

    def guard(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.can(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Role {principal.role.value} is not permitted to "
                    f"{permission.value}"
                ),
            )
        return principal

    return guard


def warn_if_auth_disabled() -> None:
    if not REQUIRE_AUTH:
        logger.warning(
            "REQUIRE_AUTH=false - every request is treated as SYSTEM_ADMINISTRATOR. "
            "This is for local development only and must never be set in a "
            "deployment."
        )


__all__ = [
    "Role",
    "Permission",
    "Principal",
    "ROLE_PERMISSIONS",
    "issue_token",
    "decode_token",
    "get_principal",
    "requires",
    "REQUIRE_AUTH",
    "warn_if_auth_disabled",
]
