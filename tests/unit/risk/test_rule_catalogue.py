"""docs/04 section 3 must list exactly the rules the code evaluates."""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

from sentinel.decision.fail_closed import FAIL_CLOSED_RULE_ID
from sentinel.domain.types import DecisionId
from sentinel.risk.approvals import verify_approvals
from sentinel.risk.kernel import ALL_RULE_IDS, KERNEL_FAILURE_RULE_ID, evaluate
from sentinel.risk.recheck import recheck_before_execution

from support.builders import NOW, REPO_ROOT, inputs

DOC = REPO_ROOT / "docs" / "04-risk-engine.md"


def _documented_ids() -> list[str]:
    text = DOC.read_text(encoding="utf-8")
    section = text[text.index("## 3. Rule catalogue") : text.index("## 4. Position sizing")]
    return re.findall(r"^\| (R-[A-Z]{3}-\d{2}) \|", section, flags=re.MULTILINE)


def _code_ids() -> set[str]:
    approval_ids = {
        r.rule_id
        for r in verify_approvals(
            (),
            now=NOW,
            decision_id=DecisionId("d"),
            state_version=None,
            snapshot_digest=None,
            payload_sha256="x",
        )
    }
    blocked_input = inputs(day_loss_pct=Decimal(2))
    original = evaluate(blocked_input)
    assert blocked_input.candidate is not None
    recheck = recheck_before_execution(
        original=original,
        candidate=blocked_input.candidate,
        approvals=(),
        fresh=inputs(now=NOW + timedelta(seconds=5)),
    )
    exe_ids = {r.rule_id for r in recheck.rule_results if r.rule_id.startswith("R-EXE")}
    return {*ALL_RULE_IDS, KERNEL_FAILURE_RULE_ID, FAIL_CLOSED_RULE_ID, *approval_ids, *exe_ids}


def test_catalogue_lists_every_rule_exactly_once() -> None:
    documented = _documented_ids()
    assert len(documented) == len(set(documented)), "duplicate rule ids in docs/04"
    assert set(documented) == _code_ids()
