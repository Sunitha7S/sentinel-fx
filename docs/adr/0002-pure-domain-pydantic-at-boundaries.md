# ADR 0002 — Pure standard-library domain; Pydantic only at boundaries

- **Status:** accepted
- **Date:** 2026-10-04
- **Deviates from:** docs/01 §3 and docs/08 §1, which put Pydantic models in `domain/`.

## Context
The risk kernel must be usable, testable and auditable without any framework. If Pydantic
models were the domain types, every core module would depend on a third-party validation
library whose coercion rules (for example float to Decimal in lax mode) are not ours to
control.

## Decision
- `sentinel.domain`, `sentinel.fxmath`, `sentinel.risk` and `sentinel.audit` use only the
  standard library: frozen `dataclasses` with validation in `__post_init__`.
- `sentinel.schemas` holds Pydantic models for messages crossing a process or storage
  boundary (`*Msg`, the `Envelope`, the LLM `NewsClassification`). Each converts to and
  from its domain type; converting into the domain re-runs domain validation.
- Enforced three ways: an allow-list AST test (`tests/architecture/test_import_rules.py`),
  a subprocess test that core imports load no infrastructure library
  (`test_domain_standalone.py`), and import-linter contracts.

## Consequences
- Two representations per exchanged type. Round-trip tests in `tests/unit/schemas` keep them in step.
- The kernel can be reused unchanged in the backtester (M4) and the execution re-check (M10).
- One exception to "standard library only": `zoneinfo` reads the `tzdata` package's data
  files on Windows. That is a read of static data, not a dependency in behaviour.
