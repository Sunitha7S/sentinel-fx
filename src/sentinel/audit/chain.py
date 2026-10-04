"""Hash-chained audit records.

Each record's hash covers its sequence number, timestamp, kind, subject, canonical payload
and the previous record's hash. Editing, inserting, reordering or deleting any record
breaks verification from that point on.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from sentinel.domain.canonical import JsonValue, canonical, canonical_json
from sentinel.domain.types import require_utc

__all__ = [
    "GENESIS_HASH",
    "AuditChainError",
    "AuditKind",
    "AuditLog",
    "AuditRecord",
    "AuditSink",
    "InMemoryAuditSink",
    "seal",
    "verify_chain",
]

GENESIS_HASH = "0" * 64


class AuditChainError(Exception):
    """The audit chain is broken: a record was altered, removed, inserted or reordered."""


class AuditKind(StrEnum):
    RISK_DECISION = "RISK_DECISION"
    APPROVALS_ISSUED = "APPROVALS_ISSUED"
    STATE_TRANSITION = "STATE_TRANSITION"
    STATE_TRANSITION_REFUSED = "STATE_TRANSITION_REFUSED"
    POLICY_ACTIVATED = "POLICY_ACTIVATED"
    EXECUTION_REFUSED = "EXECUTION_REFUSED"


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    recorded_at: datetime
    kind: AuditKind
    subject_id: str
    payload: JsonValue
    prev_hash: str
    hash: str

    def body(self) -> dict[str, JsonValue]:
        return {
            "seq": self.seq,
            "recorded_at": canonical(self.recorded_at),
            "kind": self.kind.value,
            "subject_id": self.subject_id,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }


def _digest(body: dict[str, JsonValue]) -> str:
    return hashlib.sha256(canonical_json(body).encode("ascii")).hexdigest()


def seal(
    *,
    seq: int,
    recorded_at: datetime,
    kind: AuditKind,
    subject_id: str,
    payload: object,
    prev_hash: str,
) -> AuditRecord:
    unsealed = AuditRecord(
        seq=seq,
        recorded_at=require_utc(recorded_at, field="recorded_at"),
        kind=kind,
        subject_id=subject_id,
        payload=canonical(payload),
        prev_hash=prev_hash,
        hash="",
    )
    return dataclasses.replace(unsealed, hash=_digest(unsealed.body()))


def verify_chain(records: Iterable[AuditRecord]) -> int:
    """Verify an entire chain from genesis. Returns the number of records."""
    prev = GENESIS_HASH
    count = 0
    for expected_seq, record in enumerate(records, start=1):
        if record.seq != expected_seq:
            raise AuditChainError(f"sequence gap: expected {expected_seq}, found {record.seq}")
        if record.prev_hash != prev:
            raise AuditChainError(f"record {record.seq}: previous-hash mismatch")
        if _digest(record.body()) != record.hash:
            raise AuditChainError(f"record {record.seq}: content does not match its hash")
        prev = record.hash
        count += 1
    return count


class AuditSink(Protocol):
    def append(self, record: AuditRecord) -> None: ...

    def tail(self) -> AuditRecord | None: ...

    def __iter__(self) -> Iterator[AuditRecord]: ...


class InMemoryAuditSink:
    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self._records.append(record)

    def tail(self) -> AuditRecord | None:
        return self._records[-1] if self._records else None

    def __iter__(self) -> Iterator[AuditRecord]:
        return iter(tuple(self._records))


class AuditLog:
    """Appends sealed records to a sink. A failed append raises; callers fail closed."""

    def __init__(self, sink: AuditSink, *, clock: Callable[[], datetime]) -> None:
        self._sink = sink
        self._clock = clock

    def record(self, kind: AuditKind, subject_id: str, payload: object) -> AuditRecord:
        last = self._sink.tail()
        record = seal(
            seq=(last.seq + 1) if last else 1,
            recorded_at=self._clock(),
            kind=kind,
            subject_id=subject_id,
            payload=payload,
            prev_hash=last.hash if last else GENESIS_HASH,
        )
        self._sink.append(record)
        return record
