"""Decisions, approvals and shadow trades (append-only).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-04

* Every decision references the exact policy hash it was made under (foreign key). An
  APPROVED decision must reference the policy that is active when it is recorded, and must
  carry the bindings ``RiskDecision`` requires (candidate, state version, snapshot digest).
* Approvals must belong to an APPROVED decision and repeat its bindings.
* Shadow trades must match the outcome of the decision they belong to. Their simulated
  outcomes will go to a separate append-only table in M9, so rows are never updated.
* ``svc_risk`` writes all three; nobody updates, deletes or truncates.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

READERS = "human_admin, svc_risk, svc_learning, svc_execution"


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE ON sentinel.{table} "
        "FOR EACH ROW EXECUTE FUNCTION sentinel.forbid_mutation()"
    )
    op.execute(
        f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON sentinel.{table} "
        "FOR EACH STATEMENT EXECUTE FUNCTION sentinel.forbid_mutation()"
    )


def upgrade() -> None:
    op.execute("SET LOCAL ROLE sentinel_owner")
    op.execute(
        """
        CREATE TABLE sentinel.decisions (
          decision_id         text PRIMARY KEY,
          candidate_id        text,
          as_of               timestamptz NOT NULL,
          outcome             text NOT NULL CHECK (outcome IN ('APPROVED', 'BLOCKED')),
          units               numeric NOT NULL CHECK (units >= 0),
          risk_amount         numeric,
          risk_currency       char(3),
          effective_risk_pct  numeric,
          policy_id           text,
          policy_sha256       char(64) REFERENCES sentinel.policy_versions (sha256),
          state_version       bigint,
          snapshot_digest     char(64),
          blocking_rules      text[] NOT NULL,
          payload             jsonb NOT NULL,
          recorded_at         timestamptz NOT NULL DEFAULT now(),
          CHECK (outcome = 'APPROVED' OR units = 0),
          CHECK (outcome = 'BLOCKED' OR (
            units > 0 AND candidate_id IS NOT NULL AND policy_sha256 IS NOT NULL
            AND state_version IS NOT NULL AND snapshot_digest IS NOT NULL
            AND cardinality(blocking_rules) = 0))
        )
        """
    )
    op.execute("CREATE INDEX decisions_as_of ON sentinel.decisions (as_of)")
    op.execute(
        """
        CREATE FUNCTION sentinel.check_decision() RETURNS trigger
        LANGUAGE plpgsql SET search_path = sentinel, pg_temp AS $$
        DECLARE active char(64);
        BEGIN
          IF NEW.outcome = 'APPROVED' THEN
            SELECT policy_sha256 INTO active
              FROM sentinel.policy_activations ORDER BY seq DESC LIMIT 1;
            IF NOT FOUND OR active IS DISTINCT FROM NEW.policy_sha256 THEN
              RAISE EXCEPTION 'approved decision % references policy % but the active policy is %',
                NEW.decision_id, NEW.policy_sha256, active;
            END IF;
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER decisions_policy_binding BEFORE INSERT ON sentinel.decisions "
        "FOR EACH ROW EXECUTE FUNCTION sentinel.check_decision()"
    )

    op.execute(
        """
        CREATE TABLE sentinel.approvals (
          approval_id      text PRIMARY KEY,
          decision_id      text NOT NULL REFERENCES sentinel.decisions (decision_id),
          candidate_id     text NOT NULL,
          authority        text NOT NULL CHECK (authority IN ('VALIDATION', 'RISK', 'PORTFOLIO')),
          state_version    bigint NOT NULL,
          snapshot_digest  char(64) NOT NULL,
          payload_sha256   char(64) NOT NULL,
          issued_at        timestamptz NOT NULL,
          expires_at       timestamptz NOT NULL,
          recorded_at      timestamptz NOT NULL DEFAULT now(),
          UNIQUE (decision_id, authority),
          CHECK (expires_at > issued_at)
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION sentinel.check_approval() RETURNS trigger
        LANGUAGE plpgsql SET search_path = sentinel, pg_temp AS $$
        DECLARE d record;
        BEGIN
          SELECT outcome, candidate_id, state_version, snapshot_digest INTO d
            FROM sentinel.decisions WHERE decision_id = NEW.decision_id;
          IF NOT FOUND OR d.outcome <> 'APPROVED' THEN
            RAISE EXCEPTION 'approval % needs an APPROVED decision', NEW.approval_id;
          END IF;
          IF d.candidate_id <> NEW.candidate_id OR d.state_version <> NEW.state_version
             OR d.snapshot_digest <> NEW.snapshot_digest THEN
            RAISE EXCEPTION 'approval % does not match its decision bindings', NEW.approval_id;
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER approvals_binding BEFORE INSERT ON sentinel.approvals "
        "FOR EACH ROW EXECUTE FUNCTION sentinel.check_approval()"
    )

    op.execute(
        """
        CREATE TABLE sentinel.shadow_trades (
          decision_id       text PRIMARY KEY REFERENCES sentinel.decisions (decision_id),
          candidate_id      text NOT NULL,
          symbol            text NOT NULL,
          side              text NOT NULL CHECK (side IN ('LONG', 'SHORT')),
          detector          text NOT NULL,
          decision_outcome  text NOT NULL CHECK (decision_outcome IN ('APPROVED', 'BLOCKED')),
          blocking_rules    text[] NOT NULL,
          policy_sha256     char(64),
          snapshot_digest   char(64),
          payload           jsonb NOT NULL,
          recorded_at       timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX shadow_trades_candidate ON sentinel.shadow_trades (candidate_id)")
    op.execute(
        """
        CREATE FUNCTION sentinel.check_shadow_trade() RETURNS trigger
        LANGUAGE plpgsql SET search_path = sentinel, pg_temp AS $$
        DECLARE o text;
        BEGIN
          SELECT outcome INTO o FROM sentinel.decisions WHERE decision_id = NEW.decision_id;
          IF NOT FOUND OR o <> NEW.decision_outcome THEN
            RAISE EXCEPTION 'shadow trade outcome does not match decision %', NEW.decision_id;
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER shadow_trades_binding BEFORE INSERT ON sentinel.shadow_trades "
        "FOR EACH ROW EXECUTE FUNCTION sentinel.check_shadow_trade()"
    )

    for table in ("decisions", "approvals", "shadow_trades"):
        _append_only(table)
        op.execute(f"GRANT SELECT ON sentinel.{table} TO {READERS}")
        op.execute(f"GRANT INSERT ON sentinel.{table} TO svc_risk")
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET LOCAL ROLE sentinel_owner")
    for table in ("shadow_trades", "approvals", "decisions"):
        op.execute(f"DROP TABLE sentinel.{table}")
    for fn in ("check_shadow_trade", "check_approval", "check_decision"):
        op.execute(f"DROP FUNCTION sentinel.{fn}()")
    op.execute("RESET ROLE")
