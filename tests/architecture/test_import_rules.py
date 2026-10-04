"""Static import rules that keep the core free of infrastructure.

import-linter enforces layering *inside* ``sentinel``. This test enforces the
other half: core packages may import nothing outside the standard library and
an explicit allow-list of internal packages. An allow-list (rather than a list
of banned libraries) means a new third-party import in the core fails CI by
default.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "sentinel"

# package -> internal packages it may import (besides itself)
CORE_ALLOWED: dict[str, frozenset[str]] = {
    "domain": frozenset(),
    "fxmath": frozenset({"domain"}),
    "risk": frozenset({"domain", "fxmath"}),
    "audit": frozenset({"domain"}),
}


def _imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module))
    return found


def _violations(package: str, allowed_internal: frozenset[str]) -> list[str]:
    root = SRC / package
    if not root.exists():
        return []
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for lineno, module in _imports(path):
            top = module.split(".")[0]
            if top in sys.stdlib_module_names or top == "__future__":
                continue
            if top == "sentinel":
                parts = module.split(".")
                target = parts[1] if len(parts) > 1 else ""
                if target == package or target in allowed_internal:
                    continue
            problems.append(f"{path.relative_to(SRC.parent)}:{lineno} imports {module}")
    return problems


@pytest.mark.invariant("INV-ARCH-01")
@pytest.mark.parametrize(("package", "allowed"), sorted(CORE_ALLOWED.items()))
def test_core_package_imports_only_stdlib_and_allowed_internals(
    package: str, allowed: frozenset[str]
) -> None:
    assert _violations(package, allowed) == []
