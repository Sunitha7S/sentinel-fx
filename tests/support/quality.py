"""Quality configurations for tests (never shipped; config/quality holds the real ones)."""

from __future__ import annotations

import tempfile
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from sentinel.config.quality_loader import (
    APPROVALS_DIRNAME,
    QualityConfigRegistry,
    load_quality_registry,
)
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


# ----------------------------------------------------------------------------- trusted registries


def config_yaml(config: QualityConfig) -> str:
    """The YAML file for ``config``; it parses back to the same identity."""
    data = {
        "version": config.version,
        "status": config.status,
        "engine": config.engine,
        "calendar": config.calendar,
        "report": {"max_listed": config.max_listed},
        "timeframes": {
            tf.value: {
                "min_completeness_pct": str(t.min_completeness_pct),
                "max_gap_bars": t.max_gap_bars,
                "max_unexpected_bars": t.max_unexpected_bars,
                "jump_flag_pct": str(t.jump_flag_pct),
                "range_flag_pct": str(t.range_flag_pct),
            }
            for tf, t in config.timeframes.items()
        },
        "symbols": {s: {"max_spread": str(t.max_spread)} for s, t in config.symbols.items()},
    }
    return yaml.safe_dump(data, sort_keys=False)


def approval_yaml(config: QualityConfig, **overrides: object) -> str:
    """An approval record for ``config``; ``overrides`` replace or (with ``None``) drop keys."""
    record: dict[str, object] = {
        "config_version": config.version,
        "config_sha256": config.sha256,
        "approved_by": "test-reviewer",
        "approved_on": date(2026, 1, 1),
        "evidence": "tests only; never shipped",
    }
    record.update(overrides)
    return yaml.safe_dump({k: v for k, v in record.items() if v is not None}, sort_keys=False)


def write_configs(
    directory: Path,
    *configs: QualityConfig,
    approve: Iterable[QualityConfig] | None = None,
) -> Path:
    """Write ``configs`` and approval records for ``approve`` (default: every APPROVED one)."""
    directory.mkdir(parents=True, exist_ok=True)
    for config in configs:
        (directory / f"{config.version}.yaml").write_text(config_yaml(config), encoding="utf-8")
    approved = [c for c in configs if c.status == APPROVED] if approve is None else list(approve)
    if approved:
        folder = directory / APPROVALS_DIRNAME
        folder.mkdir(exist_ok=True)
        for config in approved:
            (folder / f"{config.version}.yaml").write_text(approval_yaml(config), encoding="utf-8")
    return directory


def trusted_registry(*configs: QualityConfig) -> QualityConfigRegistry:
    """A registry loaded, the only way there is, from a temporary directory holding
    ``configs`` and an approval record for each APPROVED one."""
    with tempfile.TemporaryDirectory() as tmp:
        return load_quality_registry(write_configs(Path(tmp), *configs))
