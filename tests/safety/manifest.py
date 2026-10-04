"""Registry of safety invariants.

Every invariant listed here must be enforced by at least one test decorated with
``@pytest.mark.invariant("<ID>")``. ``test_manifest.py`` fails the build when an
invariant has no test or a test references an unknown ID. Removing an entry from
this file is a deliberate, reviewable act; it should be accompanied by an ADR.
"""

from __future__ import annotations

REQUIRED_INVARIANTS: dict[str, str] = {
    "INV-ARCH-01": "Core packages import only the standard library and allowed internals.",
}
