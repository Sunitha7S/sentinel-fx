"""Quality configuration: strict schema, Decimal-only thresholds, semantic hash (qc_v1)."""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from sentinel.cli import main
from sentinel.config.quality_loader import (
    QUALITY_CONFIG_DIR,
    load_quality_config,
    load_quality_registry,
    parse_quality_yaml,
)
from sentinel.domain.market_data import Timeframe
from sentinel.domain.quality import (
    APPROVED,
    PROVISIONAL,
    QUALITY_CALENDAR,
    QUALITY_ENGINE,
    QualityConfig,
    QualityConfigError,
)

QC_V1 = QUALITY_CONFIG_DIR / "qc_v1.yaml"
TEXT = QC_V1.read_text(encoding="utf-8")


def _mapping() -> dict[str, Any]:
    return {
        "version": "qc_v9",
        "status": PROVISIONAL,
        "engine": QUALITY_ENGINE,
        "calendar": QUALITY_CALENDAR,
        "report": {"max_listed": 5},
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


# ----------------------------------------------------------------------------- qc_v1


def test_qc_v1_is_provisional_and_covers_the_ingested_series() -> None:
    cfg = load_quality_config(QC_V1)
    assert cfg.version == "qc_v1"
    assert cfg.status == PROVISIONAL  # not trusted until reviewed against real data
    assert cfg.engine == QUALITY_ENGINE
    assert cfg.calendar == QUALITY_CALENDAR
    assert set(cfg.timeframes) == {Timeframe.M1, Timeframe.M5, Timeframe.H1}
    assert set(cfg.symbols) == {"EUR_USD", "USD_JPY"}
    assert "PROVISIONAL / UNCALIBRATED" in TEXT


def test_every_threshold_in_qc_v1_is_documented() -> None:
    for key in (
        "min_completeness_pct",
        "max_gap_bars",
        "max_unexpected_bars",
        "jump_flag_pct",
        "range_flag_pct",
        "max_spread",
        "max_listed",
    ):
        block = TEXT.split(f"# {key}:", 1)
        assert len(block) == 2, f"{key} has no explanation block"
        explanation = block[1].split("\n#\n", 1)[0]
        for heading in ("unit:", "detects:", "false positives:", "scope:"):
            assert heading in explanation, f"{key}: missing {heading!r}"


def test_registry_resolves_every_shipped_config_by_hash() -> None:
    registry = load_quality_registry()
    cfg = load_quality_config(QC_V1)
    assert registry.resolve(cfg.sha256) == cfg
    assert registry.by_version("qc_v1") == cfg
    with pytest.raises(QualityConfigError, match="not available"):
        registry.resolve("0" * 64)
    with pytest.raises(QualityConfigError, match="not available"):
        registry.by_version("qc_v99")


# ----------------------------------------------------------------------------- semantic hash


def test_hash_ignores_line_endings_comments_key_order_and_decimal_scale() -> None:
    base = parse_quality_yaml(TEXT).sha256
    assert parse_quality_yaml(TEXT.replace("\n", "\r\n")).sha256 == base
    stripped = "\n".join(line for line in TEXT.splitlines() if not line.lstrip().startswith("#"))
    assert parse_quality_yaml(stripped).sha256 == base
    assert "min_completeness_pct: 99.0" in TEXT
    rescaled = TEXT.replace("min_completeness_pct: 99.0", "min_completeness_pct: 99.000")
    assert parse_quality_yaml(rescaled).sha256 == base
    a = QualityConfig.from_mapping(_mapping())
    reordered = dict(reversed(list(_mapping().items())))
    assert QualityConfig.from_mapping(reordered).sha256 == a.sha256


def test_hash_covers_version_status_and_every_threshold() -> None:
    base = QualityConfig.from_mapping(_mapping()).sha256
    variants: list[dict[str, Any]] = []
    for path, value in (
        (("version",), "qc_v10"),
        (("status",), APPROVED),
        (("report", "max_listed"), 6),
        (("timeframes", "M1", "min_completeness_pct"), Decimal("99.1")),
        (("timeframes", "M1", "max_gap_bars"), 16),
        (("timeframes", "M1", "max_unexpected_bars"), 1),
        (("timeframes", "M1", "jump_flag_pct"), Decimal("0.31")),
        (("timeframes", "M1", "range_flag_pct"), Decimal("0.51")),
        (("symbols", "EUR_USD", "max_spread"), Decimal("0.0101")),
    ):
        m = _mapping()
        target = m
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        variants.append(m)
    hashes = {QualityConfig.from_mapping(m).sha256 for m in variants}
    assert base not in hashes
    assert len(hashes) == len(variants)


QC_V1_SHA256 = "96163e820dd54ed14b144de11d54d06d383ac745c7b264919af19ee3353bf27b"


def test_hash_is_pinned_for_qc_v1() -> None:
    """Changing qc_v1 in any semantic way must be a deliberate, visible act (a new version)."""
    assert load_quality_config(QC_V1).sha256 == QC_V1_SHA256


# ----------------------------------------------------------------------------- strict schema


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("version: qc_v1", "version: v1", "version"),
        ("status: PROVISIONAL_UNCALIBRATED", "status: TRUSTED", "status"),
        ("engine: sentinel-quality/v1", "engine: sentinel-quality/v2", "engine"),
        ("calendar: fx-ny1700-xmas-newyear/v1", "calendar: other/v1", "calendar"),
        ("version: qc_v1", "version: qc_v1\nextra: 1", "unknown key"),
        ("    max_spread: 0.0100", "    max_spread: 0.0100\n    min_spread: 0", "unknown key"),
        ("  max_listed: 20", "  max_listed: 0", "max_listed"),
        ("  max_listed: 20", "  max_listed: 20.0", "integer"),
        ("    max_gap_bars: 15", "    max_gap_bars: -1", "max_gap_bars"),
        ("    max_gap_bars: 15", "    max_gap_bars: true", "integer"),
        ("    max_gap_bars: 15", "    max_gap_bars: 15.5", "integer"),
        ("    min_completeness_pct: 99.0", "    min_completeness_pct: 100.1", "completeness"),
        ("    min_completeness_pct: 99.0", "    min_completeness_pct: 0", "completeness"),
        ("    min_completeness_pct: 99.0", "    min_completeness_pct: .nan", "finite"),
        ("    min_completeness_pct: 99.0", "    min_completeness_pct: .inf", "finite"),
        ("    min_completeness_pct: 99.0", "    min_completeness_pct: yes", "decimal"),
        ("    jump_flag_pct: 0.30", "    jump_flag_pct: 0", "positive"),
        ("    max_spread: 0.0100", "    max_spread: -0.0100", "positive"),
        ("  M1:", "  H4:", "fixed-grid"),
        ("  M1:", "  M2:", "timeframe"),
        ("  EUR_USD:", "  EURUSD:", "symbol"),
    ],
)
def test_malformed_configs_are_rejected(old: str, new: str, message: str) -> None:
    assert old in TEXT, old
    with pytest.raises(QualityConfigError, match=message):
        parse_quality_yaml(TEXT.replace(old, new, 1))


@pytest.mark.parametrize(
    "key", ["version", "status", "engine", "calendar", "report", "timeframes", "symbols"]
)
def test_every_top_level_key_is_required(key: str) -> None:
    m = _mapping()
    del m[key]
    with pytest.raises(QualityConfigError, match="missing key"):
        QualityConfig.from_mapping(m)


@pytest.mark.parametrize(
    "key",
    ["min_completeness_pct", "max_gap_bars", "max_unexpected_bars", "jump_flag_pct",
     "range_flag_pct"],
)  # fmt: skip
def test_every_timeframe_threshold_is_required(key: str) -> None:
    m = _mapping()
    del m["timeframes"]["M1"][key]
    with pytest.raises(QualityConfigError, match="missing key"):
        QualityConfig.from_mapping(m)


def test_symbol_threshold_and_sections_must_be_present_and_non_empty() -> None:
    m = _mapping()
    del m["symbols"]["EUR_USD"]["max_spread"]
    with pytest.raises(QualityConfigError, match="missing key"):
        QualityConfig.from_mapping(m)
    for section in ("timeframes", "symbols"):
        m = _mapping()
        m[section] = {}
        with pytest.raises(QualityConfigError, match="at least one"):
            QualityConfig.from_mapping(m)


def test_floats_are_rejected_even_when_passed_programmatically() -> None:
    m = _mapping()
    m["timeframes"]["M1"]["jump_flag_pct"] = 0.3
    with pytest.raises(QualityConfigError, match="float"):
        QualityConfig.from_mapping(m)
    m = _mapping()
    m["symbols"]["EUR_USD"]["max_spread"] = 0.01
    with pytest.raises(QualityConfigError, match="float"):
        QualityConfig.from_mapping(m)


def test_yaml_never_produces_floats_and_refuses_duplicate_keys() -> None:
    cfg = parse_quality_yaml(TEXT)
    assert isinstance(cfg.timeframes[Timeframe.M1].jump_flag_pct, Decimal)
    assert isinstance(cfg.symbols["EUR_USD"].max_spread, Decimal)
    assert cfg.symbols["EUR_USD"].max_spread == Decimal("0.0100")
    duplicated = TEXT.replace(
        "    max_gap_bars: 15", "    max_gap_bars: 15\n    max_gap_bars: 900", 1
    )
    with pytest.raises(QualityConfigError, match="duplicate key"):
        parse_quality_yaml(duplicated)


@pytest.mark.parametrize("text", ["", "- 1\n- 2\n", "version: [unclosed", "just text"])
def test_non_mapping_and_invalid_yaml_are_rejected(text: str) -> None:
    with pytest.raises(QualityConfigError):
        parse_quality_yaml(text)


def test_registry_refuses_two_files_with_the_same_version_or_content(tmp_path: Path) -> None:
    (tmp_path / "qc_v1.yaml").write_text(TEXT, encoding="utf-8")
    (tmp_path / "copy.yaml").write_text(TEXT.replace("\n", "\r\n"), encoding="utf-8")
    with pytest.raises(QualityConfigError, match="same content"):
        load_quality_registry(tmp_path)
    (tmp_path / "copy.yaml").write_text(
        TEXT.replace("    max_gap_bars: 15", "    max_gap_bars: 14", 1), encoding="utf-8"
    )
    with pytest.raises(QualityConfigError, match="qc_v1 is defined twice"):
        load_quality_registry(tmp_path)
    (tmp_path / "copy.yaml").write_text("version: [", encoding="utf-8")
    with pytest.raises(QualityConfigError):
        load_quality_registry(tmp_path)  # one bad file fails the whole registry
    (tmp_path / "copy.yaml").unlink()
    (tmp_path / "qc_v1.yaml").unlink()
    with pytest.raises(QualityConfigError, match="no quality configuration"):
        load_quality_registry(tmp_path)


# ----------------------------------------------------------------------------- immutability


def test_configuration_is_immutable() -> None:
    cfg = load_quality_config(QC_V1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.status = APPROVED  # type: ignore[misc]
    with pytest.raises(TypeError):
        cfg.timeframes[Timeframe.M1] = cfg.timeframes[Timeframe.M5]  # type: ignore[index]
    with pytest.raises(TypeError):
        cfg.symbols["GBP_USD"] = cfg.symbols["EUR_USD"]  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.timeframes[Timeframe.M1].max_gap_bars = 10_000  # type: ignore[misc]


# ----------------------------------------------------------------------------- CLI


def test_quality_hash_command(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["quality-hash", str(QC_V1)]) == 0
    out = capsys.readouterr().out.split()
    cfg = load_quality_config(QC_V1)
    assert out == [cfg.version, cfg.status, cfg.sha256]
    bad = tmp_path / "bad.yaml"
    bad.write_text(TEXT.replace("version: qc_v1", "version: qc_v1\nextra: 1"), encoding="utf-8")
    assert main(["quality-hash", str(bad)]) == 1
    assert "INVALID" in capsys.readouterr().err
    assert main(["quality-hash", str(tmp_path / "missing.yaml")]) == 1


# ----------------------------------------------------------------------------- isolation


def test_research_code_cannot_load_quality_configuration(tmp_path: Path) -> None:
    """Learning, backtest, indicators and strategy may not import the loader (import-linter
    forbids ``sentinel.config`` for them; the AST scan used by INV-DATA-VERIFIED-ONLY agrees).
    Nothing in the code writes a configuration, so there is no adaptation path either."""
    from architecture.test_verified_only import RESEARCH, _files, _raw_imports

    probe = tmp_path / "probe.py"
    probe.write_text(
        "from sentinel.config.quality_loader import load_quality_config\n"
        "import sentinel.config.quality_loader\n",
        encoding="utf-8",
    )
    assert len(_raw_imports(probe)) == 2
    assert "learning" in RESEARCH
    assert [p for pkg in RESEARCH for f in _files(pkg) for p in _raw_imports(f)] == []
    src = Path(__file__).resolve().parents[3] / "src"
    users = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "QUALITY_CONFIG_DIR" in path.read_text(encoding="utf-8")
        or "config/quality" in path.read_text(encoding="utf-8")
        or '"quality"' in path.read_text(encoding="utf-8")
    )
    assert users == ["sentinel/config/quality_loader.py"]
    loader = (src / "sentinel/config/quality_loader.py").read_text(encoding="utf-8")
    for call in (
        "write_text",
        "write_bytes",
        ".write(",
        "open(",
        "unlink",
        "rename",
        "os.replace",
        "shutil",
    ):
        assert call not in loader, call
