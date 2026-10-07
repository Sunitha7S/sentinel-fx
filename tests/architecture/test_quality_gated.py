"""INV-DATA-QUALITY-GATED, structurally: one way in, no way around.

* Only the snapshot store attests (and only ``freeze`` calls it), and only the dataset module
  holds the issuer keys of reports and attestations.
* ``freeze``, ``load`` and ``issue_verified_dataset`` take exactly the arguments they need:
  no flag can skip, force or override the quality gates, anywhere in the code that judges,
  attests, freezes or verifies.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from sentinel.domain.dataset import issue_verified_dataset, judge_dataset
from sentinel.store.postgres.dataset_store import PostgresDatasetStore

SRC = Path(__file__).resolve().parents[2] / "src"
DATASET = Path("sentinel/domain/dataset.py")
STORE = Path("sentinel/store/postgres/dataset_store.py")
GATED = (
    DATASET,
    STORE,
    Path("sentinel/domain/quality.py"),
    Path("sentinel/config/quality_loader.py"),
)
BYPASS = re.compile(r"(force|override|bypass|skip|unsafe|ignore|allow_fail|unchecked|lenient)")
ATTEST_CALL = re.compile(r"\battest\(")


def _bypass_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            a = node.args
            names = [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
            names += [x.arg for x in (a.vararg, a.kwarg) if x is not None]
            names.append(node.name)
        elif isinstance(node, ast.keyword) and node.arg:
            names = [node.arg]
        elif isinstance(node, ast.Name):
            names = [node.id]
        found += [f"{path.name}:{node.lineno} {n}" for n in names if BYPASS.search(n.lower())]  # type: ignore[attr-defined]
    return found


def _code_without_docstrings(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0] = ast.Pass()
    return ast.unparse(tree)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_only_the_snapshot_store_attests_and_only_dataset_holds_the_keys() -> None:
    offenders = []
    for path in sorted((SRC / "sentinel").rglob("*.py")):
        rel = path.relative_to(SRC)
        code = _code_without_docstrings(path)
        if rel not in (DATASET, STORE) and ATTEST_CALL.search(code):
            offenders.append(f"{rel} calls attest()")
        if rel != DATASET and ("_ATTESTATION_KEY" in code or "_REPORT_KEY" in code):
            offenders.append(f"{rel} references an issuer key")
    assert offenders == []
    store = _code_without_docstrings(SRC / STORE)
    assert len(ATTEST_CALL.findall(store)) == 1  # in freeze, after judging under the lock


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_no_entry_point_accepts_anything_that_could_bypass_the_gates() -> None:
    def params(fn: object) -> list[str]:
        return list(inspect.signature(fn).parameters)  # type: ignore[arg-type]

    assert params(PostgresDatasetStore.freeze) == ["self", "spec", "quality"]
    assert params(PostgresDatasetStore.load) == ["self", "snapshot_id"]
    assert params(issue_verified_dataset) == ["record", "candles", "quality"]
    assert params(judge_dataset) == ["spec", "config", "as_of", "candles"]
    assert [p for path in GATED for p in _bypass_names(SRC / path)] == []


def test_the_scanners_detect_violations(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def freeze(spec, quality, force=False): ...\n"
        "def skip_gates(): ...\n"
        "store.load(x, override=True)\n"
        "def f():\n    '''attest() in a docstring is fine'''\n    return attest(report)\n",
        encoding="utf-8",
    )
    assert len(_bypass_names(bad)) == 3
    assert len(ATTEST_CALL.findall(_code_without_docstrings(bad))) == 1
