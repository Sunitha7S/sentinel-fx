"""Append-only JSON-lines audit sink.

Each record is one line, flushed and fsync'd before ``append`` returns, so a record that
was reported as written survives a crash. On open, the whole file is re-verified; a broken
chain refuses to load rather than silently continuing from a tampered log.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from sentinel.audit.chain import AuditKind, AuditRecord, verify_chain

__all__ = ["JsonlAuditSink"]


def _to_json(record: AuditRecord) -> str:
    return json.dumps(
        {
            "seq": record.seq,
            "recorded_at": record.recorded_at.isoformat(),
            "kind": record.kind.value,
            "subject_id": record.subject_id,
            "payload": record.payload,
            "prev_hash": record.prev_hash,
            "hash": record.hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _from_json(line: str) -> AuditRecord:
    doc = json.loads(line)
    return AuditRecord(
        seq=doc["seq"],
        recorded_at=datetime.fromisoformat(doc["recorded_at"]),
        kind=AuditKind(doc["kind"]),
        subject_id=doc["subject_id"],
        payload=doc["payload"],
        prev_hash=doc["prev_hash"],
        hash=doc["hash"],
    )


class JsonlAuditSink:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._records: list[AuditRecord] = []
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                self._records = [_from_json(line) for line in fh if line.strip()]
            verify_chain(self._records)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: AuditRecord) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(_to_json(record) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._records.append(record)

    def tail(self) -> AuditRecord | None:
        return self._records[-1] if self._records else None

    def __iter__(self) -> Iterator[AuditRecord]:
        return iter(tuple(self._records))
