"""The runtime trust boundary around freeze and load (ADR 0015 §4; review findings N1, N2).

INV-DATA-QUALITY-GATED, at run time:

* N1: the snapshot store cannot be subclassed, its registry and other state cannot be
  replaced after construction, and the authorisation decision does not run through a method a
  subclass could override. Every refusal here happens before the database is touched.
* N2: production cannot choose where approval comes from. ``load_quality_registry`` takes no
  directory, the packaged directory must have the pinned trust root (so redirecting it gains
  nothing), and a fixture registry over any other directory authorises nothing outside a
  test session.

Threat model: these checks stop ordinary mistakes and convenient bypasses by application code.
Code in the same process that deliberately rewrites objects or classes (``object.__setattr__``
on private slots, patching modules) is out of scope; the architecture scan flags it in review.
"""

from __future__ import annotations

import inspect
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest

from sentinel import cli
from sentinel.config import quality_loader
from sentinel.config.quality_loader import (
    CANONICAL,
    FIXTURE,
    QUALITY_CONFIG_DIR,
    TRUST_ROOT_SHA256,
    ApprovalRecord,
    QualityConfigRegistry,
    load_fixture_registry,
    load_quality_registry,
    trust_root,
)
from sentinel.domain.dataset import DatasetError, DatasetSpec
from sentinel.domain.market_data import Timeframe
from sentinel.domain.quality import QualityConfig, QualityConfigError
from sentinel.store.postgres import dataset_store
from sentinel.store.postgres.dataset_store import PostgresDatasetStore

from support.quality import quality_config, write_configs

QC_V1_SHA256 = "96163e820dd54ed14b144de11d54d06d383ac745c7b264919af19ee3353bf27b"
SNAPSHOT_ID = "00000000-0000-0000-0000-000000000001"
MON = datetime(2015, 1, 5, tzinfo=UTC)
SPEC = DatasetSpec("EUR_USD", Timeframe.M1, "oanda-practice", MON, MON + timedelta(hours=1))


class _NoDatabase:
    """Any use of the engine fails the test: refusals must come before the database."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the database was touched ({name})")


def _store(registry: QualityConfigRegistry | None) -> PostgresDatasetStore:
    return PostgresDatasetStore(_NoDatabase(), registry=registry)  # type: ignore[arg-type]


def _fixture(tmp_path: Path) -> tuple[QualityConfigRegistry, QualityConfig]:
    cfg = quality_config()
    registry = load_fixture_registry(write_configs(tmp_path, cfg))
    return registry, registry.approved(cfg.sha256)


def _refuses_before_the_database(store: PostgresDatasetStore, quality: QualityConfig) -> None:
    """freeze and load both refuse with a DatasetError; touching the engine would raise an
    AssertionError instead and fail the test."""
    with pytest.raises(DatasetError):
        store.freeze(SPEC, quality)
    with pytest.raises(DatasetError):
        store.load(SNAPSHOT_ID)


@pytest.fixture
def outside_a_test_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process as the runtime image runs it: no test runner loaded."""
    monkeypatch.delitem(sys.modules, "pytest")


# ----------------------------------------------------------------------------- N1: the store


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_the_store_cannot_be_subclassed_to_override_authorisation() -> None:
    assert not hasattr(PostgresDatasetStore, "_trusted")  # no overridable hook remains
    with pytest.raises(TypeError, match="cannot be subclassed"):

        class Evil(PostgresDatasetStore):
            __slots__ = ()

            def _trusted(self, sha256: str, what: str) -> QualityConfig:
                return quality_config()

            def _checked(self, what: str) -> QualityConfigRegistry:
                raise NotImplementedError

    with pytest.raises(TypeError, match="cannot be subclassed"):
        type("Evil", (PostgresDatasetStore,), {"_trusted": lambda *a: quality_config()})


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_the_stores_state_cannot_be_replaced_after_construction(tmp_path: Path) -> None:
    registry, trusted = _fixture(tmp_path)
    store = _store(registry)
    assert not hasattr(store, "__dict__")
    for name in ("_registry", "_engine", "_max_rows", "_trusted", "freeze", "__class__"):
        with pytest.raises(AttributeError, match="read-only"):
            setattr(store, name, None)
    with pytest.raises(AttributeError, match="read-only"):
        store._registry = None
    for name in ("_registry", "_engine", "_max_rows"):
        with pytest.raises(AttributeError, match="read-only"):
            delattr(store, name)
    with pytest.raises(AssertionError, match="the database was touched"):
        store.freeze(SPEC, trusted)  # unchanged and trusted


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_a_registry_forced_into_the_store_is_verified_again_before_the_database(
    tmp_path: Path,
) -> None:
    """Even a private slot rewritten with object.__setattr__ (out of the threat model) gains
    nothing unless the new registry itself verifies: freeze and load re-check it first."""
    registry, trusted = _fixture(tmp_path)
    hollow = QualityConfigRegistry.__new__(QualityConfigRegistry)
    forged = object.__new__(QualityConfigRegistry)
    object.__setattr__(forged, "_configs", MappingProxyType({trusted.sha256: trusted}))
    object.__setattr__(forged, "_approved", frozenset({trusted.sha256}))
    object.__setattr__(forged, "_sealed", object())
    for replacement in (None, hollow, forged, {trusted.sha256: trusted}, object()):
        store = _store(registry)
        object.__setattr__(store, "_registry", replacement)
        _refuses_before_the_database(store, trusted)


# ----------------------------------------------------------------------------- N2: the directory


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_production_cannot_choose_the_approval_directory(tmp_path: Path) -> None:
    assert inspect.signature(load_quality_registry).parameters == {}
    write_configs(tmp_path, quality_config())
    with pytest.raises(TypeError):
        load_quality_registry(tmp_path)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        load_quality_registry(directory=tmp_path)  # type: ignore[call-arg]


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_the_packaged_registry_has_the_pinned_trust_root() -> None:
    registry = load_quality_registry()
    assert registry.origin == CANONICAL
    assert registry.trust_root == TRUST_ROOT_SHA256
    assert trust_root([QC_V1_SHA256], []) == TRUST_ROOT_SHA256  # qc_v1 alone, no approvals
    assert registry.verify() is registry


def _redirect(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    monkeypatch.setattr(quality_loader, "QUALITY_CONFIG_DIR", directory)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_a_redirected_directory_authorises_only_the_pinned_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where the files are read from does not matter; what they contain does."""
    copy = tmp_path / "copy"
    shutil.copytree(QUALITY_CONFIG_DIR, copy)
    _redirect(monkeypatch, copy)
    assert load_quality_registry().trust_root == TRUST_ROOT_SHA256  # same content: same root

    cfg = quality_config()
    write_configs(copy, cfg)  # a new APPROVED configuration with a well-formed record
    with pytest.raises(QualityConfigError, match="not the pinned"):
        load_quality_registry()

    arbitrary = write_configs(tmp_path / "arbitrary", cfg)
    _redirect(monkeypatch, arbitrary)
    with pytest.raises(QualityConfigError, match="not the pinned"):
        load_quality_registry()


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_absent_or_altered_packaged_files_refuse_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect(monkeypatch, tmp_path / "missing")
    with pytest.raises(QualityConfigError, match="no quality configuration"):
        load_quality_registry()

    altered = tmp_path / "altered"
    shutil.copytree(QUALITY_CONFIG_DIR, altered)
    qc_v1 = altered / "qc_v1.yaml"
    text = qc_v1.read_text(encoding="utf-8")
    qc_v1.write_text(text.replace("max_gap_bars: 15", "max_gap_bars: 16", 1), encoding="utf-8")
    _redirect(monkeypatch, altered)
    with pytest.raises(QualityConfigError, match="not the pinned"):
        load_quality_registry()

    comments = tmp_path / "comments"
    shutil.copytree(QUALITY_CONFIG_DIR, comments)
    (comments / "qc_v1.yaml").write_text("# a comment\n" + text.replace("\n", "\r\n"), "utf-8")
    _redirect(monkeypatch, comments)
    assert load_quality_registry().trust_root == TRUST_ROOT_SHA256  # content, not bytes


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_a_canonical_registry_stops_authorising_if_the_pin_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = load_quality_registry()
    monkeypatch.setattr(quality_loader, "TRUST_ROOT_SHA256", "0" * 64)
    with pytest.raises(QualityConfigError, match="not the pinned"):
        registry.verify()
    with pytest.raises(DatasetError, match="not the pinned"):
        _store(registry)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
@pytest.mark.usefixtures("outside_a_test_session")
def test_an_arbitrary_directory_cannot_be_loaded_outside_a_test_session(tmp_path: Path) -> None:
    write_configs(tmp_path, quality_config())
    with pytest.raises(QualityConfigError, match="outside a test session"):
        load_fixture_registry(tmp_path)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_a_fixture_registry_authorises_nothing_outside_a_test_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry loaded while tests run, then used as the runtime image would use it."""
    registry, trusted = _fixture(tmp_path)
    assert registry.origin == FIXTURE
    store = _store(registry)  # built inside the session
    monkeypatch.delitem(sys.modules, "pytest")
    with pytest.raises(QualityConfigError, match="outside a test session"):
        registry.verify()
    with pytest.raises(QualityConfigError, match="outside a test session"):
        registry.approved(trusted.sha256)
    with pytest.raises(DatasetError, match="outside a test session"):
        _store(registry)
    with pytest.raises(DatasetError, match="outside a test session"):
        store.freeze(SPEC, trusted)
    with pytest.raises(DatasetError, match="outside a test session"):
        store.load(SNAPSHOT_ID)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_a_registry_with_an_unknown_origin_authorises_nothing() -> None:
    registry = load_quality_registry()
    object.__setattr__(registry, "_origin", "elsewhere")  # out of the threat model, still refused
    with pytest.raises(QualityConfigError, match="unknown registry origin"):
        registry.verify()


def test_the_trust_root_is_order_and_name_independent() -> None:
    a, b = quality_config(), quality_config(version="qc_v902", max_gap_bars=16)
    ra = ApprovalRecord(a.version, a.sha256, "x", date(2026, 1, 1), "e")
    rb = ApprovalRecord(b.version, b.sha256, "y", date(2026, 1, 2), "f")
    one = trust_root([a.sha256, b.sha256], [(Path("1.yaml"), ra), (Path("2.yaml"), rb)])
    two = trust_root([b.sha256, a.sha256], [(Path("z.yaml"), rb), (Path("y.yaml"), ra)])
    assert one == two
    assert trust_root([a.sha256], [(Path("1.yaml"), ra)]) != trust_root([a.sha256], [])
    edited = ApprovalRecord(a.version, a.sha256, "x", date(2026, 1, 1), "other evidence")
    assert trust_root([a.sha256], [(Path("1.yaml"), edited)]) != trust_root(
        [a.sha256], [(Path("1.yaml"), ra)]
    )


# ----------------------------------------------------------------------------- operator command


def test_quality_registry_command_prints_the_verified_trust_root(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["quality-registry"]) == 0
    out = capsys.readouterr().out
    assert f"trust root {TRUST_ROOT_SHA256}" in out
    assert f"qc_v1 PROVISIONAL_UNCALIBRATED {QC_V1_SHA256} not approved" in out


def test_quality_registry_command_refuses_unpinned_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _redirect(monkeypatch, write_configs(tmp_path, quality_config()))
    assert cli.main(["quality-registry"]) == 2
    assert "not the pinned" in capsys.readouterr().err


def test_an_approval_takes_effect_only_with_its_pinned_trust_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """How a reviewed approval change ships: the record and the new pin land together."""
    cfg = quality_config()
    directory = write_configs(tmp_path, cfg)
    _redirect(monkeypatch, directory)
    with pytest.raises(QualityConfigError, match="not the pinned"):
        load_quality_registry()
    pinned = load_fixture_registry(directory).trust_root
    monkeypatch.setattr(quality_loader, "TRUST_ROOT_SHA256", pinned)
    registry = load_quality_registry()
    assert registry.origin == CANONICAL
    assert registry.approved(cfg.sha256) is registry.approved(cfg.sha256)
    assert cli.main(["quality-registry"]) == 0
    assert f"{cfg.version} APPROVED {cfg.sha256} approved" in capsys.readouterr().out


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_the_per_call_check_refuses_anything_but_the_store_itself() -> None:
    with pytest.raises(DatasetError, match="not a PostgresDatasetStore"):
        dataset_store._store_registry(object(), "freeze a snapshot")  # type: ignore[arg-type]
