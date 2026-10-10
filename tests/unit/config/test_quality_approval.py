"""Approval is bound to an exact configuration identity and a human approval record (ADR 0015).

INV-DATA-QUALITY-GATED: a configuration cannot carry a hash that does not match its content
(V1: ``replace(qc_v1, status=APPROVED)``; V2: a directly built configuration with a forged
hash), threshold objects validate themselves, and only a registry loaded from configuration
files with exactly one matching approval record per APPROVED configuration can hand the
snapshot store a configuration to freeze or load with. A correctly hashed configuration
built in memory gains no trust by being passed to the registry or the store.
"""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from sentinel.config.quality_loader import (
    APPROVALS_DIRNAME,
    QUALITY_CONFIG_DIR,
    ApprovalRecord,
    QualityConfigRegistry,
    load_quality_config,
    load_quality_registry,
    parse_approval_yaml,
    parse_quality_yaml,
)
from sentinel.domain.dataset import DatasetError, DatasetSpec, attest, judge_dataset
from sentinel.domain.market_data import Timeframe
from sentinel.domain.quality import (
    APPROVED,
    PROVISIONAL,
    QualityConfig,
    QualityConfigError,
    SymbolThresholds,
)
from sentinel.store.postgres.dataset_store import PostgresDatasetStore

from support.market import candle, minutes
from support.quality import (
    approval_yaml,
    config_yaml,
    lenient_config,
    quality_config,
    trusted_registry,
    write_configs,
)

QC_V1 = load_quality_config(QUALITY_CONFIG_DIR / "qc_v1.yaml")
QC_V1_SHA256 = "96163e820dd54ed14b144de11d54d06d383ac745c7b264919af19ee3353bf27b"
MON = datetime(2015, 1, 5, tzinfo=UTC)
SPEC = DatasetSpec("EUR_USD", Timeframe.M1, "oanda-practice", MON, MON + timedelta(minutes=30))
BARS = [candle("EUR_USD", t) for t in minutes(MON, 30)]


def _fields(config: QualityConfig) -> dict[str, Any]:
    return {f.name: getattr(config, f.name) for f in dataclasses.fields(config)}


def _approved_twin(config: QualityConfig) -> QualityConfig:
    """``config`` with status APPROVED and its correctly recomputed hash, built in memory."""
    text = config_yaml(config).replace(f"status: {config.status}", f"status: {APPROVED}", 1)
    honest = parse_quality_yaml(text)
    assert honest.status == APPROVED
    return replace(config, status=APPROVED, sha256=honest.sha256)


# ----------------------------------------------------------------------------- V1, V2: identity


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_v1_replacing_the_status_of_qc_v1_is_refused() -> None:
    assert QC_V1.status == PROVISIONAL
    assert QC_V1.sha256 == QC_V1_SHA256
    with pytest.raises(QualityConfigError, match="identity mismatch"):
        replace(QC_V1, status=APPROVED)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_v2_a_directly_built_configuration_with_a_forged_hash_is_refused() -> None:
    fields = _fields(quality_config())
    for forged in ("f" * 64, QC_V1_SHA256, "", fields["sha256"].upper()):
        with pytest.raises(QualityConfigError, match="identity mismatch"):
            QualityConfig(**{**fields, "sha256": forged})
    with pytest.raises(QualityConfigError, match="identity mismatch"):
        QualityConfig(**{**fields, "sha256": None})
    assert QualityConfig(**fields) == quality_config()  # the honest hash builds


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_no_field_can_change_while_keeping_the_old_identity() -> None:
    cfg = quality_config()
    m1 = cfg.timeframes[Timeframe.M1]
    changes: list[dict[str, Any]] = [
        {"status": PROVISIONAL},
        {"version": "qc_v902"},
        {"max_listed": 6},
        {"sha256": "0" * 64},
        {"timeframes": {**cfg.timeframes, Timeframe.M1: replace(m1, max_gap_bars=16)}},
        {"timeframes": {**cfg.timeframes, Timeframe.M1: replace(m1, jump_flag_pct=Decimal(1))}},
        {"timeframes": {Timeframe.M1: m1}},  # a timeframe removed
        {"symbols": {**cfg.symbols, "EUR_USD": SymbolThresholds(Decimal("0.0200"))}},
        {"symbols": {**cfg.symbols, "GBP_USD": SymbolThresholds(Decimal("0.0100"))}},
    ]
    for change in changes:
        with pytest.raises(QualityConfigError, match="identity mismatch"):
            replace(cfg, **change)
    for change, match in (
        ({"engine": "sentinel-quality/v2"}, "engine"),
        ({"calendar": "other/v1"}, "calendar"),
        ({"version": "v1"}, "version"),
        ({"status": "TRUSTED"}, "status"),
        ({"max_listed": 0}, "max_listed"),
        ({"timeframes": {}}, "at least one"),
        ({"timeframes": {"M1": m1}}, "unknown timeframe"),
        ({"timeframes": {Timeframe.H4: m1}}, "fixed-grid"),
        ({"timeframes": {Timeframe.M1: {"max_gap_bars": 1}}}, "TimeframeThresholds"),
        ({"symbols": {"EURUSD": cfg.symbols["EUR_USD"]}}, "symbol"),
        ({"symbols": {"EUR_USD": m1}}, "SymbolThresholds"),
    ):
        with pytest.raises(QualityConfigError, match=match):
            replace(cfg, **change)


def test_a_configuration_keeps_private_read_only_copies_of_its_thresholds() -> None:
    fields = _fields(quality_config())
    timeframes = dict(fields["timeframes"])
    cfg = QualityConfig(**{**fields, "timeframes": timeframes})
    timeframes.clear()  # the caller's dict changes; the configuration does not
    assert set(cfg.timeframes) == {Timeframe.M1, Timeframe.M5, Timeframe.H1}
    assert isinstance(cfg.timeframes, MappingProxyType)
    assert isinstance(cfg.symbols, MappingProxyType)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_directly_built_thresholds_are_validated() -> None:
    good = quality_config().timeframes[Timeframe.M1]
    for change, match in (
        ({"min_completeness_pct": Decimal("100.1")}, "completeness"),
        ({"min_completeness_pct": Decimal(0)}, "completeness"),
        ({"min_completeness_pct": Decimal("NaN")}, "finite"),
        ({"min_completeness_pct": 99.0}, "Decimal"),
        ({"min_completeness_pct": "99.0"}, "Decimal"),
        ({"max_gap_bars": -1}, "max_gap_bars"),
        ({"max_gap_bars": True}, "integer"),
        ({"max_unexpected_bars": Decimal(1)}, "integer"),
        ({"jump_flag_pct": Decimal(0)}, "positive"),
        ({"range_flag_pct": Decimal("-0.5")}, "positive"),
        ({"range_flag_pct": Decimal("Infinity")}, "finite"),
    ):
        with pytest.raises(QualityConfigError, match=match):
            replace(good, **change)
    for bad in (Decimal(0), Decimal("-0.01"), 0.01, "0.01", Decimal("sNaN")):
        with pytest.raises(QualityConfigError):
            SymbolThresholds(bad)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- approval records


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_the_shipped_registry_holds_qc_v1_as_provisional_and_approves_nothing() -> None:
    registry = load_quality_registry()
    assert not (QUALITY_CONFIG_DIR / APPROVALS_DIRNAME).exists()  # no approval is shipped
    assert registry.resolve(QC_V1_SHA256) == QC_V1
    assert registry.resolve(QC_V1_SHA256).status == PROVISIONAL
    with pytest.raises(QualityConfigError, match="PROVISIONAL_UNCALIBRATED"):
        registry.approved(QC_V1_SHA256)
    with pytest.raises(QualityConfigError, match="not available"):
        registry.approved(_approved_twin(QC_V1).sha256)  # an in-memory approval is unknown


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_an_approved_configuration_with_its_record_is_trusted(tmp_path: Path) -> None:
    cfg, provisional = quality_config(), lenient_config(status=PROVISIONAL)
    registry = load_quality_registry(write_configs(tmp_path, cfg, provisional))
    trusted = registry.approved(cfg.sha256)
    assert trusted == cfg
    assert trusted is registry.approved(cfg.sha256)  # always the registry's own object
    assert trusted is not cfg
    report = judge_dataset(SPEC, trusted, MON + timedelta(days=1), BARS)
    assert attest(report).config_sha256 == cfg.sha256
    with pytest.raises(QualityConfigError, match="PROVISIONAL_UNCALIBRATED"):
        registry.approved(provisional.sha256)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_an_approved_configuration_without_a_record_refuses_the_registry(tmp_path: Path) -> None:
    write_configs(tmp_path, quality_config(), lenient_config(), approve=[lenient_config()])
    with pytest.raises(QualityConfigError, match="qc_v900 is APPROVED but has no approval record"):
        load_quality_registry(tmp_path)


def _registry_with_record(tmp_path: Path, record: str, *configs: QualityConfig) -> None:
    write_configs(tmp_path, *(configs or (quality_config(),)), approve=[])
    folder = tmp_path / APPROVALS_DIRNAME
    folder.mkdir(exist_ok=True)
    (folder / "record.yaml").write_text(record, encoding="utf-8")
    load_quality_registry(tmp_path)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_unknown_stale_provisional_and_mismatched_records_refuse_the_registry(
    tmp_path: Path,
) -> None:
    cfg = quality_config()
    provisional = quality_config(version="qc_v903", status=PROVISIONAL)
    before_edit = quality_config(max_gap_bars=14)  # same version, an older threshold
    cases = [
        (approval_yaml(cfg, config_sha256="e" * 64), "matches no shipped configuration"),
        (approval_yaml(before_edit), "stale or unknown"),
        (approval_yaml(provisional), "PROVISIONAL_UNCALIBRATED, not APPROVED"),
        (approval_yaml(cfg, config_version="qc_v901"), "names version qc_v901"),
    ]
    for i, (record, match) in enumerate(cases):
        with pytest.raises(QualityConfigError, match=match):
            _registry_with_record(tmp_path / str(i), record, cfg, provisional)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_two_records_for_one_configuration_refuse_the_registry(tmp_path: Path) -> None:
    cfg = quality_config()
    write_configs(tmp_path, cfg)
    (tmp_path / APPROVALS_DIRNAME / "again.yaml").write_text(
        approval_yaml(cfg, approved_by="someone-else"), encoding="utf-8"
    )
    with pytest.raises(QualityConfigError, match="approved twice"):
        load_quality_registry(tmp_path)


VALID = approval_yaml(quality_config())


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
@pytest.mark.parametrize(
    ("record", "match"),
    [
        (VALID + "extra: 1\n", "unknown key"),
        (approval_yaml(quality_config(), evidence=None), "missing key"),
        (VALID + "approved_by: someone-else\n", "duplicate key"),
        (approval_yaml(quality_config(), approved_by=""), "approved_by"),
        (approval_yaml(quality_config(), approved_by=7), "approved_by"),
        (approval_yaml(quality_config(), evidence="   "), "evidence"),
        (approval_yaml(quality_config(), config_sha256="F" * 64), "config_sha256"),
        (approval_yaml(quality_config(), config_sha256="abc"), "config_sha256"),
        (approval_yaml(quality_config(), approved_on="yesterday"), "approved_on"),
        (approval_yaml(quality_config(), approved_on=datetime(2026, 1, 1, tzinfo=UTC)), "type"),
        ("config_version: 1.5\n" + VALID.split("\n", 1)[1], "wrong type"),
        ("- not\n- a mapping\n", "mapping"),
        ("config_version: [", "invalid YAML"),
    ],
)
def test_malformed_records_refuse_the_registry(tmp_path: Path, record: str, match: str) -> None:
    with pytest.raises(QualityConfigError, match=match):
        _registry_with_record(tmp_path, record)


def test_a_record_is_strict_data() -> None:
    record = parse_approval_yaml(VALID)
    assert isinstance(record, ApprovalRecord)
    assert record.approved_on == date(2026, 1, 1)
    assert record.config_sha256 == quality_config().sha256
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.config_sha256 = "e" * 64  # type: ignore[misc]
    with pytest.raises(QualityConfigError, match="config_sha256"):
        replace(record, config_sha256="nope")


def test_an_approvals_path_that_is_not_a_directory_refuses_the_registry(tmp_path: Path) -> None:
    write_configs(tmp_path, lenient_config(status=PROVISIONAL))
    (tmp_path / APPROVALS_DIRNAME).write_text("", encoding="utf-8")
    with pytest.raises(QualityConfigError, match="not a directory"):
        load_quality_registry(tmp_path)


def test_generated_config_files_round_trip_to_the_same_identity(tmp_path: Path) -> None:
    for cfg in (quality_config(), lenient_config(), QC_V1):
        path = tmp_path / f"{cfg.version}.yaml"
        path.write_text(config_yaml(cfg), encoding="utf-8")
        assert load_quality_config(path) == cfg


# ----------------------------------------------------------------------------- the registry itself


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_a_registry_cannot_be_built_copied_or_changed_outside_the_loader() -> None:
    cfg = quality_config()
    with pytest.raises(QualityConfigError, match="load_quality_registry"):
        QualityConfigRegistry({cfg.sha256: cfg}, frozenset({cfg.sha256}), _key=object())
    registry = trusted_registry(cfg)
    with pytest.raises(TypeError):
        replace(registry, configs={})  # type: ignore[type-var]
    for name in ("_approved", "_configs", "configs"):
        with pytest.raises(AttributeError):
            setattr(registry, name, frozenset({QC_V1_SHA256}))
    with pytest.raises(AttributeError):
        del registry._approved
    with pytest.raises(TypeError):
        registry.configs[QC_V1_SHA256] = QC_V1  # type: ignore[index]


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_a_correctly_hashed_configuration_built_in_memory_gains_no_trust() -> None:
    registry = trusted_registry(quality_config())
    twin = _approved_twin(QC_V1)  # honest content, honest hash, APPROVED, never reviewed
    assert twin.status == APPROVED
    with pytest.raises(QualityConfigError, match="not available"):
        registry.approved(twin.sha256)
    with pytest.raises(QualityConfigError, match="not available"):
        trusted_registry(quality_config()).approved(lenient_config().sha256)


# ----------------------------------------------------------------------- the store, no database


class _NoDatabase:
    """Any use of the engine fails the test: refusals must come before the database."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the database was touched ({name})")


def _store(registry: QualityConfigRegistry | None) -> PostgresDatasetStore:
    return PostgresDatasetStore(_NoDatabase(), registry=registry)  # type: ignore[arg-type]


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_freeze_and_load_refuse_without_a_trusted_registry() -> None:
    store = _store(None)
    with pytest.raises(DatasetError, match="no trusted quality registry"):
        store.freeze(SPEC, quality_config())
    with pytest.raises(DatasetError, match="no trusted quality registry"):
        store.load("00000000-0000-0000-0000-000000000001")
    cfg = quality_config()
    for fake in ({cfg.sha256: cfg}, object()):
        with pytest.raises(DatasetError, match="QualityConfigRegistry"):
            _store(fake)  # type: ignore[arg-type]


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_freeze_refuses_untrusted_configurations_before_touching_the_database() -> None:
    cfg = quality_config()
    registry = trusted_registry(cfg, lenient_config(status=PROVISIONAL))
    store = _store(registry)
    trusted = registry.approved(cfg.sha256)
    for quality, match in (
        (QC_V1, "not available"),  # provisional and not in this registry
        (registry.resolve(lenient_config(status=PROVISIONAL).sha256), "PROVISIONAL"),
        (_approved_twin(QC_V1), "not available"),  # rehashed in memory
        (quality_config(max_gap_bars=16), "not available"),  # honest, APPROVED, untrusted
        (cfg, "not obtained from the trusted registry"),  # same content, not the trusted object
        (replace(trusted, max_listed=trusted.max_listed), "not obtained from the trusted"),
    ):
        with pytest.raises(DatasetError, match=match):
            store.freeze(SPEC, quality)
    with pytest.raises(DatasetError, match="quality configuration is required"):
        store.freeze(SPEC, None)  # type: ignore[arg-type]
