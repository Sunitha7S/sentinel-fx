"""INV-DATA-VERIFIED-ONLY: research code reaches historical data only as a VerifiedDataset.

Three layers, each independent of the others:

1. import-linter (pyproject) forbids research packages from importing the store, the raw
   market-data providers, configuration and the CLI;
2. this AST test forbids them from importing database or HTTP libraries directly, and forbids
   pure-research entry points from accepting raw candle collections;
3. ``VerifiedDataset`` can only be issued by ``issue_verified_dataset``, which only the
   snapshot store calls (checked here), after recomputing the digest.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sentinel.domain.dataset import VerifiedDataset

SRC = Path(__file__).resolve().parents[2] / "src" / "sentinel"
RESEARCH = ("backtest", "indicators", "strategy", "learning")
# Pure research: never fed live bars, so every entry point takes a VerifiedDataset.
VERIFIED_ONLY_ENTRY_POINTS = ("backtest", "learning")
RAW_ACCESS = (
    "sentinel.store",
    "sentinel.perception.market_data",
    "sentinel.config",
    "sentinel.cli",
    "sqlalchemy",
    "psycopg",
    "psycopg2",
    "asyncpg",
    "alembic",
    "httpx",
    "requests",
    "urllib.request",
    "http.client",
    "socket",
)
ISSUERS = {
    Path("sentinel/domain/dataset.py"),
    Path("sentinel/store/postgres/dataset_store.py"),
}


def _files(package: str) -> list[Path]:
    root = SRC / package
    return sorted(root.rglob("*.py")) if root.exists() else []


def _imports(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, a.name) for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
            found.extend((node.lineno, f"{node.module}.{a.name}") for a in node.names)
    return found


def _raw_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = {
        line
        for line, module in _imports(tree)
        if any(module == banned or module.startswith(banned + ".") for banned in RAW_ACCESS)
    }
    return [f"{path.name}:{line} imports raw market-data access" for line in sorted(lines)]


def _raw_candle_parameters(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith(
            "_"
        ):
            args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            for arg in args:
                if arg.annotation is None:
                    continue
                annotation = ast.unparse(arg.annotation)
                if "Candle" in annotation and "VerifiedDataset" not in annotation:
                    problems.append(
                        f"{path.name}:{node.lineno} {node.name}({arg.arg}: {annotation}) "
                        "must take a VerifiedDataset"
                    )
    return problems


@pytest.mark.invariant("INV-DATA-VERIFIED-ONLY")
@pytest.mark.parametrize("package", RESEARCH)
def test_research_packages_cannot_reach_raw_data(package: str) -> None:
    assert [p for f in _files(package) for p in _raw_imports(f)] == []


@pytest.mark.invariant("INV-DATA-VERIFIED-ONLY")
@pytest.mark.parametrize("package", VERIFIED_ONLY_ENTRY_POINTS)
def test_research_entry_points_take_verified_datasets(package: str) -> None:
    assert [p for f in _files(package) for p in _raw_candle_parameters(f)] == []


@pytest.mark.invariant("INV-DATA-VERIFIED-ONLY")
def test_only_the_snapshot_store_issues_verified_datasets() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent)
        if rel in ISSUERS:
            continue
        source = path.read_text(encoding="utf-8")
        for name in ("issue_verified_dataset", "_ISSUER_KEY"):
            if name in source:
                offenders.append(f"{rel} references {name}")
    assert offenders == []


def test_the_checks_detect_violations(tmp_path: Path) -> None:
    """The scanners themselves must work, or the invariant would pass vacuously."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import sqlalchemy\n"
        "from sentinel.store.postgres import dataset_store\n"
        "from sentinel.perception.market_data.oanda import OandaProvider\n"
        "from collections.abc import Sequence\n"
        "def run(bars: Sequence[Candle]) -> None: ...\n"
        "def ok(data: VerifiedDataset) -> None: ...\n"
        "def _private(bars: list[Candle]) -> None: ...\n",
        encoding="utf-8",
    )
    assert len(_raw_imports(bad)) == 3
    assert len(_raw_candle_parameters(bad)) == 1
    assert VerifiedDataset.__name__ == "VerifiedDataset"
