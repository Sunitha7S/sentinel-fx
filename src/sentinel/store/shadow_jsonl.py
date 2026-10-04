"""JSON-lines store for shadow-trade records (replaced by PostgreSQL in M2).

Records are written and read through the ``ShadowTradeMsg`` boundary schema, so the file
format is the same typed contract other services will consume.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from sentinel.domain.decision import ShadowTradeRecord
from sentinel.schemas.messages import ShadowTradeMsg

__all__ = ["JsonlShadowStore"]


class JsonlShadowStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, record: ShadowTradeRecord) -> None:
        line = ShadowTradeMsg.from_domain(record).model_dump_json()
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def __iter__(self) -> Iterator[ShadowTradeRecord]:
        if not self._path.exists():
            return iter(())
        with self._path.open(encoding="utf-8") as fh:
            lines = [line for line in fh if line.strip()]
        return iter([ShadowTradeMsg.model_validate_json(line).to_domain() for line in lines])
