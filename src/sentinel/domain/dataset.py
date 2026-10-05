"""Dataset snapshots: digest format v1 and the VerifiedDataset capability (ADR 0014).

A snapshot pins an exact market-data series range ``[start, end)`` from one source. Its
SHA-256 is computed here, purely, from the candles themselves, so the same rows give the same
hash on every machine, OS, Python version and database:

* rows are ordered by strictly increasing open time, each must belong to the spec;
* every row is one ASCII line ``ts|bid_o|bid_h|bid_l|bid_c|ask_o|ask_h|ask_l|ask_c|volume``
  terminated by LF. ``ts`` is UTC with microseconds (``2015-01-05T00:00:00.000000Z``);
  prices use the canonical decimal form (normalised, fixed-point, no exponent), so the scale
  a database returns (``1.19500`` vs ``1.195``) does not matter. No float is ever involved;
* ``rows_sha256`` hashes the row lines; the snapshot hash covers a versioned header that
  names the format, symbol, timeframe, source, range, row count and ``rows_sha256``.

Research code never reads raw candles. It receives a ``VerifiedDataset``, which only
``issue_verified_dataset`` can create, and only after recomputing the digest of the rows it
was handed and finding it identical to the snapshot record (row count, rows hash, hash).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from sentinel.domain.canonical import canonical
from sentinel.domain.market_data import Candle, Timeframe
from sentinel.domain.types import DomainError, require_utc

__all__ = [
    "DATASET_FORMAT",
    "DatasetDigest",
    "DatasetDigester",
    "DatasetError",
    "DatasetSpec",
    "QualityAttestation",
    "SnapshotRecord",
    "VerifiedDataset",
    "digest_candles",
    "format_ts",
    "issue_verified_dataset",
]

DATASET_FORMAT: Final = "sentinel-dataset/v1"
PASS: Final = "PASS"  # noqa: S105 - a quality verdict, not a credential
_SYMBOL = re.compile(r"^[A-Z]{3}_[A-Z]{3}$")
_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class DatasetError(DomainError):
    """A dataset or snapshot is invalid, inconsistent or unverifiable. Always fatal."""


def format_ts(ts: datetime) -> str:
    """UTC, microsecond precision, ``Z`` suffix. Locale-independent (numeric fields only)."""
    utc = require_utc(ts, field="ts").astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S.%f}Z"


def _dec(value: Decimal) -> str:
    text = canonical(value)
    if not isinstance(text, str):  # pragma: no cover - canonical() maps Decimal to str
        raise DatasetError(f"cannot serialise {value!r}")
    return text


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    symbol: str
    timeframe: Timeframe
    source: str
    start: datetime
    """Inclusive."""
    end: datetime
    """Exclusive."""

    def __post_init__(self) -> None:
        if not _SYMBOL.match(self.symbol):
            raise DatasetError(f"symbol must look like EUR_USD, got {self.symbol!r}")
        if not isinstance(self.timeframe, Timeframe):
            raise DatasetError("timeframe must be a Timeframe")
        if not _SOURCE.match(self.source):
            raise DatasetError(f"source must match {_SOURCE.pattern}, got {self.source!r}")
        object.__setattr__(self, "start", require_utc(self.start, field="start").astimezone(UTC))
        object.__setattr__(self, "end", require_utc(self.end, field="end").astimezone(UTC))
        if self.end <= self.start:
            raise DatasetError("a snapshot range [start, end) must not be empty")

    def header(self, rows: int, rows_sha256: str) -> str:
        return (
            f"{DATASET_FORMAT}\n"
            f"symbol={self.symbol}\n"
            f"timeframe={self.timeframe.value}\n"
            f"source={self.source}\n"
            f"from={format_ts(self.start)}\n"
            f"to={format_ts(self.end)}\n"
            f"rows={rows}\n"
            f"rows_sha256={rows_sha256}\n"
        )


@dataclass(frozen=True, slots=True)
class DatasetDigest:
    rows: int
    rows_sha256: str
    sha256: str


class DatasetDigester:
    """Streaming digest: constant memory, refuses any row that does not belong."""

    def __init__(self, spec: DatasetSpec) -> None:
        self._spec = spec
        self._hash = hashlib.sha256()
        self._rows = 0
        self._last: datetime | None = None
        self._done = False

    def add(self, c: Candle) -> None:
        if self._done:
            raise DatasetError("digest already finished")
        spec = self._spec
        if c.symbol != spec.symbol or c.timeframe is not spec.timeframe:
            raise DatasetError(
                f"row {c.symbol} {c.timeframe.value} does not belong to "
                f"{spec.symbol} {spec.timeframe.value}"
            )
        if not spec.start <= c.ts < spec.end:
            raise DatasetError(f"row at {format_ts(c.ts)} lies outside the snapshot range")
        if self._last is not None and c.ts <= self._last:
            raise DatasetError(f"rows are not strictly increasing at {format_ts(c.ts)}")
        line = "|".join(
            (
                format_ts(c.ts),
                _dec(c.bid.o),
                _dec(c.bid.h),
                _dec(c.bid.l),
                _dec(c.bid.c),
                _dec(c.ask.o),
                _dec(c.ask.h),
                _dec(c.ask.l),
                _dec(c.ask.c),
                str(c.tick_volume),
            )
        )
        self._hash.update(line.encode("ascii") + b"\n")
        self._rows += 1
        self._last = c.ts

    def finish(self) -> DatasetDigest:
        if self._done:
            raise DatasetError("digest already finished")
        self._done = True
        if self._rows == 0:
            raise DatasetError("a snapshot of an empty range is not allowed")
        rows_sha = self._hash.hexdigest()
        header = self._spec.header(self._rows, rows_sha)
        return DatasetDigest(
            self._rows, rows_sha, hashlib.sha256(header.encode("ascii")).hexdigest()
        )


def digest_candles(spec: DatasetSpec, candles: Iterable[Candle]) -> DatasetDigest:
    d = DatasetDigester(spec)
    for c in candles:
        d.add(c)
    return d.finish()


@dataclass(frozen=True, slots=True)
class QualityAttestation:
    """Evidence that the range passed the strict data-quality gates. PASS is the only verdict."""

    verdict: str
    report_sha256: str
    """SHA-256 of the quality report the verdict came from."""
    config_sha256: str
    """SHA-256 of the thresholds that report was judged against."""

    def __post_init__(self) -> None:
        if self.verdict != PASS:
            raise DatasetError(f"only a PASS quality verdict can be frozen, got {self.verdict!r}")
        for name in ("report_sha256", "config_sha256"):
            if not _HEX64.match(getattr(self, name)):
                raise DatasetError(f"{name} must be a lowercase SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    """A frozen snapshot as stored. Trusted only after ``issue_verified_dataset`` agrees."""

    snapshot_id: str
    spec: DatasetSpec
    row_count: int
    rows_sha256: str
    sha256: str
    quality: QualityAttestation
    created_at: datetime


_ISSUER_KEY: Final = object()


@dataclass(frozen=True, slots=True)
class VerifiedDataset:
    """Candles whose digest was recomputed and matched a frozen snapshot. Read-only."""

    record: SnapshotRecord
    candles: tuple[Candle, ...]
    _key: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._key is not _ISSUER_KEY:
            raise DatasetError(
                "a VerifiedDataset can only be issued by issue_verified_dataset after verification"
            )

    @property
    def spec(self) -> DatasetSpec:
        return self.record.spec

    @property
    def sha256(self) -> str:
        return self.record.sha256

    @property
    def snapshot_id(self) -> str:
        return self.record.snapshot_id

    def __len__(self) -> int:
        return len(self.candles)


def issue_verified_dataset(record: SnapshotRecord, candles: Iterable[Candle]) -> VerifiedDataset:
    """Recompute the digest of ``candles`` and issue a VerifiedDataset only on an exact match."""
    rows: list[Candle] = []
    digester = DatasetDigester(record.spec)
    for c in candles:
        digester.add(c)
        rows.append(c)
    digest = digester.finish()
    if digest.rows != record.row_count:
        raise DatasetError(
            f"snapshot {record.snapshot_id}: row count mismatch "
            f"(stored {record.row_count}, found {digest.rows})"
        )
    if digest.rows_sha256 != record.rows_sha256 or digest.sha256 != record.sha256:
        raise DatasetError(f"snapshot {record.snapshot_id}: hash mismatch")
    return VerifiedDataset(record, tuple(rows), _ISSUER_KEY)
