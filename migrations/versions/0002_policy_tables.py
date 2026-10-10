"""Policy versions and activations (INV-POLICY-IMMUTABLE).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-04

* ``policy_versions``: content-addressed, immutable. The primary key is the SHA-256 of the
  canonical policy, and ``content`` is ``RiskPolicy.to_mapping()``; the application
  re-parses and re-hashes it on every load.
* ``policy_activations``: append-only log. The active policy is the row with the highest
  ``seq``. The database enforces a linear history (each activation names the policy it
  replaces), the 24-hour cooling-off for anything that is not pure tightening, and that a
  row is only recorded once it is effective.
* Only ``human_admin`` may insert; no role may update, delete or truncate.
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

READERS = "human_admin, svc_risk, svc_learning, svc_audit, svc_execution"


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
        CREATE TABLE sentinel.policy_versions (
          sha256         char(64) PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
          policy_id      text NOT NULL UNIQUE CHECK (length(btrim(policy_id)) > 0),
          content        jsonb NOT NULL,
          parent_sha256  char(64) REFERENCES sentinel.policy_versions (sha256),
          created_by     text NOT NULL CHECK (length(btrim(created_by)) > 0),
          created_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE sentinel.policy_activations (
          seq              bigint PRIMARY KEY CHECK (seq >= 1),
          policy_sha256    char(64) NOT NULL REFERENCES sentinel.policy_versions (sha256),
          based_on_sha256  char(64) REFERENCES sentinel.policy_versions (sha256),
          direction        text NOT NULL
                           CHECK (direction IN ('INITIAL', 'TIGHTEN', 'LOOSEN', 'MIXED')),
          request_id       text NOT NULL UNIQUE,
          reason           text NOT NULL CHECK (length(btrim(reason)) > 0),
          approved_by      text NOT NULL CHECK (length(btrim(approved_by)) > 0),
          approved_at      timestamptz NOT NULL,
          effective_at     timestamptz NOT NULL,
          recorded_at      timestamptz NOT NULL DEFAULT now(),
          CHECK ((seq = 1) = (direction = 'INITIAL')),
          CHECK ((seq = 1) = (based_on_sha256 IS NULL)),
          CHECK (effective_at >= approved_at),
          CHECK (direction IN ('INITIAL', 'TIGHTEN')
                 OR effective_at >= approved_at + interval '24 hours')
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION sentinel.check_policy_activation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = sentinel, pg_temp AS $$
        DECLARE
          tip_seq bigint;
          tip_sha char(64);
        BEGIN
          PERFORM pg_advisory_xact_lock(hashtext('sentinel.policy_activations'));
          SELECT seq, policy_sha256 INTO tip_seq, tip_sha
            FROM sentinel.policy_activations ORDER BY seq DESC LIMIT 1;
          IF NOT FOUND THEN
            IF NEW.seq <> 1 THEN
              RAISE EXCEPTION 'the first policy activation must have seq 1';
            END IF;
          ELSE
            IF NEW.seq <> tip_seq + 1 THEN
              RAISE EXCEPTION 'policy activation seq % does not follow %', NEW.seq, tip_seq;
            END IF;
            IF NEW.based_on_sha256 IS DISTINCT FROM tip_sha THEN
              RAISE EXCEPTION 'activation is based on % but the active policy is %',
                NEW.based_on_sha256, tip_sha;
            END IF;
            IF NEW.policy_sha256 = tip_sha THEN
              RAISE EXCEPTION 'policy % is already active', tip_sha;
            END IF;
          END IF;
          IF NEW.effective_at > now() THEN
            RAISE EXCEPTION 'an activation can only be recorded once it is effective (%)',
              NEW.effective_at;
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER policy_activations_linear BEFORE INSERT ON sentinel.policy_activations "
        "FOR EACH ROW EXECUTE FUNCTION sentinel.check_policy_activation()"
    )
    for table in ("policy_versions", "policy_activations"):
        _append_only(table)
        op.execute(f"GRANT SELECT ON sentinel.{table} TO {READERS}")
        op.execute(f"GRANT INSERT ON sentinel.{table} TO human_admin")
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET LOCAL ROLE sentinel_owner")
    op.execute("DROP TABLE sentinel.policy_activations")
    op.execute("DROP TABLE sentinel.policy_versions")
    op.execute("DROP FUNCTION sentinel.check_policy_activation()")
    op.execute("RESET ROLE")
