"""Quality configurations for tests (never shipped; config/quality holds the real ones)."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from sentinel.domain.quality import APPROVED, QUALITY_CALENDAR, QUALITY_ENGINE, QualityConfig

SPREAD_CAPS = {"EUR_USD": "0.0100", "USD_JPY": "1.000"}


def quality_config(
    *,
    version: str = "qc_v900",
    status: str = APPROVED,
    min_completeness_pct: str = "99.0",
    max_gap_bars: int = 15,
    max_unexpected_bars: int = 0,
    jump_flag_pct: str = "0.30",
    range_flag_pct: str = "0.50",
    spreads: Mapping[str, str] = SPREAD_CAPS,
    max_listed: int = 5,
    timeframes: tuple[str, ...] = ("M1", "M5", "H1"),
) -> QualityConfig:
    return QualityConfig.from_mapping(
        {
            "version": version,
            "status": status,
            "engine": QUALITY_ENGINE,
            "calendar": QUALITY_CALENDAR,
            "report": {"max_listed": max_listed},
            "timeframes": {
                tf: {
                    "min_completeness_pct": Decimal(min_completeness_pct),
                    "max_gap_bars": max_gap_bars,
                    "max_unexpected_bars": max_unexpected_bars,
                    "jump_flag_pct": Decimal(jump_flag_pct),
                    "range_flag_pct": Decimal(range_flag_pct),
                }
                for tf in timeframes
            },
            "symbols": {s: {"max_spread": Decimal(v)} for s, v in spreads.items()},
        }
    )


def lenient_config(**overrides: object) -> QualityConfig:
    """Accepts gaps and sparse series; integrity gates still apply. For tests about something
    other than completeness (sealing, locking, tampering)."""
    params: dict[str, object] = {
        "version": "qc_v901",
        "min_completeness_pct": "0.001",
        "max_gap_bars": 1_000_000,
        "max_unexpected_bars": 1_000_000,
    }
    params.update(overrides)
    return quality_config(**params)  # type: ignore[arg-type]
