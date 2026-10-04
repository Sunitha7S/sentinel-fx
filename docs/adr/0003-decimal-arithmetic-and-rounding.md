# ADR 0003 — Decimal arithmetic and conservative rounding

- **Status:** accepted
- **Date:** 2026-10-04

## Context
Position sizing is where a small numerical error becomes a real loss. Binary floats
cannot represent 0.1, and silent conversions hide bugs.

## Decision
1. Money, prices, sizes and percentages are `Decimal`. `to_decimal` **rejects** `float` and
   `bool` with `TypeError` rather than converting. Boundary schemas reject floats too, so
   JSON carries decimals as strings. The YAML policy loader overrides PyYAML's float
   constructor so `0.50` becomes `Decimal("0.50")` from the literal text.
2. Percentages are a `Percent` type in percent units, with an explicit `.fraction`. This
   removes the 0.5 versus 0.005 ambiguity.
3. Sizing rounds every intermediate step in the conservative direction:
   - loss per unit uses `ROUND_CEILING`, so risk is never under-estimated;
   - size uses `ROUND_FLOOR` to the broker's unit step;
   - a final guard removes one step while `units × loss_per_unit > budget`;
   - below the broker minimum, the result is zero units and a block. It is never rounded up.
4. Arithmetic runs at 50 significant digits inside `localcontext`.
5. Policy hashes are computed over a canonical encoding (sorted keys, normalised decimals),
   so `0.50` and `0.5` hash identically.

## Consequences
Property tests assert `risk_amount <= equity × fraction` exactly in `Decimal`, with no
tolerance. Test builders round reference equity values *up* to whole cents, so a test can
never show less loss than it intends.
