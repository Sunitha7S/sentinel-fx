"""Dataset digest format v1, VerifiedDataset issuance (INV-DATA-SNAPSHOT, ADR 0014).

The golden test spells out the exact bytes of format v1 by hand, independently of the
implementation, so any change to the format (or to its normalisation) fails here.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sentinel.domain.dataset import (
    DATASET_FORMAT,
    DatasetDigester,
    DatasetError,
    DatasetSpec,
    QualityProvenance,
    SnapshotRecord,
    VerifiedDataset,
    attest,
    digest_candles,
    issue_verified_dataset,
    judge_dataset,
)
from sentinel.domain.market_data import OHLC, Candle, Timeframe
from sentinel.domain.types import DomainError

from support.market import candle, minutes
from support.quality import lenient_config, quality_config

MON = datetime(2015, 1, 5, tzinfo=UTC)
SPEC = DatasetSpec("EUR_USD", Timeframe.M1, "oanda-practice", MON, MON + timedelta(minutes=3))
HEX = "a" * 64
CFG = lenient_config()
AS_OF = datetime(2020, 1, 1, tzinfo=UTC)


def _c(ts: datetime, bid: tuple[str, ...], ask: tuple[str, ...], vol: int) -> Candle:
    return Candle(
        "EUR_USD",
        Timeframe.M1,
        ts,
        OHLC(*(Decimal(v) for v in bid)),
        OHLC(*(Decimal(v) for v in ask)),
        vol,
    )


GOLDEN = [
    _c(
        MON,
        ("1.19500", "1.19530", "1.19480", "1.19510"),
        ("1.19520", "1.19550", "1.19500", "1.19530"),
        12,
    ),
    _c(
        MON + timedelta(minutes=1),
        ("1.2", "1.2", "1.2", "1.2"),
        ("1.20010", "1.2001", "1.20010", "1.2001"),
        0,
    ),
]


def _record(spec: DatasetSpec = SPEC, rows: list[Candle] = GOLDEN) -> SnapshotRecord:
    """A record exactly as freeze() would store it: digest and quality judged together."""
    a = attest(judge_dataset(spec, CFG, AS_OF, rows))
    return SnapshotRecord(
        snapshot_id="00000000-0000-0000-0000-000000000001",
        spec=spec,
        row_count=a.rows,
        rows_sha256=a.rows_sha256,
        sha256=a.sha256,
        quality=a.provenance,
        created_at=AS_OF,
    )


# ----------------------------------------------------------------------------- format v1


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
def test_golden_vector_matches_the_hand_written_format() -> None:
    rows = (
        "2015-01-05T00:00:00.000000Z|1.195|1.1953|1.1948|1.1951|1.1952|1.1955|1.195|1.1953|12\n"
        "2015-01-05T00:01:00.000000Z|1.2|1.2|1.2|1.2|1.2001|1.2001|1.2001|1.2001|0\n"
    )
    rows_sha = hashlib.sha256(rows.encode("ascii")).hexdigest()
    header = (
        "sentinel-dataset/v1\n"
        "symbol=EUR_USD\n"
        "timeframe=M1\n"
        "source=oanda-practice\n"
        "from=2015-01-05T00:00:00.000000Z\n"
        "to=2015-01-05T00:03:00.000000Z\n"
        "rows=2\n"
        f"rows_sha256={rows_sha}\n"
    )
    digest = digest_candles(SPEC, GOLDEN)
    assert DATASET_FORMAT == "sentinel-dataset/v1"
    assert digest.rows == 2
    assert digest.rows_sha256 == rows_sha
    assert digest.sha256 == hashlib.sha256(header.encode("ascii")).hexdigest()
    # Pinned: the same bytes on every machine, OS, Python version and database.
    assert digest.sha256 == "24589c973456e436f1678efe484b1f4a0f36e22b3dfc5317fa8c5cefec19039a"


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
def test_decimal_scale_and_timestamp_zone_do_not_change_the_digest() -> None:
    """Numerics come back at the scale they were stored; non-UTC ranges are refused outright."""
    q = Decimal("0.0000001")
    rescaled = [
        replace(c, bid=OHLC(*(x.quantize(q) for x in (c.bid.o, c.bid.h, c.bid.l, c.bid.c))))
        for c in GOLDEN
    ]
    assert digest_candles(SPEC, rescaled) == digest_candles(SPEC, GOLDEN)
    tokyo = ZoneInfo("Asia/Tokyo")
    with pytest.raises(DomainError, match="UTC"):
        replace(SPEC, start=SPEC.start.astimezone(tokyo), end=SPEC.end.astimezone(tokyo))


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
def test_every_field_and_the_header_participate() -> None:
    base = digest_candles(SPEC, GOLDEN).sha256
    one = GOLDEN[0]
    changed = [
        replace(one, tick_volume=13),
        replace(one, bid=replace(one.bid, c=Decimal("1.19511"))),
        replace(one, ask=replace(one.ask, h=Decimal("1.19560"))),
        replace(one, ts=MON + timedelta(seconds=30)),
    ]
    for variant in changed:
        assert digest_candles(SPEC, [variant, GOLDEN[1]]).sha256 != base
    for spec in (
        replace(SPEC, source="dukascopy"),
        replace(SPEC, end=SPEC.end + timedelta(minutes=1)),
        replace(SPEC, start=SPEC.start - timedelta(minutes=1)),
    ):
        assert digest_candles(spec, GOLDEN).sha256 != base
    assert digest_candles(SPEC, GOLDEN[:1]).sha256 != base


@given(st.lists(st.integers(min_value=0, max_value=10_000), min_size=1, max_size=40, unique=True))
def test_digest_is_a_pure_function_of_the_rows(offsets: list[int]) -> None:
    spec = replace(SPEC, end=MON + timedelta(minutes=10_001))
    bars = [candle("EUR_USD", MON + timedelta(minutes=m)) for m in sorted(offsets)]
    assert digest_candles(spec, bars) == digest_candles(spec, list(bars))
    assert digest_candles(spec, bars).rows == len(bars)


# ----------------------------------------------------------------------------- fail closed


@pytest.mark.parametrize(
    "bad",
    [
        [GOLDEN[1], GOLDEN[0]],  # out of order
        [GOLDEN[0], GOLDEN[0]],  # duplicate
        [replace(GOLDEN[0], ts=MON - timedelta(minutes=1))],  # before the range
        [replace(GOLDEN[0], ts=SPEC.end)],  # at the (exclusive) end
        [candle("USD_JPY", MON, mid="150.00")],  # other symbol
        [candle("EUR_USD", MON, tf=Timeframe.H1)],  # other timeframe
    ],
)
def test_digester_refuses_rows_that_do_not_belong(bad: list[Candle]) -> None:
    with pytest.raises(DatasetError):
        digest_candles(SPEC, bad)


def test_digest_of_an_empty_range_is_refused() -> None:
    with pytest.raises(DatasetError, match="empty"):
        digest_candles(SPEC, [])


def test_digester_cannot_be_reused_after_finishing() -> None:
    d = DatasetDigester(SPEC)
    d.add(GOLDEN[0])
    d.finish()
    with pytest.raises(DatasetError):
        d.add(GOLDEN[1])
    with pytest.raises(DatasetError):
        d.finish()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"end": MON},  # empty range
        {"end": MON - timedelta(minutes=1)},
        {"start": datetime(2015, 1, 5)},  # noqa: DTZ001 - naive on purpose
        {"source": ""},
        {"source": "oanda\nrows=0"},  # header injection
        {"source": "a|b"},
        {"symbol": "EURUSD"},
    ],
)
def test_spec_is_validated(kwargs: dict[str, object]) -> None:
    with pytest.raises((DatasetError, DomainError)):
        replace(SPEC, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("verdict", ["FAIL", "WARNING", "DEGRADED", "SOFT_FAIL", "pass", ""])
def test_a_record_can_only_name_a_pass_verdict(verdict: str) -> None:
    with pytest.raises(DatasetError, match="PASS"):
        QualityProvenance(verdict, HEX, HEX)


def test_provenance_digests_must_be_sha256_hex() -> None:
    with pytest.raises(DatasetError):
        QualityProvenance("PASS", "not-a-hash", HEX)


# ----------------------------------------------------------------------------- VerifiedDataset


@pytest.mark.invariant("INV-DATA-VERIFIED-ONLY")
def test_a_verified_dataset_cannot_be_constructed_directly() -> None:
    with pytest.raises(DatasetError, match="only"):
        VerifiedDataset(_record(), tuple(GOLDEN), object())


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
def test_issuance_recomputes_and_matches_the_record() -> None:
    d = digest_candles(SPEC, GOLDEN)
    ds = issue_verified_dataset(_record(), GOLDEN, CFG)
    assert ds.sha256 == d.sha256
    assert ds.candles == tuple(GOLDEN)
    assert ds.spec == SPEC
    assert len(ds) == 2


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("sha256", "b" * 64, "hash mismatch"),
        ("rows_sha256", "b" * 64, "hash mismatch"),
        ("row_count", 3, "row count mismatch"),
    ],
)
def test_issuance_refuses_any_mismatch(field: str, value: object, match: str) -> None:
    record = replace(_record(), **{field: value})  # type: ignore[arg-type]
    with pytest.raises(DatasetError, match=match):
        issue_verified_dataset(record, GOLDEN, CFG)


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
def test_issuance_refuses_missing_extra_or_foreign_rows() -> None:
    record = _record()
    with pytest.raises(DatasetError):
        issue_verified_dataset(record, GOLDEN[:1], CFG)  # missing row
    extra = _c(MON + timedelta(minutes=2), ("1.2",) * 4, ("1.2001",) * 4, 1)
    with pytest.raises(DatasetError):
        issue_verified_dataset(record, [*GOLDEN, extra], CFG)
    with pytest.raises(DatasetError):
        issue_verified_dataset(replace(record, spec=replace(SPEC, source="other")), GOLDEN, CFG)


def test_verified_dataset_is_immutable() -> None:
    ds = issue_verified_dataset(_record(), GOLDEN, CFG)
    with pytest.raises(AttributeError):
        ds.candles = ()  # type: ignore[misc]
    assert isinstance(ds.candles, tuple)


# ----------------------------------------------------------------------------- benchmark


def test_hash_generation_throughput(capsys: pytest.CaptureFixture[str]) -> None:
    n = 100_000
    spec = replace(SPEC, end=MON + timedelta(minutes=n))
    bars = [candle("EUR_USD", t) for t in minutes(MON, n)]
    started = time.perf_counter()
    digest_candles(spec, bars)
    elapsed = time.perf_counter() - started
    with capsys.disabled():
        import sys

        sys.stdout.write(
            f"\n[bench] dataset digest v1: {n:,} candles in {elapsed:.2f}s "
            f"({n / elapsed:,.0f} candles/s)\n"
        )
    assert elapsed < 60


# ----------------------------------------------------------------------------- quality on issuance


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
def test_issuance_requires_the_snapshots_own_quality_configuration() -> None:
    record = _record()
    other = lenient_config(max_gap_bars=999)  # also lenient, also PASS, but not the same config
    with pytest.raises(DatasetError, match="quality configuration mismatch"):
        issue_verified_dataset(record, GOLDEN, other)
    with pytest.raises(DatasetError, match="quality configuration mismatch"):
        issue_verified_dataset(record, GOLDEN, quality_config())


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
@pytest.mark.parametrize(
    "tamper",
    [
        lambda r: replace(r, quality=QualityProvenance("PASS", HEX, r.quality.config_sha256)),
        lambda r: replace(r, created_at=r.created_at + timedelta(seconds=1)),
    ],
    ids=["report hash", "evaluation time"],
)
def test_issuance_refuses_a_quality_result_it_cannot_reproduce(tamper: object) -> None:
    record = tamper(_record())  # type: ignore[operator]
    with pytest.raises(DatasetError, match="quality report mismatch"):
        issue_verified_dataset(record, GOLDEN, CFG)
