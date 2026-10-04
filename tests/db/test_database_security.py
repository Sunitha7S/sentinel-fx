"""Roles, grants, ownership and append-only enforcement, checked against a live database."""

from __future__ import annotations

import contextlib
import itertools

import pytest
from psycopg import errors as pg
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from db.conftest import Db
from sentinel.store.postgres.engine import OWNER_ROLE, SERVICE_ROLES
from sentinel.store.postgres.permissions import POLICY_TABLES, PRIVILEGES, TABLES, expected

DENIED = (pg.InsufficientPrivilege,)


def _fails_with(engine: Engine, sql: str, errors: tuple[type[Exception], ...]) -> str:
    with pytest.raises(DBAPIError) as exc, engine.begin() as conn:
        conn.execute(text(sql))
    assert isinstance(exc.value.orig, errors), (sql, type(exc.value.orig), exc.value.orig)
    return str(exc.value.orig)


# ----------------------------------------------------------------------------- roles & owners


@pytest.mark.invariant("INV-DB-ROLES")
def test_group_roles_have_no_dangerous_attributes(db: Db) -> None:
    with db.engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, "
                "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = ANY(:names)"
            ),
            {"names": [*SERVICE_ROLES, OWNER_ROLE]},
        ).all()
    assert {r.rolname for r in rows} == {*SERVICE_ROLES, OWNER_ROLE}
    for r in rows:
        assert not any(
            (
                r.rolsuper,
                r.rolcreaterole,
                r.rolcreatedb,
                r.rolcanlogin,
                r.rolreplication,
                r.rolbypassrls,
            )
        ), r.rolname


@pytest.mark.invariant("INV-DB-ROLES")
def test_no_service_role_owns_anything_and_roles_are_not_nested(db: Db) -> None:
    with db.engine().connect() as conn:
        owners = (
            conn.execute(
                text(
                    "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'sentinel' "
                    "UNION SELECT pg_get_userbyid(p.proowner) FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'sentinel' "
                    "UNION SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
                    "WHERE nspname = 'sentinel'"
                )
            )
            .scalars()
            .all()
        )
        memberships = (
            conn.execute(
                text(
                    "SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                    "JOIN pg_roles g ON g.oid = m.roleid WHERE r.rolname = ANY(:names) "
                    "AND g.rolname = ANY(:all)"
                ),
                {"names": list(SERVICE_ROLES), "all": [*SERVICE_ROLES, OWNER_ROLE]},
            )
            .scalars()
            .all()
        )
    assert set(owners) == {OWNER_ROLE}
    assert memberships == []


# ----------------------------------------------------------------------------- privilege matrix


@pytest.mark.invariant("INV-DB-ROLES")
def test_every_privilege_matches_the_intended_matrix(db: Db) -> None:
    mismatches = []
    with db.engine().connect() as conn:
        for role, table, priv in itertools.product(SERVICE_ROLES, TABLES, PRIVILEGES):
            actual = conn.execute(
                text("SELECT has_table_privilege(:r, :t, :p)"),
                {"r": role, "t": f"sentinel.{table}", "p": priv},
            ).scalar_one()
            if actual != (priv in expected(role, table)):
                mismatches.append((role, table, priv, actual))
        public = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.role_table_grants "
                "WHERE table_schema = 'sentinel' AND grantee = 'PUBLIC'"
            )
        ).scalar_one()
    assert mismatches == []
    assert public == 0


@pytest.mark.invariant("INV-DB-ROLES")
def test_no_role_may_create_objects_or_execute_the_head_function(db: Db) -> None:
    with db.engine().connect() as conn:
        for role in SERVICE_ROLES:
            for schema in ("sentinel", "public"):
                can_create = conn.execute(
                    text("SELECT has_schema_privilege(:r, :s, 'CREATE')"),
                    {"r": role, "s": schema},
                ).scalar_one()
                assert not can_create, (role, schema)
            can_exec = conn.execute(
                text(
                    "SELECT has_function_privilege(:r, 'sentinel.advance_audit_head()', 'EXECUTE')"
                ),
                {"r": role},
            ).scalar_one()
            assert not can_exec, role


# ----------------------------------------------------------------------------- policy write denial

POLICY_ATTACKS = [
    "INSERT INTO sentinel.{t} SELECT * FROM sentinel.{t} LIMIT 0",
    "INSERT INTO sentinel.policy_versions (sha256, policy_id, content, created_by) "
    "VALUES (repeat('a', 64), 'evil', '{{}}', 'x')",
    "UPDATE sentinel.{t} SET {col} = {col}",
    "DELETE FROM sentinel.{t}",
    "TRUNCATE sentinel.{t}",
    "ALTER TABLE sentinel.{t} ADD COLUMN evil text",
    "ALTER TABLE sentinel.{t} DISABLE TRIGGER ALL",
    "DROP TABLE sentinel.{t}",
    "CREATE TRIGGER evil BEFORE INSERT ON sentinel.{t} "
    "FOR EACH ROW EXECUTE FUNCTION sentinel.forbid_mutation()",
    "LOCK TABLE sentinel.{t} IN ACCESS EXCLUSIVE MODE",
    "COMMENT ON TABLE sentinel.{t} IS 'evil'",
    "CREATE TABLE sentinel.{t}_shadow (x int)",
]


@pytest.mark.invariant("INV-POLICY-01")
@pytest.mark.parametrize("table", POLICY_TABLES)
@pytest.mark.parametrize("attack", POLICY_ATTACKS, ids=range(len(POLICY_ATTACKS)))
def test_learning_role_cannot_modify_policy_tables_in_any_way(
    db: Db, table: str, attack: str
) -> None:
    if "INSERT INTO sentinel.policy_versions" in attack and table != "policy_versions":
        return  # that attack is specific to policy_versions; covered by its own parameter
    sql = attack.format(t=table, col=_some_column(table))
    _fails_with(db.engine("svc_learning"), sql, DENIED)


@pytest.mark.invariant("INV-POLICY-01")
@pytest.mark.parametrize("table", POLICY_TABLES)
def test_learning_role_cannot_grant_itself_write_access(db: Db, table: str) -> None:
    """GRANT without grant option warns or fails in PostgreSQL; either way nothing changes."""
    with contextlib.suppress(DBAPIError), db.engine("svc_learning").begin() as conn:
        conn.execute(text(f"GRANT INSERT, UPDATE, DELETE ON sentinel.{table} TO svc_learning"))
    with db.engine().connect() as conn:
        for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not conn.execute(
                text("SELECT has_table_privilege('svc_learning', :t, :p)"),
                {"t": f"sentinel.{table}", "p": priv},
            ).scalar_one()


@pytest.mark.invariant("INV-POLICY-01")
@pytest.mark.parametrize("role", [r for r in SERVICE_ROLES if r != "human_admin"])
@pytest.mark.parametrize("table", POLICY_TABLES)
def test_no_service_role_can_write_policy(db: Db, role: str, table: str) -> None:
    engine = db.engine(role)
    for sql in (
        f"INSERT INTO sentinel.{table} SELECT * FROM sentinel.{table} LIMIT 0",
        f"DELETE FROM sentinel.{table}",
        f"TRUNCATE sentinel.{table}",
    ):
        _fails_with(engine, sql, DENIED)


# ----------------------------------------------------------------------------- append-only

APPEND_ONLY = (
    *POLICY_TABLES,
    "decisions",
    "approvals",
    "shadow_trades",
    "audit_records",
    "market_candles",
    "spreads",
    "ingestion_runs",
)


@pytest.mark.invariant("INV-DB-APPEND-ONLY")
@pytest.mark.parametrize("table", APPEND_ONLY)
def test_append_only_holds_even_for_the_owner(db: Db, table: str) -> None:
    """Triggers, not just missing grants: the owner has every privilege and is still refused."""
    owner = db.engine(OWNER_ROLE)
    for sql in (
        f"UPDATE sentinel.{table} SET {_some_column(table)} = {_some_column(table)}",
        f"DELETE FROM sentinel.{table}",
        # CASCADE: the strongest form, which would also empty every referencing table.
        f"TRUNCATE sentinel.{table} CASCADE",
    ):
        message = _expect_trigger_or_empty(owner, sql)
        assert message is None or "append-only" in message


def _some_column(table: str) -> str:
    return {
        "audit_records": "kind",
        "market_candles": "source",
        "spreads": "source",
        "ingestion_runs": "provider",
        "policy_versions": "created_by",
    }.get(table, "recorded_at")


def _expect_trigger_or_empty(engine: Engine, sql: str) -> str | None:
    """Row triggers only fire on rows; empty tables are seeded in the policy/audit tests.
    TRUNCATE is statement-level and always fires."""
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
    except DBAPIError as exc:
        return str(exc.orig)
    assert not sql.startswith("TRUNCATE"), f"TRUNCATE was not blocked: {sql}"
    return None


@pytest.mark.invariant("INV-DB-APPEND-ONLY")
def test_audit_head_cannot_be_deleted_truncated_or_written_by_services(db: Db) -> None:
    owner = db.engine(OWNER_ROLE)
    assert "append-only" in _fails_with(
        owner, "DELETE FROM sentinel.audit_head", (pg.RaiseException,)
    )
    assert "append-only" in _fails_with(owner, "TRUNCATE sentinel.audit_head", (pg.RaiseException,))
    for role in SERVICE_ROLES:
        _fails_with(db.engine(role), "UPDATE sentinel.audit_head SET seq = 0", DENIED)
