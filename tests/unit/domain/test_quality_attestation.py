"""Quality reports and attestations are unforgeable and bound to exactly the rows judged.

INV-DATA-QUALITY-GATED (ADR 0015): ``attest`` judges rows itself and never accepts a
report, so a report relabelled with another configuration (even consistently, JSON and
hash recomputed) cannot become an attestation; attestations cannot be replaced, copied,
subclassed or built directly; configurations are re-verified, never trusted by type."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from sentinel.domain.canonical import canonical_json
from sentinel.domain.dataset import (
    QUALITY_REPORT_FORMAT,
    DatasetError,
    DatasetSpec,
    QualityAttestation,
    QualityReport,
    attest,
    digest_candles,
    judge_dataset,
)
from sentinel.domain.market_data import Timeframe
from sentinel.domain.quality import APPROVED, PROVISIONAL, QualityConfig

from support.market import candle, minutes
from support.quality import lenient_config, quality_config

MON = datetime(2015, 1, 5, tzinfo=UTC)
AS_OF = datetime(2020, 1, 1, tzinfo=UTC)
SPEC = DatasetSpec("EUR_USD", Timeframe.M1, "oanda-practice", MON, MON + timedelta(minutes=30))
BARS = [candle("EUR_USD", t) for t in minutes(MON, 30)]
CFG = quality_config()


def test_a_pass_report_names_the_dataset_config_and_time_it_judged() -> None:
    report = judge_dataset(SPEC, CFG, AS_OF, BARS)
    assert report.passed
    assert report.digest == digest_candles(SPEC, BARS)
    body = json.loads(report.canonical_json)
    assert body["format"] == QUALITY_REPORT_FORMAT
    assert body["config"] == {"version": CFG.version, "status": CFG.status, "sha256": CFG.sha256}
    assert body["dataset"]["sha256"] == report.digest.sha256
    assert body["dataset"]["rows_sha256"] == report.digest.rows_sha256
    assert body["dataset"]["source"] == "oanda-practice"
    assert body["as_of"] == "2020-01-01T00:00:00.000000Z"
    assert body["result"]["verdict"] == "PASS"
    assert report.sha256 == hashlib.sha256(report.canonical_json.encode("ascii")).hexdigest()


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_the_report_hash_changes_with_rows_config_range_source_and_time() -> None:
    base = judge_dataset(SPEC, CFG, AS_OF, BARS).sha256
    variants = [
        judge_dataset(SPEC, CFG, AS_OF, [*BARS[:10], replace(BARS[10], tick_volume=7), *BARS[11:]]),
        judge_dataset(SPEC, quality_config(max_gap_bars=16), AS_OF, BARS),
        judge_dataset(SPEC, CFG, AS_OF + timedelta(seconds=1), BARS),
        judge_dataset(replace(SPEC, source="dukascopy"), CFG, AS_OF, BARS),
        judge_dataset(replace(SPEC, end=SPEC.end - timedelta(minutes=1)), CFG, AS_OF, BARS[:-1]),
    ]
    hashes = {r.sha256 for r in variants}
    assert base not in hashes
    assert len(hashes) == len(variants)
    assert judge_dataset(SPEC, CFG, AS_OF, list(BARS)).sha256 == base  # deterministic


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_attestation_binds_every_identity_field() -> None:
    report = judge_dataset(SPEC, CFG, AS_OF, BARS)
    a = attest(SPEC, CFG, AS_OF, BARS)
    assert a.spec == SPEC
    assert (a.rows, a.rows_sha256, a.sha256) == (
        30,
        report.digest.rows_sha256,
        report.digest.sha256,
    )
    assert (a.config_version, a.config_sha256) == (CFG.version, CFG.sha256)
    assert a.report_sha256 == report.sha256
    assert a.provenance.verdict == "PASS"
    assert a.provenance.report_sha256 == report.sha256


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_a_failing_range_cannot_be_attested() -> None:
    holed = BARS[:5] + BARS[25:]
    report = judge_dataset(SPEC, CFG, AS_OF, holed)
    assert report.verdict == "FAIL"
    with pytest.raises(DatasetError, match=r"FAIL.*gap_above_max"):
        attest(SPEC, CFG, AS_OF, holed)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_only_an_approved_configuration_can_attest() -> None:
    provisional = quality_config(status=PROVISIONAL)
    report = judge_dataset(SPEC, provisional, AS_OF, BARS)
    assert report.passed  # it can be judged and reported on...
    with pytest.raises(DatasetError, match="PROVISIONAL_UNCALIBRATED"):
        attest(SPEC, provisional, AS_OF, BARS)  # ...but not frozen


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_reports_and_attestations_cannot_be_constructed_directly() -> None:
    report = judge_dataset(SPEC, CFG, AS_OF, BARS)
    with pytest.raises(DatasetError, match="judge_dataset"):
        QualityReport(
            report.spec,
            report.digest,
            report.config,
            report.as_of,
            report.result,
            report.canonical_json,
            report.sha256,
            object(),
        )
    with pytest.raises(DatasetError, match="attest"):
        QualityAttestation(report, _key=object())
    with pytest.raises(DatasetError, match="only a QualityConfig"):
        attest(SPEC, object(), AS_OF, BARS)  # type: ignore[arg-type]


def test_rows_the_digest_cannot_accept_are_refused_not_reported() -> None:
    with pytest.raises(DatasetError, match="strictly increasing"):
        judge_dataset(SPEC, lenient_config(), AS_OF, [BARS[1], BARS[0]])
    with pytest.raises(DatasetError, match="empty"):
        judge_dataset(SPEC, lenient_config(), AS_OF, [])


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_judging_does_not_change_the_rows() -> None:
    rows = list(BARS)
    judge_dataset(SPEC, CFG, AS_OF, rows)
    assert rows == BARS


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_v3_a_report_cannot_be_given_another_configuration() -> None:
    """``replace`` copies the issuer key, so the key alone proved nothing: a provisional
    report re-labelled with an APPROVED configuration used to attest."""
    provisional = quality_config(status=PROVISIONAL)
    report = judge_dataset(SPEC, provisional, AS_OF, BARS)
    with pytest.raises(DatasetError, match="do not match its canonical JSON"):
        replace(report, config=CFG)
    approved_body = judge_dataset(SPEC, CFG, AS_OF, BARS)
    with pytest.raises(DatasetError, match="do not match its canonical JSON"):
        replace(report, config=CFG, sha256=approved_body.sha256)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_no_report_field_can_be_replaced_without_breaking_its_integrity() -> None:
    report = judge_dataset(SPEC, CFG, AS_OF, BARS)
    failing = judge_dataset(SPEC, CFG, AS_OF, BARS[:5] + BARS[25:])
    other_spec = replace(SPEC, source="dukascopy")
    changes: list[tuple[dict[str, object], str]] = [
        ({"sha256": "0" * 64}, "hash does not match"),
        ({"canonical_json": failing.canonical_json}, "do not match"),
        ({"canonical_json": report.canonical_json + " "}, "do not match"),
        ({"result": failing.result}, "do not match"),
        ({"digest": failing.digest}, "do not match"),
        ({"spec": other_spec}, "do not match"),
        ({"as_of": AS_OF + timedelta(seconds=1)}, "do not match"),
        ({"config": quality_config(max_gap_bars=16)}, "do not match"),
        ({"config": None}, "wrong types"),
        ({"as_of": "2020-01-01T00:00:00Z"}, "wrong types"),
    ]
    for change, match in changes:
        with pytest.raises(DatasetError, match=match):
            replace(report, **change)  # type: ignore[arg-type]
    assert replace(report).sha256 == report.sha256  # an unchanged copy is still consistent


# ----------------------------------------------------------------------------- F3: relabelling


def _relabelled(report: QualityReport, config: QualityConfig) -> QualityReport:
    """The F3 forgery: a report relabelled with ``config``, JSON and hash recomputed, so the
    report itself is internally consistent. Uses public API only."""
    body = json.loads(report.canonical_json)
    body["config"] = {"version": config.version, "status": config.status, "sha256": config.sha256}
    text = canonical_json(body)
    sha = hashlib.sha256(text.encode("ascii")).hexdigest()
    return replace(report, config=config, canonical_json=text, sha256=sha)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_a_consistently_relabelled_report_cannot_become_an_attestation() -> None:
    holed = BARS[:10] + BARS[15:]  # a 5-bar gap: PASS when lenient, FAIL when strict
    provisional = lenient_config(status=PROVISIONAL)
    report = judge_dataset(SPEC, provisional, AS_OF, holed)
    assert report.passed
    forged = _relabelled(report, CFG)  # names the APPROVED, strict configuration
    assert forged.config is CFG
    assert forged.passed  # the forged object claims PASS...
    with pytest.raises(TypeError):
        attest(forged)  # type: ignore[call-arg, arg-type]  # ...but attest never takes a report
    with pytest.raises(DatasetError, match=r"FAIL.*completeness_below_min"):
        attest(SPEC, CFG, AS_OF, holed)  # it judges the rows under CFG's own gates
    assert attest(SPEC, lenient_config(), AS_OF, holed).config_sha256 == lenient_config().sha256


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_attestations_cannot_be_replaced_copied_changed_or_subclassed() -> None:
    a = attest(SPEC, CFG, AS_OF, BARS)
    with pytest.raises(TypeError):
        replace(a, config_sha256="e" * 64)  # type: ignore[type-var]
    assert not dataclasses.is_dataclass(a)
    for name in ("config_sha256", "report_sha256", "rows", "spec"):
        with pytest.raises(AttributeError):
            setattr(a, name, "e" * 64)
    with pytest.raises(AttributeError):
        del a.sha256
    with pytest.raises(AttributeError):
        copy.copy(a)
    with pytest.raises(TypeError, match="cannot be subclassed"):

        class Forged(QualityAttestation):
            pass

    with pytest.raises(TypeError, match="cannot be subclassed"):

        class ForgedReport(QualityReport):
            pass


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_configuration_subclasses_and_hollow_objects_cannot_attest() -> None:
    with pytest.raises(TypeError, match="cannot be subclassed"):

        class Fake(QualityConfig):
            pass

    # Allocated without its constructor, with a forged hash and status: no __post_init__ ran.
    hollow = object.__new__(QualityConfig)
    for f in dataclasses.fields(CFG):
        object.__setattr__(hollow, f.name, getattr(CFG, f.name))
    object.__setattr__(hollow, "max_listed", CFG.max_listed + 1)  # content no longer matches
    with pytest.raises(DatasetError, match="identity mismatch"):
        attest(SPEC, hollow, AS_OF, BARS)
    empty = object.__new__(QualityConfig)
    with pytest.raises(DatasetError, match="incomplete quality configuration"):
        attest(SPEC, empty, AS_OF, BARS)
    weak = object.__new__(type(CFG.timeframes[Timeframe.M1]))
    for f in dataclasses.fields(weak):
        object.__setattr__(weak, f.name, getattr(CFG.timeframes[Timeframe.M1], f.name))
    object.__setattr__(weak, "max_gap_bars", -1)
    hollow2 = object.__new__(QualityConfig)
    for f in dataclasses.fields(CFG):
        object.__setattr__(hollow2, f.name, getattr(CFG, f.name))
    object.__setattr__(hollow2, "timeframes", MappingProxyType({Timeframe.M1: weak}))
    with pytest.raises(DatasetError, match="max_gap_bars"):
        attest(SPEC, hollow2, AS_OF, BARS)
    assert CFG.verify() is CFG
    assert CFG.status == APPROVED
