from __future__ import annotations

import re
from pathlib import Path

import pytest

from safety.manifest import REQUIRED_INVARIANTS

TESTS_ROOT = Path(__file__).resolve().parents[1]
MARKER = re.compile(r"""mark\.invariant\(\s*["'](INV-[A-Z0-9-]+)["']\s*\)""")


def _referenced_ids() -> set[str]:
    found: set[str] = set()
    for path in TESTS_ROOT.rglob("test_*.py"):
        if path.name == "test_manifest.py":
            continue
        found.update(MARKER.findall(path.read_text(encoding="utf-8")))
    return found


@pytest.mark.invariant("INV-ARCH-01")
def test_every_required_invariant_has_a_test() -> None:
    missing = set(REQUIRED_INVARIANTS) - _referenced_ids()
    assert not missing, f"invariants without tests: {sorted(missing)}"


@pytest.mark.invariant("INV-ARCH-01")
def test_no_test_references_an_unknown_invariant() -> None:
    unknown = _referenced_ids() - set(REQUIRED_INVARIANTS)
    assert not unknown, f"unknown invariant ids: {sorted(unknown)}"
