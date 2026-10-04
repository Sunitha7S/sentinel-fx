"""The risk kernel: a pure, total function from ``RiskInputs`` to ``RiskDecision``.

* **Pure.** No I/O, no clock reads (``as_of`` is an input), no randomness.
* **Total.** It never raises. Missing or malformed inputs fail the rules that need them.
* **Exhaustive.** Every rule runs and is reported, so a BLOCKED decision lists every
  reason, not just the first one found.
* **Final.** APPROVED only if every HARD rule passed and a valid size exists; the
  ``RiskDecision`` constructor re-checks this, so an invalid approval cannot be built.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from sentinel.domain.decision import Outcome, RiskDecision, RuleResult, Stage
from sentinel.domain.types import Money
from sentinel.risk.context import Context, MissingInput, RiskInputs
from sentinel.risk.rules import account, market, portfolio, system, trade
from sentinel.risk.rules.base import Rule

__all__ = ["ALL_RULE_IDS", "RULES", "RiskInputs", "blocked_decision", "evaluate"]

RULES: tuple[Rule, ...] = (
    *system.RULES,
    *account.RULES,
    *market.RULES,
    *trade.RULES,
    *portfolio.RULES,
)
ALL_RULE_IDS: tuple[str, ...] = tuple(r.rule_id for r in RULES)

KERNEL_FAILURE_RULE_ID = "R-SYS-99"


def _run(rule: Rule, ctx: Context) -> RuleResult:
    try:
        check = rule.check(ctx)
    except MissingInput as exc:
        return RuleResult(
            rule.rule_id, rule.stage, False, f"missing input: {exc}", severity=rule.severity
        )
    except Exception as exc:  # noqa: BLE001 - a broken rule must block, never crash the kernel
        return RuleResult(
            rule.rule_id,
            rule.stage,
            False,
            f"rule error: {type(exc).__name__}: {exc}",
            severity=rule.severity,
        )
    return RuleResult(
        rule.rule_id,
        rule.stage,
        check.passed,
        check.message,
        observed=check.observed,
        threshold=check.threshold,
        severity=rule.severity,
    )


def _try[T](getter: Callable[[], T]) -> T | None:
    """Evaluate an optional enrichment; failures are already reported by the rules."""
    try:
        return getter()
    except Exception:  # noqa: BLE001
        return None


def blocked_decision(inputs: RiskInputs, reason: str) -> RiskDecision:
    """A minimal BLOCKED decision for when the kernel itself cannot run."""
    policy = inputs.policy
    return RiskDecision(
        decision_id=inputs.decision_id,
        candidate_id=inputs.candidate.candidate_id if inputs.candidate else None,
        as_of=inputs.as_of,
        outcome=Outcome.BLOCKED,
        units=Decimal(0),
        risk_amount=None,
        effective_risk=None,
        rule_results=(RuleResult(KERNEL_FAILURE_RULE_ID, Stage.SYSTEM, False, reason),),
        policy_id=policy.policy_id if policy else None,
        policy_sha256=_safe_sha(inputs),
        state_version=inputs.account.state_version if inputs.account else None,
        snapshot_digest=None,
    )


def _safe_sha(inputs: RiskInputs) -> str | None:
    try:
        return inputs.policy.sha256 if inputs.policy else None
    except Exception:  # noqa: BLE001
        return None


def evaluate(inputs: RiskInputs) -> RiskDecision:
    try:
        return _evaluate(inputs)
    except Exception as exc:  # noqa: BLE001 - totality: the kernel never raises
        return blocked_decision(inputs, f"kernel failure: {type(exc).__name__}: {exc}")


def _evaluate(inputs: RiskInputs) -> RiskDecision:
    ctx = Context(inputs)
    results = tuple(_run(rule, ctx) for rule in RULES)
    all_passed = not any(r.blocks for r in results)

    effective = _try(lambda: ctx.effective_risk)
    sizing = _try(lambda: ctx.sizing)
    digest = _try(lambda: ctx.snapshot_digest)
    account = inputs.account
    policy = inputs.policy
    approved = (
        all_passed
        and sizing is not None
        and sizing.ok
        and digest is not None
        and account is not None
    )
    units = Decimal(0)
    risk_amount = None
    if approved and sizing is not None and account is not None:
        units = sizing.units
        risk_amount = Money(sizing.risk_amount, account.currency)
    return RiskDecision(
        decision_id=inputs.decision_id,
        candidate_id=inputs.candidate.candidate_id if inputs.candidate else None,
        as_of=inputs.as_of,
        outcome=Outcome.APPROVED if approved else Outcome.BLOCKED,
        units=units,
        risk_amount=risk_amount,
        effective_risk=effective,
        rule_results=results,
        policy_id=policy.policy_id if policy else None,
        policy_sha256=_safe_sha(inputs),
        state_version=account.state_version if account else None,
        snapshot_digest=digest,
    )
