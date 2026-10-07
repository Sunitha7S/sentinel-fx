"""Quality reports and attestations are unforgeable and bound to exactly the rows judged."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

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
from sentinel.domain.quality import PROVISIONAL

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
    a = attest(report)
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
        attest(report)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_only_an_approved_configuration_can_attest() -> None:
    provisional = quality_config(status=PROVISIONAL)
    report = judge_dataset(SPEC, provisional, AS_OF, BARS)
    assert report.passed  # it can be judged and reported on...
    with pytest.raises(DatasetError, match="PROVISIONAL_UNCALIBRATED"):
        attest(report)  # ...but not frozen


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
        QualityAttestation(SPEC, 30, "a" * 64, "b" * 64, "qc_v1", "c" * 64, "d" * 64, object())
    with pytest.raises(DatasetError, match="judge_dataset"):
        attest(object())  # type: ignore[arg-type]


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
