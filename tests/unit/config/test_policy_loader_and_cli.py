"""Policy-file parsing edge cases, learning proposals, and the read-only CLI commands."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sentinel.audit.chain import AuditKind, AuditLog
from sentinel.cli import main
from sentinel.config.policy_loader import parse_policy_yaml
from sentinel.domain.types import Percent
from sentinel.learning.proposals import ProposalKind, draft_proposal
from sentinel.risk.governance import ProtectedPolicyStore, issue_human_risk_admin
from sentinel.risk.policy import PolicyViolation
from sentinel.store.audit_jsonl import JsonlAuditSink

from support.builders import NOW, POLICY_PATH, policy
from support.fakes import FixedClock

TEXT = POLICY_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("id: rs_v1", "id: 42", "string"),
        ("account_currency: USD", "account_currency: XXX", "XXX"),
        ("  max_open_positions: 3", "  max_open_positions: 3.5", "integer"),
        ("  require_broker_side_stop: true", '  require_broker_side_stop: "yes"', "true or false"),
        (
            'friday_no_new_entries_after_utc: "18:00"',
            'friday_no_new_entries_after_utc: "6pm"',
            "HH:MM",
        ),
        ("tz: America/New_York", "tz: Mars/Olympus", "timezone"),
        ("  max_stop_atr_h1: 4.0", "  max_stop_atr_h1: 0.9", "min_stop_atr_h1"),
        ("  halt_after_consecutive_losses: 5", "  halt_after_consecutive_losses: 2", "halt"),
        ("  weekly_pct: 3.0", "  weekly_pct: 1.0", "daily <= weekly"),
        ("  max_hold_horizon_hours: 4", "  max_hold_horizon_hours: 100", "max_hold_horizon"),
        ("  max_open_risk_pct: 1.50", "  max_open_risk_pct: .nan", "finite"),
        ("  - {drawdown_pct: 4.0, risk_multiplier: 0.75}", "  - 5", "mapping"),
        (
            "  - {drawdown_pct: 4.0, risk_multiplier: 0.75}",
            "  - {drawdown_pct: 4.0}",
            "risk_multiplier",
        ),
    ],
)
def test_malformed_policy_files_are_rejected(old: str, new: str, message: str) -> None:
    assert old in TEXT
    with pytest.raises(PolicyViolation, match=message):
        parse_policy_yaml(TEXT.replace(old, new, 1))


def test_non_mapping_and_invalid_yaml() -> None:
    with pytest.raises(PolicyViolation):
        parse_policy_yaml("- a\n- b\n")
    with pytest.raises(PolicyViolation):
        parse_policy_yaml("a: [unclosed\n")


def test_throttle_multiplier_lookup() -> None:
    p = policy()
    assert p.throttle_multiplier(Percent(Decimal(0))) == 1
    assert p.throttle_multiplier(Percent(Decimal(4))) == Decimal("0.75")
    assert p.throttle_multiplier(Percent(Decimal(9))) == Decimal("0.25")


def test_learning_drafts_proposals_against_the_read_only_view() -> None:
    store = ProtectedPolicyStore(
        policy(), installed_by=issue_human_risk_admin("ops", step_up_verified=True), at=NOW
    )
    proposal = draft_proposal(
        store.reader(),
        kind=ProposalKind.FILTER,
        target="range_breakout@1.0.0",
        summary="block when ATR percentile < 20",
        evidence={"n": "87", "q": "0.04"},
        created_at=NOW,
    )
    assert proposal.based_on_policy_sha256 == policy().sha256
    assert store.active() == policy()
    assert len(store.history) == 1


# ----------------------------------------------------------------------------- CLI


def test_cli_policy_hash(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["policy-hash", str(POLICY_PATH)]) == 0
    assert capsys.readouterr().out.strip() == f"rs_v1 {policy().sha256}"
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        TEXT.replace("risk_per_trade_pct: 0.50", "risk_per_trade_pct: 2"), encoding="utf-8"
    )
    assert main(["policy-hash", str(bad)]) == 1
    assert "INVALID" in capsys.readouterr().err


def test_cli_audit_verify(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(JsonlAuditSink(path), clock=FixedClock(NOW))
    log.record(AuditKind.STATE_TRANSITION, "s", {"to": "HALTED"})
    log.record(AuditKind.STATE_TRANSITION, "s", {"to": "NO_NEW_TRADES"})
    assert main(["audit-verify", str(path)]) == 0
    assert "OK: 2 records" in capsys.readouterr().out
    lines = path.read_text(encoding="utf-8").splitlines()
    doc = json.loads(lines[1])
    doc["payload"]["to"] = "ACTIVE"
    path.write_text(lines[0] + "\n" + json.dumps(doc) + "\n", encoding="utf-8")
    assert main(["audit-verify", str(path)]) == 1
    assert "BROKEN" in capsys.readouterr().err


def test_cli_settings_reports_execution_refused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SENTINEL_ENV", raising=False)
    assert main(["settings", "--env", "live"]) == 0
    out = capsys.readouterr().out
    assert "execution: REFUSED" in out
    assert "hard-disabled" in out
    assert main(["settings", "--env", "nope"]) == 1


def test_signal_ttl_policy_is_minutes() -> None:
    assert policy().signal_ttl == timedelta(minutes=15)
