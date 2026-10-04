# ADR 0009 — Audit immutability is a named invariant; the chain head is anchored

- **Status:** accepted
- **Date:** 2026-10-04

## Context
INV-AUDIT-01 tested that decisions are recorded in a hash chain. Writing the stronger
invariant, "no historical decision can be modified without breaking verification", as a
property test showed two attacks that an unkeyed hash chain cannot detect on its own:

1. **Tail truncation.** Deleting the newest *n* records leaves a valid, shorter chain.
2. **Full re-hash.** Anyone with write access can edit a record and recompute every later
   hash. The result is internally consistent.

## Decision
- New safety invariant **INV-AUDIT-IMMUTABLE**, property-tested over random chains of
  records and real risk decisions (`tests/safety/test_audit_immutability.py`):
  - edits to any field, deletions, duplications, insertions and reorders of any record
    that has a successor are detected by the chain alone;
  - any of those on *any* record, plus tail truncation, appended forgeries and full
    re-hashing, are detected when verifying against an anchored `AuditHead`
    (sequence number and hash of the last record);
  - the tests also assert that truncation and re-hashing *pass* without the anchor, so
    the limitation stays documented by the build.
- `verify_chain(records, expected_head=...)` provides the check.

## Consequences
- The anchor only helps if it is stored where the audit writer cannot rewrite it. M2 will
  store the head in PostgreSQL under a role the writer does not hold, and the verifier will
  require it. A later step can publish periodic heads outside the system (e.g. a signed or
  timestamped digest) to protect against a fully compromised host.
- Until M2 the JSONL sink re-verifies the chain on open but has no independent anchor.
