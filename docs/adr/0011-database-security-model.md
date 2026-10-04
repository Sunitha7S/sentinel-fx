# ADR 0011 — Database security model: roles, grants, append-only, and its limits

- **Status:** accepted
- **Date:** 2026-10-04
- **Supersedes:** the role list in docs/02 §9 (`svc_perception`, `svc_decision`, `svc_api`,
  `human_risk_admin`).

## Roles
All are `NOLOGIN` group roles with no SUPERUSER, CREATEROLE, CREATEDB, REPLICATION or
BYPASSRLS attribute, and none is a member of another.

| Role | Purpose | Writes (INSERT only) |
|---|---|---|
| `sentinel_owner` | Owns the schema, tables and functions; used only by migrations | — |
| `human_admin` | Policy governance after step-up authentication | `policy_versions`, `policy_activations` |
| `svc_risk` | Risk gate / decision service | `decisions`, `approvals`, `shadow_trades` |
| `svc_audit` | Audit writer | `audit_records` |
| `svc_execution` | Execution service (M10) | — (read-only in M2) |
| `svc_learning` | Offline research | — (read-only) |
| `svc_market_data` | Historical and live market-data ingestion | `market_candles`, `spreads`, `ingestion_runs` |

The full matrix is `sentinel.store.postgres.permissions.EXPECTED`. Tests compare it with
`has_table_privilege` for every role × table × privilege, so an extra grant fails CI.

Deployments create LOGIN roles that are members of exactly one group. A service that
needs two (e.g. decisions and audit) gets a login that is a member of both, and pins each
connection to one group with `SET ROLE` (`role_engine(url, role)`).

## Guarantees (enforced and tested)
1. **No service role owns anything.** Owners bypass grants and can disable triggers.
2. **No role has UPDATE, DELETE, TRUNCATE, REFERENCES or TRIGGER on any table**, and none
   may create objects in `sentinel` or `public`.
3. **Append-only triggers** refuse UPDATE, DELETE and TRUNCATE (including
   `TRUNCATE ... CASCADE`) on every append-only table, **even for the owner**, so a
   mistaken grant would still not allow history to change.
4. **Policy:** only `human_admin` inserts. Activations form a linear history (each names
   the policy it replaces; enforced by trigger with an advisory lock). Anything that is
   not pure tightening must be recorded at least 24 h after approval (CHECK constraint).
   An APPROVED decision must reference the currently active policy hash (trigger).
5. **Audit:** `svc_audit` appends; the chain head lives in `audit_head`, which only a
   `SECURITY DEFINER` trigger owned by `sentinel_owner` can advance. Each insert must
   extend the head exactly (`seq + 1`, `prev_hash = head.hash`), so the chain cannot fork.

## What this does not protect against
**These protections defend against application bugs and misuse of service roles. They do
not defend against a PostgreSQL superuser**, the owner role, or anyone with access to the
database files. A superuser can disable triggers, edit rows, and move the audit head.
What remains in that case:
- the application recomputes every audit hash and every policy hash on load, so edits that
  do not also rewrite the hashes are still detected;
- a coordinated rewrite of records *and* head is not detectable from inside the database.
  **External anchoring** (periodically publishing the head hash outside the system, e.g. a
  signed or timestamped digest) is future work, tracked in ADR 0009.

Operationally: superuser credentials are not used by any service, and migrations run as a
separate, rarely used role.
