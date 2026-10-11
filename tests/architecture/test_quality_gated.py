"""INV-DATA-QUALITY-GATED, structurally: one way in, no way around.

* Only the snapshot store attests (and only ``freeze`` calls it), and only the dataset module
  holds the issuer keys of reports and attestations.
* ``freeze``, ``load`` and ``issue_verified_dataset`` take exactly the arguments they need:
  no flag can skip, force or override the quality gates, anywhere in the code that judges,
  attests, freezes or verifies.
* Trust has one origin (ADR 0015): only the loader creates a ``QualityConfigRegistry`` or
  holds its key, production code loads the registry only from the shipped directory (a
  direct call without arguments; no alias, ``partial`` or ``getattr``), no guarded class is
  subclassed or allocated without its constructor, and the snapshot store takes a registry,
  never a caller-supplied mapping of configurations.

These scans are a second line of defence that catches convenient bypasses in review. They
cannot prove the absence of a deliberate one (string building, ``exec``); the runtime checks
in the loader, the dataset module and the store are authoritative.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from sentinel.domain.dataset import attest, issue_verified_dataset, judge_dataset
from sentinel.store.postgres.dataset_store import PostgresDatasetStore

SRC = Path(__file__).resolve().parents[2] / "src"
DATASET = Path("sentinel/domain/dataset.py")
STORE = Path("sentinel/store/postgres/dataset_store.py")
LOADER = Path("sentinel/config/quality_loader.py")
GATED = (
    DATASET,
    STORE,
    Path("sentinel/domain/quality.py"),
    LOADER,
)
PRIVATE: dict[str, Path] = {
    "_REGISTRY_KEY": LOADER,
    "_sealed": LOADER,
    "_load_approvals": LOADER,
    "_match_approvals": LOADER,
    "_REPORT_KEY": DATASET,
    "_ATTESTATION_KEY": DATASET,
}
"""Private issuance mechanisms and the only module that may name them."""
GUARDED = frozenset(
    {"QualityConfigRegistry", "QualityConfig", "QualityReport", "QualityAttestation"}
)
REGISTRY_LOADER = "load_quality_registry"
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


def _ident(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text) is not None


def _boundary_violations(path: Path, rel: Path) -> list[str]:  # noqa: PLR0912 - one rule each
    """Ways production code could forge, copy, widen or redirect trust in quality config."""
    tree = ast.parse(_code_without_docstrings(path))
    found: list[str] = []

    def flag(node: ast.AST, what: str) -> None:
        found.append(f"{rel}:{getattr(node, 'lineno', '?')} {what}")

    bare_calls = {
        id(n.func)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and not n.args and not n.keywords
    }
    for node in ast.walk(tree):
        texts: list[str] = []
        if isinstance(node, ast.Name | ast.Attribute):
            texts = [_ident(node)]
        elif isinstance(node, ast.alias):
            texts = [node.name, node.asname or ""]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            texts = [node.value]  # getattr(module, "_REGISTRY_KEY") and the like
        for private, owner in PRIVATE.items():
            if rel != owner and any(_mentions(t, private) for t in texts):
                flag(node, f"references {private}")
        if rel != LOADER:
            if _ident(node) == REGISTRY_LOADER and id(node) not in bare_calls:
                flag(node, f"{REGISTRY_LOADER} used other than as a call without arguments")
            if isinstance(node, ast.alias) and node.name == REGISTRY_LOADER and node.asname:
                flag(node, f"{REGISTRY_LOADER} imported under an alias")
            if isinstance(node, ast.Constant) and node.value == REGISTRY_LOADER:
                flag(node, f"{REGISTRY_LOADER} named as a string")
            if isinstance(node, ast.Call) and _ident(node.func) == "QualityConfigRegistry":
                flag(node, "builds a QualityConfigRegistry")
        if isinstance(node, ast.ClassDef) and any(_ident(b) in GUARDED for b in node.bases):
            flag(node, f"subclasses a guarded class ({node.name})")
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "__new__"
            and (_ident(node.value) in GUARDED)
        ):
            flag(node, f"allocates {_ident(node.value)} without its constructor")
        if (
            isinstance(node, ast.Call)
            and _ident(node.func) == "__new__"
            and any(_ident(a) in GUARDED for a in node.args)
        ):
            flag(node, "allocates a guarded class without its constructor")
        if isinstance(node, ast.Call) and _ident(node.func) == "type" and len(node.args) == 3:
            flag(node, "creates a class dynamically")
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
    assert params(attest) == ["spec", "config", "as_of", "candles"]  # rows, never a report
    assert [p for path in GATED for p in _bypass_names(SRC / path)] == []


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_trust_comes_only_from_the_registry_loaded_from_the_shipped_directory() -> None:
    offenders = []
    for path in sorted((SRC / "sentinel").rglob("*.py")):
        offenders += _boundary_violations(path, path.relative_to(SRC))
    assert offenders == []
    assert list(inspect.signature(PostgresDatasetStore.__init__).parameters) == [
        "self",
        "engine",
        "registry",
        "max_rows",
    ]


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


EVASIONS = {
    "direct call with a directory": "load_quality_registry(Path('elsewhere'))\n",
    "keyword directory": "loader.load_quality_registry(directory=d)\n",
    "alias by assignment": "f = loader.load_quality_registry\nf(Path('x'))\n",
    "alias by import": "from sentinel.config.quality_loader import load_quality_registry as lq\n",
    "partial": "functools.partial(load_quality_registry, Path('x'))()\n",
    "getattr of the loader": "getattr(loader, 'load_quality_registry')(d)\n",
    "key by name": "from sentinel.config.quality_loader import _REGISTRY_KEY\n",
    "key by attribute": "k = ql._REGISTRY_KEY\n",
    "key by getattr": "k = getattr(ql, '_REGISTRY_KEY')\n",
    "seal forged": "object.__setattr__(r, '_sealed', k)\n",
    "private matcher": "ql._match_approvals(configs, records)\n",
    "report key": "getattr(ds, '_REPORT_KEY')\n",
    "attestation key": "ds._ATTESTATION_KEY\n",
    "registry built": "ql.QualityConfigRegistry({}, frozenset(), _key=k)\n",
    "registry subclass": "class Evil(ql.QualityConfigRegistry):\n    pass\n",
    "config subclass": "class Fake(QualityConfig):\n    pass\n",
    "attestation subclass": "class A(dataset.QualityAttestation):\n    pass\n",
    "allocation without constructor": "r = QualityConfigRegistry.__new__(QualityConfigRegistry)\n",
    "dynamic subclass": "Evil = type('Evil', (QualityConfigRegistry,), {})\n",
    "object.__new__ of a config": "c = object.__new__(QualityConfig)\n",
    "object.__new__ of a registry": "r = object.__new__(ql.QualityConfigRegistry)\n",
}
ALLOWED = (
    "from sentinel.config.quality_loader import QualityConfigRegistry, load_quality_registry\n"
    "registry = load_quality_registry()\n"
    "registry2 = quality_loader.load_quality_registry()\n"
    "def f(r: QualityConfigRegistry) -> bool:\n"
    "    return type(r) is QualityConfigRegistry\n"
    'def g():\n    "load_quality_registry(d) and _REGISTRY_KEY in a docstring are fine"\n'
    "SQL = 'market_candles_sealed_range'\n"
)


@pytest.mark.parametrize("snippet", list(EVASIONS.values()), ids=list(EVASIONS))
def test_the_boundary_scanner_detects_each_evasion(tmp_path: Path, snippet: str) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(snippet, encoding="utf-8")
    assert _boundary_violations(probe, Path("sentinel/other.py")) != []


def test_the_boundary_scanner_allows_legitimate_use(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(ALLOWED, encoding="utf-8")
    assert _boundary_violations(probe, Path("sentinel/other.py")) == []
    keyed = tmp_path / "keyed.py"
    keyed.write_text("QualityConfigRegistry(c, a, _key=_REGISTRY_KEY)\n", encoding="utf-8")
    assert _boundary_violations(keyed, LOADER) == []  # the loader issues its own registry
    assert _boundary_violations(keyed, Path("sentinel/other.py")) != []
