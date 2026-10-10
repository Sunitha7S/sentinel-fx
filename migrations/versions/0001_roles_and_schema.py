"""Roles, the sentinel schema, and the append-only guard.

Revision ID: 0001
Revises:
Create Date: 2026-10-04

Roles are cluster-wide in PostgreSQL, so they are created idempotently and are not dropped
on downgrade (other databases on the cluster may use them). All roles are NOLOGIN group
roles; deployments create LOGIN roles that are members of exactly the group they need.
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

OWNER = "sentinel_owner"
SERVICE_ROLES = (
    "human_admin",
    "svc_risk",
    "svc_learning",
    "svc_audit",
    "svc_execution",
    "svc_market_data",
)


def upgrade() -> None:
    for role in (OWNER, *SERVICE_ROLES):
        op.execute(
            f"""
            DO $$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                  NOREPLICATION NOBYPASSRLS;
              END IF;
            END $$;
            """
        )
    # Nobody gets to create objects in the public schema of this database.
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute(f"CREATE SCHEMA sentinel AUTHORIZATION {OWNER}")
    op.execute("REVOKE ALL ON SCHEMA sentinel FROM PUBLIC")
    op.execute(f"GRANT USAGE ON SCHEMA sentinel TO {', '.join(SERVICE_ROLES)}")

    op.execute(f"SET LOCAL ROLE {OWNER}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA sentinel "
        "REVOKE ALL ON TABLES FROM PUBLIC"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA sentinel "
        "REVOKE ALL ON FUNCTIONS FROM PUBLIC"
    )
    op.execute(
        """
        CREATE FUNCTION sentinel.forbid_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'append-only table sentinel.%: % is not allowed', TG_TABLE_NAME, TG_OP
            USING ERRCODE = 'P0001';
        END $$
        """
    )
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("DROP SCHEMA sentinel CASCADE")
    op.execute("RESET ROLE")
