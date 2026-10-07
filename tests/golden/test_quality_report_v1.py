"""Golden quality reports for engine v1 (sentinel-quality/v1, fx-ny1700-xmas-newyear/v1).

Snapshots record the hash of their quality report, and verified load must reproduce it
byte for byte. So the engine's output is part of every snapshot's identity: any change to
a gate, the calendar, number formatting, ordering or the report layout would make every
existing snapshot unloadable. These tests pin the exact canonical JSON and its hash for a
fixed dataset and a fixed configuration written out here (not shared with other tests).

If one fails, do not update the expected value to make it pass: either the change was
unintended (revert it), or it is a new engine version (keep v1 working and add v2).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sentinel.domain.dataset import DatasetSpec, judge_dataset
from sentinel.domain.market_data import OHLC, Candle, Timeframe
from sentinel.domain.quality import QualityConfig

GOLDEN = Path(__file__).resolve().parent / "quality_report_v1"
MON = datetime(2015, 1, 5, tzinfo=UTC)  # Sunday 19:00 New York
AS_OF = datetime(2015, 1, 6, 12, 0, tzinfo=UTC)
CONFIG = QualityConfig.from_mapping(
    {
        "version": "qc_v1",
        "status": "APPROVED",
        "engine": "sentinel-quality/v1",
        "calendar": "fx-ny1700-xmas-newyear/v1",
        "report": {"max_listed": 3},
        "timeframes": {
            "M1": {
                "min_completeness_pct": Decimal("99.0"),
                "max_gap_bars": 15,
                "max_unexpected_bars": 0,
                "jump_flag_pct": Decimal("0.30"),
                "range_flag_pct": Decimal("0.50"),
            }
        },
        "symbols": {"EUR_USD": {"max_spread": Decimal("0.0100")}},
    }
)


def _bar(i: int, mid: str = "1.10000", spread: str = "0.00010", vol: int = 42) -> Candle:
    m, s, p = Decimal(mid), Decimal(spread), Decimal("0.00010")
    bid = OHLC(m, m + 2 * p, m - 2 * p, m + p)
    ask = OHLC(m + s, m + 2 * p + s, m - 2 * p + s, m + p + s)
    return Candle("EUR_USD", Timeframe.M1, MON + timedelta(minutes=i), bid, ask, vol)


def _clean() -> list[Candle]:
    return [_bar(i) for i in range(60)]


def _defective() -> list[Candle]:
    """120 minutes with a 20-minute hole, a zero-volume bar, two jumps (up and back), a wide
    bar and a spread above the cap: one FAIL per series gate kind plus every flag."""
    bars = []
    for i in range(120):
        if 30 <= i < 50:
            continue
        if i == 60:
            bars.append(_bar(i, vol=0))
        elif i == 70:
            bars.append(_bar(i, mid="1.10600"))
        elif i == 80:
            b = _bar(i)
            wide = Decimal("0.00800")
            bars.append(
                replace(
                    b, bid=replace(b.bid, h=b.bid.h + wide), ask=replace(b.ask, h=b.ask.h + wide)
                )
            )
        elif i == 90:
            bars.append(_bar(i, spread="0.01500"))
        else:
            bars.append(_bar(i))
    return bars


CASES = {
    "pass": (60, _clean, "PASS"),
    "fail": (120, _defective, "FAIL"),
}
PINNED = {
    "pass": "914b57de55c34658f1df2f2325d5c45c33ac7b0ddd41d667dfd910c45d57b0fa",
    "fail": "15ada9accbd4dcc5d0464b6aff0003842ba9f5bbabf81611cbe08b6fdb7dbb9d",
}


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
@pytest.mark.parametrize("case", sorted(CASES))
def test_engine_v1_output_is_pinned(case: str) -> None:
    minutes, rows, verdict = CASES[case]
    spec = DatasetSpec(
        "EUR_USD", Timeframe.M1, "oanda-practice", MON, MON + timedelta(minutes=minutes)
    )
    report = judge_dataset(spec, CONFIG, AS_OF, rows())
    assert report.verdict == verdict
    expected = (GOLDEN / f"{case}.json").read_text(encoding="ascii")
    assert report.canonical_json == expected.rstrip("\n")
    assert report.sha256 == hashlib.sha256(expected.rstrip("\n").encode("ascii")).hexdigest()
    assert report.sha256 == PINNED[case]


def test_golden_files_are_canonical_json() -> None:
    for case in CASES:
        text = (GOLDEN / f"{case}.json").read_text(encoding="ascii").rstrip("\n")
        assert json.dumps(json.loads(text), sort_keys=True, separators=(",", ":")) == text
