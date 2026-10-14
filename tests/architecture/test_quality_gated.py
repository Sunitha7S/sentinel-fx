"""INV-DATA-QUALITY-GATED, structurally: one way in, no way around.

* Only the snapshot store attests (and only ``freeze`` calls it), and only the dataset module
  holds the issuer keys of reports and attestations.
* ``freeze``, ``load`` and ``issue_verified_dataset`` take exactly the arguments they need:
  no flag can skip, force or override the quality gates, anywhere in the code that judges,
  attests, freezes or verifies.
* Trust has one origin (ADR 0015): only the loader creates a ``QualityConfigRegistry`` or
  holds its key, production code loads the registry only from the shipped directory (a
  direct call without arguments; no alias, ``partial`` or ``getattr``) and never names the
  fixture loader, no guarded class (the snapshot store included) is subclassed or allocated
  without its constructor, and the snapshot store takes a registry, never a caller-supplied
  mapping of configurations.
* Nothing rewrites trusted state from outside: no assignment or deletion of another object's
  private attributes (``x._registry = ...``, ``setattr(x, "_engine", ...)``,
  ``object.__setattr__(x, ...)``, ``x.__dict__[...]``, ``x.__class__ = ...``), no attribute
  set by a computed name on another object, and no replacement of a security-sensitive
  method or constant (``PostgresDatasetStore.freeze = ...``, ``TRUST_ROOT_SHA256 = ...``).

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
    "_read_directory": LOADER,
    "_require_test_session": LOADER,
    "_in_test_session": LOADER,
    "_TEST_RUNNER": LOADER,
    "_trust_root": LOADER,
    "_origin": LOADER,
    "_REPORT_KEY": DATASET,
    "_ATTESTATION_KEY": DATASET,
    "_registry": STORE,
    "_verified_registry": STORE,
    "_approved_config": STORE,
    "_store_registry": STORE,
}
"""Private issuance and authorisation mechanisms and the only module that may name them."""
GUARDED = frozenset(
    {
        "QualityConfigRegistry",
        "QualityConfig",
        "QualityReport",
        "QualityAttestation",
        "PostgresDatasetStore",
    }
)
REGISTRY_LOADER = "load_quality_registry"
FIXTURE_LOADER = "load_fixture_registry"
SENSITIVE = frozenset(
    {
        "freeze",
        "load",
        "judge",
        "approved",
        "verify",
        "resolve",
        "attest",
        "issue_verified_dataset",
        REGISTRY_LOADER,
        FIXTURE_LOADER,
        "trust_root",
        "QUALITY_CONFIG_DIR",
        "TRUST_ROOT_SHA256",
        "__init_subclass__",
        "__setattr__",
        "__delattr__",
    }
)
"""Methods and constants whose replacement from outside would change what is authorised."""
SETTERS = frozenset({"setattr", "delattr", "__setattr__", "__delattr__"})
OWN = frozenset({"self", "cls"})
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


def _is_own(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id in OWN


def _targets(node: ast.AST) -> list[ast.AST]:
    """What an assignment or ``del`` statement writes, with tuple targets unpacked."""
    pending: list[ast.AST] = []
    if isinstance(node, ast.Assign | ast.Delete):
        pending = list(node.targets)
    elif isinstance(node, ast.AugAssign | ast.AnnAssign):
        pending = [node.target]
    targets: list[ast.AST] = []
    while pending:
        t = pending.pop()
        if isinstance(t, ast.Tuple | ast.List):
            pending += t.elts
        else:
            targets.append(t)
    return targets


def _rewrites(node: ast.AST) -> list[str]:
    """Ways ``node`` rewrites another object's private state or a sensitive name."""
    found: list[str] = []
    for t in _targets(node):
        if isinstance(t, ast.Attribute) and not _is_own(t.value):
            if t.attr.startswith("_"):
                found.append(f"assigns or deletes the private attribute {t.attr}")
            if t.attr in SENSITIVE:
                found.append(f"replaces the security-sensitive {t.attr}")
        if isinstance(t, ast.Subscript) and _ident(t.value) == "__dict__":
            found.append("writes an instance dictionary")
    if isinstance(node, ast.Call) and _ident(node.func) in SETTERS and node.args:
        target, name = node.args[0], node.args[1] if len(node.args) > 1 else None
        if not _is_own(target):
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                found.append(f"{_ident(node.func)} by a computed name on another object")
            elif name.value.startswith("_") or name.value in SENSITIVE:
                found.append(f"{_ident(node.func)} of {name.value} on another object")
    return found


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
            if any(_mentions(t, FIXTURE_LOADER) for t in texts):
                flag(node, f"uses {FIXTURE_LOADER} (tests only)")
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
        for what in _rewrites(node):
            flag(node, what)
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
    # N1/N5: the snapshot store and the state it authorises with
    "store subclass overriding the hook": (
        "class S(PostgresDatasetStore):\n    def _trusted(self, sha, what):\n        return c\n"
    ),
    "store subclass by type()": "S = type('S', (store_module.PostgresDatasetStore,), {})\n",
    "store allocated without constructor": "s = object.__new__(PostgresDatasetStore)\n",
    "store registry assigned": "store._registry = forged\n",
    "store registry assigned by object.__setattr__": "object.__setattr__(s, '_registry', r)\n",
    "store engine set by setattr": "setattr(store, '_engine', e)\n",
    "private attribute set by computed name": "setattr(store, name, value)\n",
    "private attribute deleted": "del store._max_rows\n",
    "private attribute augmented": "store._max_rows += 1\n",
    "private attribute in a tuple target": "a, store._engine = 1, e\n",
    "class swapped": "store.__class__ = Other\n",
    "instance dictionary written": "store.__dict__['x'] = r\n",
    "freeze replaced": "PostgresDatasetStore.freeze = evil\n",
    "verify replaced": "QualityConfigRegistry.verify = lambda self: self\n",
    "approved replaced by setattr": "setattr(QualityConfigRegistry, 'approved', evil)\n",
    "pinned trust root replaced": "quality_loader.TRUST_ROOT_SHA256 = digest\n",
    "packaged directory redirected": "quality_loader.QUALITY_CONFIG_DIR = Path('x')\n",
    "fixture loader called": "load_fixture_registry(Path('x'))\n",
    "fixture loader imported": "from sentinel.config.quality_loader import load_fixture_registry\n",
    "fixture loader by getattr": "getattr(ql, 'load_fixture_registry')(d)\n",
    "test-session check named": "ql._require_test_session('x')\n",
}
ALLOWED = (
    "from sentinel.config.quality_loader import QualityConfigRegistry, load_quality_registry\n"
    "registry = load_quality_registry()\n"
    "registry2 = quality_loader.load_quality_registry()\n"
    "def f(r: QualityConfigRegistry) -> bool:\n"
    "    return type(r) is QualityConfigRegistry\n"
    'def g():\n    "load_quality_registry(d) and _REGISTRY_KEY in a docstring are fine"\n'
    "SQL = 'market_candles_sealed_range'\n"
    "store = PostgresDatasetStore(engine, registry=load_quality_registry())\n"
    "record = store.freeze(spec, quality)\n"
    "class C:\n"
    "    def __init__(self, v):\n"
    "        self._v = v\n"
    "        object.__setattr__(self, '_w', v)\n"
    "        object.__setattr__(self, name, v)\n"
    "        cls_like = self._v\n"
    "options.freeze_time = True\n"
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
