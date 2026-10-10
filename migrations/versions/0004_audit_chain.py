"""Audit chain and its separately anchored head (INV-AUDIT-IMMUTABLE, ADR 0009).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-04

* ``audit_records`` is append-only. ``seq`` and ``prev_hash`` are unique, and the insert
  trigger requires each record to extend the current head exactly, so the chain cannot
  fork even with concurrent writers.
* ``audit_head`` holds the last record's ``seq`` and ``hash``. Only the trigger function,
  which runs as ``sentinel_owner`` (SECURITY DEFINER), can change it. ``svc_audit`` can
  append records but has no write privilege on the head, so a writer cannot silently
  truncate or re-hash the log and move the anchor to match.
* ``payload`` is stored as the exact canonical JSON text that was hashed (not jsonb, which
  would normalise it), so verification recomputes byte-identical hashes.
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

GENESIS = "0" * 64


def upgrade() -> None:
    op.execute("SET LOCAL ROLE sentinel_owner")
    op.execute(
        """
        CREATE TABLE sentinel.audit_records (
          seq          bigint PRIMARY KEY CHECK (seq >= 1),
          recorded_at  timestamptz NOT NULL,
          kind         text NOT NULL,
          subject_id   text NOT NULL,
          payload      text NOT NULL,
          prev_hash    char(64) NOT NULL UNIQUE CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
          hash         char(64) NOT NULL UNIQUE CHECK (hash ~ '^[0-9a-f]{64}$'),
          inserted_at  timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE sentinel.audit_head (
          singleton   boolean PRIMARY KEY DEFAULT true CHECK (singleton),
          seq         bigint NOT NULL CHECK (seq >= 0),
          hash        char(64) NOT NULL CHECK (hash ~ '^[0-9a-f]{{64}}$'),
          updated_at  timestamptz NOT NULL DEFAULT now()
        );
        INSERT INTO sentinel.audit_head (singleton, seq, hash) VALUES (true, 0, '{GENESIS}')
        """
    )
    op.execute(
        """
        CREATE FUNCTION sentinel.advance_audit_head() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = sentinel, pg_temp AS $$
        DECLARE
          head_seq bigint;
          head_hash char(64);
        BEGIN
          SELECT seq, hash INTO head_seq, head_hash
            FROM sentinel.audit_head WHERE singleton FOR UPDATE;
          IF NEW.seq <> head_seq + 1 THEN
            RAISE EXCEPTION 'audit record % does not follow head %', NEW.seq, head_seq;
          END IF;
          IF NEW.prev_hash <> head_hash THEN
            RAISE EXCEPTION 'audit record % does not extend the anchored head', NEW.seq;
          END IF;
          UPDATE sentinel.audit_head SET seq = NEW.seq, hash = NEW.hash, updated_at = now()
            WHERE singleton;
          RETURN NEW;
        END $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION sentinel.advance_audit_head() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER audit_records_extend_head BEFORE INSERT ON sentinel.audit_records "
        "FOR EACH ROW EXECUTE FUNCTION sentinel.advance_audit_head()"
    )
    for table in ("audit_records",):
        op.execute(
            f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE ON sentinel.{table} "
            "FOR EACH ROW EXECUTE FUNCTION sentinel.forbid_mutation()"
        )
    # The head may only move forward through the trigger; it is never deleted or truncated.
    op.execute(
        "CREATE TRIGGER audit_head_no_delete BEFORE DELETE ON sentinel.audit_head "
        "FOR EACH ROW EXECUTE FUNCTION sentinel.forbid_mutation()"
    )
    for table in ("audit_records", "audit_head"):
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON sentinel.{table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION sentinel.forbid_mutation()"
        )
    op.execute("GRANT SELECT, INSERT ON sentinel.audit_records TO svc_audit")
    op.execute("GRANT SELECT ON sentinel.audit_head TO svc_audit")
    op.execute("GRANT SELECT ON sentinel.audit_records, sentinel.audit_head TO human_admin")
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET LOCAL ROLE sentinel_owner")
    op.execute("DROP TABLE sentinel.audit_records")
    op.execute("DROP TABLE sentinel.audit_head")
    op.execute("DROP FUNCTION sentinel.advance_audit_head()")
    op.execute("RESET ROLE")
