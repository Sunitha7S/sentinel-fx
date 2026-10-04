"""The core is usable without any infrastructure or boundary library installed or imported."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

CORE_MODULES = [
    "sentinel.domain.types",
    "sentinel.domain.canonical",
    "sentinel.domain.instrument",
    "sentinel.domain.snapshots",
    "sentinel.domain.decision",
    "sentinel.domain.system",
    "sentinel.domain.market_data",
    "sentinel.fxmath.sizing",
    "sentinel.fxmath.conversion",
    "sentinel.fxmath.pips",
    "sentinel.risk.policy",
    "sentinel.risk.kernel",
    "sentinel.risk.approvals",
    "sentinel.risk.recheck",
    "sentinel.risk.state_machine",
    "sentinel.risk.governance",
    "sentinel.audit.chain",
]

INFRASTRUCTURE = [
    "pydantic",
    "yaml",
    "fastapi",
    "starlette",
    "langgraph",
    "langchain_core",
    "redis",
    "sqlalchemy",
    "psycopg",
    "httpx",
    "requests",
    "pandas",
    "numpy",
]

SCRIPT = """
import json, sys
for name in {modules!r}:
    __import__(name)
print(json.dumps(sorted(m for m in {infra!r} if m in sys.modules)))
"""


@pytest.mark.invariant("INV-ARCH-02")
def test_core_imports_pull_in_no_infrastructure_libraries() -> None:
    code = SCRIPT.format(modules=CORE_MODULES, infra=INFRASTRUCTURE)
    out = subprocess.run(  # noqa: S603 - fixed interpreter and script
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
