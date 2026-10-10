"""PostgreSQL-backed protected policy store (INV-POLICY-IMMUTABLE).

Writes need two independent things: a ``HumanRiskAdmin`` capability in the application, and
a connection acting as ``human_admin`` in the database. A component holding only one of
them cannot change policy. Every load re-parses the stored policy and recomputes its hash,
so a stored version that no longer matches its key is refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, text

from sentinel.domain.types import DomainError, require_utc
from sentinel.risk.governance import (
    Activation,
    GovernanceError,
    HumanRiskAdmin,
    require_human_risk_admin,
)
from sentinel.risk.policy import PolicyReader, RiskPolicy

__all__ = ["ActivationRecord", "PolicyIntegrityError", "PostgresPolicyStore"]


class PolicyIntegrityError(DomainError):
    """A stored policy does not hash to the key it is stored under."""


@dataclass(frozen=True, slots=True)
class ActivationRecord:
    seq: int
    policy_sha256: str
    based_on_sha256: str | None
    direction: str
    request_id: str
    approved_by: str
    approved_at: datetime
    effective_at: datetime


_INSERT_VERSION = text(
    """
    INSERT INTO sentinel.policy_versions (sha256, policy_id, content, parent_sha256, created_by)
    VALUES (:sha, :pid, CAST(:content AS jsonb), :parent, :by)
    ON CONFLICT (sha256) DO NOTHING
    """
)
_INSERT_ACTIVATION = text(
    """
    INSERT INTO sentinel.policy_activations
      (seq, policy_sha256, based_on_sha256, direction, request_id, reason,
       approved_by, approved_at, effective_at)
    VALUES (:seq, :sha, :based_on, :direction, :rid, :reason, :by, :approved, :effective)
    """
)


class PostgresPolicyStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------ reads

    def _load(self, conn: Connection, sha: str) -> RiskPolicy:
        row = conn.execute(
            text("SELECT content FROM sentinel.policy_versions WHERE sha256 = :s"), {"s": sha}
        ).one_or_none()
        if row is None:
            raise PolicyIntegrityError(f"policy version {sha} not found")
        try:
            policy = RiskPolicy.from_mapping(row.content)
        except DomainError as exc:
            raise PolicyIntegrityError(f"stored policy {sha} no longer parses: {exc}") from exc
        if policy.sha256 != sha:
            raise PolicyIntegrityError(
                f"stored policy {sha} hashes to {policy.sha256}: content was altered"
            )
        return policy

    def _tip(self, conn: Connection) -> tuple[int, str] | None:
        row = conn.execute(
            text(
                "SELECT seq, policy_sha256 FROM sentinel.policy_activations "
                "ORDER BY seq DESC LIMIT 1"
            )
        ).one_or_none()
        return None if row is None else (row.seq, row.policy_sha256)

    def active(self) -> RiskPolicy:
        with self._engine.connect() as conn:
            tip = self._tip(conn)
            if tip is None:
                raise GovernanceError("no policy has been activated")
            return self._load(conn, tip[1])

    def active_sha256(self) -> str:
        return self.active().sha256

    def version(self, sha: str) -> RiskPolicy:
        with self._engine.connect() as conn:
            return self._load(conn, sha)

    def history(self) -> tuple[ActivationRecord, ...]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT seq, policy_sha256, based_on_sha256, direction, request_id, "
                    "approved_by, approved_at, effective_at "
                    "FROM sentinel.policy_activations ORDER BY seq"
                )
            ).all()
        return tuple(
            ActivationRecord(
                seq=r.seq,
                policy_sha256=r.policy_sha256,
                based_on_sha256=r.based_on_sha256,
                direction=r.direction,
                request_id=r.request_id,
                approved_by=r.approved_by,
                approved_at=r.approved_at.astimezone(UTC),
                effective_at=r.effective_at.astimezone(UTC),
            )
            for r in rows
        )

    def reader(self) -> PolicyReader:
        return _Reader(self)

    # ------------------------------------------------------------------ writes

    def _insert_version(
        self, conn: Connection, policy: RiskPolicy, parent: str | None, by: str
    ) -> None:
        conn.execute(
            _INSERT_VERSION,
            {
                "sha": policy.sha256,
                "pid": policy.policy_id,
                "content": json.dumps(policy.to_mapping(), sort_keys=True),
                "parent": parent,
                "by": by,
            },
        )

    def install_initial(
        self,
        policy: RiskPolicy,
        *,
        installed_by: HumanRiskAdmin,
        request_id: str,
        reason: str,
        at: datetime,
    ) -> None:
        admin = require_human_risk_admin(installed_by)
        at = require_utc(at, field="at")
        with self._engine.begin() as conn:
            self._insert_version(conn, policy, None, admin.identity)
            conn.execute(
                _INSERT_ACTIVATION,
                {
                    "seq": 1,
                    "sha": policy.sha256,
                    "based_on": None,
                    "direction": "INITIAL",
                    "rid": request_id,
                    "reason": reason,
                    "by": admin.identity,
                    "approved": at,
                    "effective": at,
                },
            )

    def activate(
        self, activation: Activation, *, approver: HumanRiskAdmin, now: datetime
    ) -> RiskPolicy:
        admin = require_human_risk_admin(approver)
        now = require_utc(now, field="now")
        if now < activation.effective_at:
            raise GovernanceError(
                f"cooling-off period: effective at {activation.effective_at.isoformat()}"
            )
        request = activation.request
        with self._engine.begin() as conn:
            tip = self._tip(conn)
            if tip is None or tip[1] != request.based_on_sha256:
                raise GovernanceError("request was based on a policy that is no longer active")
            self._insert_version(conn, request.proposed, request.based_on_sha256, admin.identity)
            conn.execute(
                _INSERT_ACTIVATION,
                {
                    "seq": tip[0] + 1,
                    "sha": request.proposed.sha256,
                    "based_on": request.based_on_sha256,
                    "direction": activation.direction.value,
                    "rid": request.request_id,
                    "reason": request.reason,
                    "by": admin.identity,
                    "approved": activation.approved_at,
                    "effective": activation.effective_at,
                },
            )
        return request.proposed


@dataclass(frozen=True, slots=True)
class _Reader:
    _store: PostgresPolicyStore

    def active(self) -> RiskPolicy:
        return self._store.active()

    def active_sha256(self) -> str:
        return self._store.active_sha256()
