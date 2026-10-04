# ADR 0001 — Record architecture decisions

- **Status:** accepted
- **Date:** 2026-10-04

## Context
The design in `docs/00`–`docs/09` fixes the intended architecture. Implementation will
discover places where the design is wrong, underspecified or needs a safer variant. Those
changes must stay visible, because the safety argument of the system depends on them.

## Decision
Any deviation from `docs/`, any change to a safety invariant, and any new dependency in a
core package (`domain`, `fxmath`, `risk`, `audit`) is recorded as an ADR in `docs/adr/`,
numbered sequentially, using `0000-template.md`. ADRs are append-only: a reversed decision
gets a new ADR that supersedes the old one.

## Consequences
- Reviewers can reconstruct why the code differs from the design without reading history.
- Removing an entry from `tests/safety/manifest.py` requires an ADR.
