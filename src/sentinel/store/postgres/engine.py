"""Engine construction: one engine per database role."""

from __future__ import annotations

from typing import Final

from sqlalchemy import Engine, create_engine

__all__ = ["OWNER_ROLE", "SERVICE_ROLES", "role_engine"]

OWNER_ROLE: Final = "sentinel_owner"
SERVICE_ROLES: Final = (
    "human_admin",
    "svc_risk",
    "svc_learning",
    "svc_audit",
    "svc_execution",
    "svc_market_data",
)


def role_engine(url: str, role: str | None = None, **kwargs: object) -> Engine:
    """An engine whose sessions act as ``role``.

    In production each service logs in as its own LOGIN role that is a member of exactly
    one group role, and ``role`` is left as ``None``. ``role`` exists so that a login that
    belongs to several groups (and the test suite, connecting as a superuser) can pin every
    session to one group via ``SET ROLE`` at connection start. Privileges are then exactly
    those of that group.
    """
    # Sessions always run in UTC, so timestamps come back as UTC whatever the server's zone.
    options = ["-c timezone=UTC"]
    if role is not None:
        if role not in (*SERVICE_ROLES, OWNER_ROLE):
            raise ValueError(f"unknown database role {role!r}")
        options.append(f"-c role={role}")
    return create_engine(url, connect_args={"options": " ".join(options)}, **kwargs)
