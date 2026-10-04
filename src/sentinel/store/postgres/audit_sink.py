"""PostgreSQL audit sink with a separately anchored head (ADR 0009, ADR 0011).

``append`` inserts one record; the database trigger checks it extends the head and moves the
head. ``verify`` recomputes every hash and checks the chain ends exactly at the stored head,
which detects edits, deletions, reorders, truncation and full re-hashing of the records by
anyone who cannot also write ``audit_head``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC

from sqlalchemy import Engine, TextClause, text

from sentinel.audit.chain import AuditHead, AuditKind, AuditRecord, verify_chain
from sentinel.domain.canonical import canonical_json

__all__ = ["PostgresAuditSink"]

_SELECT_ALL = text(
    "SELECT seq, recorded_at, kind, subject_id, payload, prev_hash, hash "
    "FROM sentinel.audit_records ORDER BY seq"
)
_SELECT_TAIL = text(
    "SELECT seq, recorded_at, kind, subject_id, payload, prev_hash, hash "
    "FROM sentinel.audit_records ORDER BY seq DESC LIMIT 1"
)


class PostgresAuditSink:
    """Writes need ``svc_audit``. Payloads are stored as the exact canonical JSON that was
    hashed, so verification is byte-exact."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def append(self, record: AuditRecord) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sentinel.audit_records "
                    "(seq, recorded_at, kind, subject_id, payload, prev_hash, hash) "
                    "VALUES (:seq, :at, :kind, :subject, :payload, :prev, :hash)"
                ),
                {
                    "seq": record.seq,
                    "at": record.recorded_at,
                    "kind": record.kind.value,
                    "subject": record.subject_id,
                    "payload": canonical_json(record.payload),
                    "prev": record.prev_hash,
                    "hash": record.hash,
                },
            )

    def tail(self) -> AuditRecord | None:
        records = self._select(_SELECT_TAIL)
        return records[0] if records else None

    def head(self) -> AuditHead:
        with self._engine.connect() as conn:
            row = conn.execute(text("SELECT seq, hash FROM sentinel.audit_head")).one()
        return AuditHead(row.seq, row.hash)

    def verify(self) -> int:
        """Verify every record against the anchored head. Returns the record count."""
        head = self.head()
        records = list(self)
        if head.seq == 0 and not records:
            return 0
        return verify_chain(records, expected_head=head)

    def __iter__(self) -> Iterator[AuditRecord]:
        return iter(self._select(_SELECT_ALL))

    def _select(self, statement: TextClause) -> list[AuditRecord]:
        with self._engine.connect() as conn:
            rows = conn.execute(statement).all()
        return [
            AuditRecord(
                seq=r.seq,
                recorded_at=r.recorded_at.astimezone(UTC),
                kind=AuditKind(r.kind),
                subject_id=r.subject_id,
                payload=json.loads(r.payload),
                prev_hash=r.prev_hash,
                hash=r.hash,
            )
            for r in rows
        ]
