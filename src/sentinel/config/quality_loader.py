"""Load data-quality configurations from YAML, strictly, and resolve them by hash.

Numeric scalars become ``Decimal`` built from the literal text (no float is ever created),
and a mapping that repeats a key is refused instead of silently keeping the last value.

Snapshots record the hash of the configuration they were judged with. The registry is the
set of shipped configuration files, keyed by that hash, so an old snapshot is always
re-judged against exactly its own thresholds, never against whatever is current. Removing
or editing a file whose hash a snapshot records makes that snapshot unloadable (fail
closed); a threshold change is a new version in a new file.

Approval (ADR 0015): ``status: APPROVED`` in a configuration is necessary but not enough. A
configuration is approved only when ``approvals/`` next to the configurations holds exactly
one approval record naming its exact hash and version, with the approver, the date and the
evidence. The registry refuses to load at all if any APPROVED configuration lacks exactly
one record, or any record names an unknown, stale or provisional configuration.

Trust root: ``load_quality_registry()`` takes no arguments. It reads only the packaged
``config/quality`` and refuses unless what it read (every configuration hash and every
approval record) has the digest pinned in ``TRUST_ROOT_SHA256``, so the content that can
authorise a snapshot is fixed by the reviewed source, wherever the files are read from.
Registries over other directories come only from ``load_fixture_registry``; they are marked
as fixtures and authorise nothing unless the process is a pytest session (the runtime image
installs no test tooling). A configuration built or rehashed in memory never becomes trusted
by being handed to the registry or the snapshot store.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml

from sentinel.domain.quality import APPROVED, QualityConfig, QualityConfigError

__all__ = [
    "APPROVALS_DIRNAME",
    "CANONICAL",
    "FIXTURE",
    "QUALITY_CONFIG_DIR",
    "TRUST_ROOT_FORMAT",
    "TRUST_ROOT_SHA256",
    "ApprovalRecord",
    "QualityConfigRegistry",
    "load_fixture_registry",
    "load_quality_config",
    "load_quality_registry",
    "parse_approval_yaml",
    "parse_quality_yaml",
    "trust_root",
]

QUALITY_CONFIG_DIR: Final = Path(__file__).resolve().parents[3] / "config" / "quality"
"""The packaged configurations (``/app/config/quality`` in the image, docker/Dockerfile)."""
APPROVALS_DIRNAME: Final = "approvals"
"""Approval records live in this subdirectory of the configuration directory, never apart."""
TRUST_ROOT_FORMAT: Final = "sentinel-quality-trust-root/v1"
TRUST_ROOT_SHA256: Final = "227fd71a6ece9ce0c667fbb3118f370c3ac358f1723f7d957d6bdb83e8bf77b2"
"""Digest of the shipped configurations and approval records (``trust_root``). Adding an
approval or a configuration changes it, in the same reviewed change; ``sentinel
quality-registry`` prints the value the packaged files have."""
CANONICAL: Final = "canonical"
FIXTURE: Final = "fixture"
_TEST_RUNNER: Final = "pytest"

_APPROVAL_KEYS = ("config_version", "config_sha256", "approved_by", "approved_on", "evidence")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REGISTRY_KEY: Final = object()


class _StrictLoader(yaml.SafeLoader):
    pass


def _construct_decimal(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> Decimal:
    text = str(loader.construct_scalar(node)).replace("_", "")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise QualityConfigError(f"not a finite decimal: {text!r}") from exc
    if not value.is_finite():
        raise QualityConfigError(f"not a finite decimal: {text!r}")
    return value


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node)
        if key in seen:
            raise QualityConfigError(
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            )
        seen.add(key)
    return loader.construct_mapping(node)


_StrictLoader.add_constructor("tag:yaml.org,2002:float", _construct_decimal)
_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _strict_mapping(text: str, what: str) -> Mapping[Any, Any]:
    try:
        data: Any = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise QualityConfigError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, Mapping):
        raise QualityConfigError(f"{what} must be a mapping")
    return data


def parse_quality_yaml(text: str) -> QualityConfig:
    return QualityConfig.from_mapping(_strict_mapping(text, "quality configuration"))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise QualityConfigError(f"cannot read {path}: {exc}") from exc


def load_quality_config(path: Path) -> QualityConfig:
    text = _read(path)
    try:
        return parse_quality_yaml(text)
    except QualityConfigError as exc:
        raise QualityConfigError(f"{path.name}: {exc}") from exc


# ----------------------------------------------------------------------------- approvals


def _nonempty(where: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QualityConfigError(f"{where} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """A human approval of one exact configuration identity. Plain data: it is evidence only
    inside a registry that matched it to a shipped APPROVED configuration."""

    config_version: str
    config_sha256: str
    approved_by: str
    approved_on: date
    evidence: str
    """Where the review lives: the real-data gate reports and the pull request."""

    def __post_init__(self) -> None:
        _nonempty("config_version", self.config_version)
        sha256: object = self.config_sha256
        if not isinstance(sha256, str) or not _HEX64.match(sha256):
            raise QualityConfigError("config_sha256 must be a lowercase SHA-256 hex digest")
        _nonempty("approved_by", self.approved_by)
        if type(self.approved_on) is not date:  # a datetime is a date subclass: refused too
            raise QualityConfigError("approved_on must be a date (YYYY-MM-DD)")
        _nonempty("evidence", self.evidence)


def parse_approval_yaml(text: str) -> ApprovalRecord:
    m = _strict_mapping(text, "approval record")
    unknown = sorted(str(k) for k in m if k not in _APPROVAL_KEYS)
    if unknown:
        raise QualityConfigError(f"approval record: unknown key(s) {', '.join(unknown)}")
    missing = [k for k in _APPROVAL_KEYS if k not in m]
    if missing:
        raise QualityConfigError(f"approval record: missing key(s) {', '.join(missing)}")
    for key in _APPROVAL_KEYS:
        if isinstance(m[key], Decimal | float | datetime):
            raise QualityConfigError(f"approval record: {key} has the wrong type")
    return ApprovalRecord(**{k: m[k] for k in _APPROVAL_KEYS})


def _load_approvals(directory: Path) -> list[tuple[Path, ApprovalRecord]]:
    folder = directory / APPROVALS_DIRNAME
    if not folder.exists():
        return []  # no approvals: nothing can be frozen
    if not folder.is_dir():
        raise QualityConfigError(f"{folder} is not a directory")
    records = []
    for path in sorted(folder.glob("*.yaml")):
        text = _read(path)
        try:
            records.append((path, parse_approval_yaml(text)))
        except QualityConfigError as exc:
            raise QualityConfigError(f"{APPROVALS_DIRNAME}/{path.name}: {exc}") from exc
    return records


def _match_approvals(
    configs: Mapping[str, QualityConfig], records: Iterable[tuple[Path, ApprovalRecord]]
) -> frozenset[str]:
    """Hashes of the approved configurations. Any inconsistency refuses everything."""
    approved: dict[str, Path] = {}
    for path, record in records:
        where = f"{APPROVALS_DIRNAME}/{path.name}"
        config = configs.get(record.config_sha256)
        if config is None:
            raise QualityConfigError(
                f"{where} approves {record.config_version} {record.config_sha256}, which matches "
                "no shipped configuration (stale or unknown approval)"
            )
        if config.status != APPROVED:
            raise QualityConfigError(
                f"{where} approves {config.version}, which is {config.status}, not {APPROVED}"
            )
        if record.config_version != config.version:
            raise QualityConfigError(
                f"{where} names version {record.config_version}, but {record.config_sha256} is "
                f"{config.version}"
            )
        if record.config_sha256 in approved:
            raise QualityConfigError(
                f"{config.version} is approved twice ({approved[record.config_sha256].name}, "
                f"{path.name})"
            )
        approved[record.config_sha256] = path
    for sha256, config in configs.items():
        if config.status == APPROVED and sha256 not in approved:
            raise QualityConfigError(
                f"{config.version} is {APPROVED} but has no approval record naming {sha256}"
            )
    return frozenset(approved)


# ----------------------------------------------------------------------------- registry


class QualityConfigRegistry:
    """Every configuration of one directory by content hash, and which of them are approved.

    Created only by ``load_quality_registry`` (origin ``canonical``) or
    ``load_fixture_registry`` (origin ``fixture``) and read-only afterwards. It is not a
    dataclass (``dataclasses.replace`` does not apply), it cannot be subclassed (a subclass
    could override ``approved``), and it carries a seal that only its key-checked
    constructor sets, so an instance made with ``__new__`` authorises nothing. A fixture
    registry authorises nothing outside a test session. The snapshot store additionally
    requires this exact type."""

    __slots__ = ("_approved", "_configs", "_origin", "_sealed", "_trust_root")
    _configs: Mapping[str, QualityConfig]
    _approved: frozenset[str]
    _origin: str
    _trust_root: str
    _sealed: object

    def __init__(
        self,
        configs: Mapping[str, QualityConfig],
        approved: frozenset[str],
        *,
        trust_root: str,
        origin: str,
        _key: object,
    ) -> None:
        if _key is not _REGISTRY_KEY:
            raise QualityConfigError(
                "a QualityConfigRegistry can only be created by load_quality_registry"
            )
        object.__setattr__(self, "_configs", MappingProxyType(dict(configs)))
        object.__setattr__(self, "_approved", frozenset(approved))
        object.__setattr__(self, "_trust_root", trust_root)
        object.__setattr__(self, "_origin", origin)
        object.__setattr__(self, "_sealed", _REGISTRY_KEY)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("QualityConfigRegistry cannot be subclassed")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("QualityConfigRegistry is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("QualityConfigRegistry is read-only")

    @property
    def origin(self) -> str:
        """``canonical`` (the packaged, pinned directory) or ``fixture`` (tests only)."""
        return self._origin

    @property
    def trust_root(self) -> str:
        return self._trust_root

    @property
    def configs(self) -> Mapping[str, QualityConfig]:
        return self._configs

    def verify(self) -> QualityConfigRegistry:
        """Refuse unless the loader issued this exact object and it may authorise here.

        An instance allocated with ``__new__``, or sealed with anything but the loader's
        private key, authorises nothing; neither does a canonical registry whose trust root
        is not the pinned one, nor a fixture registry outside a test session."""
        if type(self) is not QualityConfigRegistry or (
            getattr(self, "_sealed", None) is not _REGISTRY_KEY
        ):
            raise QualityConfigError(
                "this registry was not issued by load_quality_registry; it authorises nothing"
            )
        if self._origin == CANONICAL:
            if self._trust_root != TRUST_ROOT_SHA256:
                raise QualityConfigError(_trust_root_mismatch(self._trust_root))
        elif self._origin == FIXTURE:
            _require_test_session("a fixture registry")
        else:
            raise QualityConfigError(f"unknown registry origin {self._origin!r}")
        return self

    def approved(self, sha256: str) -> QualityConfig:
        """The registry's own configuration with this hash, if it is approved; else refuse.

        This is the only way the snapshot store obtains a configuration to freeze or load
        with; an object built elsewhere is never returned, whatever its hash."""
        config = self.verify().resolve(sha256)
        if sha256 not in self._approved or config.status != APPROVED:
            raise QualityConfigError(
                f"quality configuration {config.version} ({sha256}) is {config.status} and has "
                "no approval record; it cannot freeze or verify a snapshot"
            )
        return config

    def resolve(self, sha256: str) -> QualityConfig:
        config = self.configs.get(sha256)
        if config is None:
            raise QualityConfigError(
                f"quality configuration {sha256} is not available; the result it produced "
                "cannot be reproduced"
            )
        return config

    def by_version(self, version: str) -> QualityConfig:
        for config in self.configs.values():
            if config.version == version:
                return config
        raise QualityConfigError(f"quality configuration {version} is not available")


def _in_test_session() -> bool:
    return _TEST_RUNNER in sys.modules


def _require_test_session(what: str) -> None:
    if not _in_test_session():
        raise QualityConfigError(
            f"{what} authorises nothing outside a test session; production loads only the "
            "packaged configuration (load_quality_registry())"
        )


def _trust_root_mismatch(actual: str) -> str:
    return (
        f"the quality configuration directory has trust root {actual}, not the pinned "
        f"{TRUST_ROOT_SHA256}; it authorises nothing (a file absent, altered or added)"
    )


def trust_root(configs: Iterable[str], records: Iterable[tuple[Path, ApprovalRecord]]) -> str:
    """SHA-256 of the canonical JSON of every configuration hash and every approval record.

    Content only: file names, comments, key order and line endings do not change it."""
    approvals = [
        {
            "config_version": r.config_version,
            "config_sha256": r.config_sha256,
            "approved_by": r.approved_by,
            "approved_on": r.approved_on.isoformat(),
            "evidence": r.evidence,
        }
        for _, r in records
    ]
    document = {
        "format": TRUST_ROOT_FORMAT,
        "configs": sorted(configs),
        "approvals": sorted(approvals, key=lambda a: json.dumps(a, sort_keys=True)),
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _read_directory(directory: Path) -> tuple[dict[str, QualityConfig], frozenset[str], str]:
    """Every ``*.yaml`` in ``directory`` and every approval record in its ``approvals/``.
    Any invalid, conflicting or unmatched file fails all."""
    configs: dict[str, QualityConfig] = {}
    versions: dict[str, Path] = {}
    for path in sorted(directory.glob("*.yaml")):
        config = load_quality_config(path)
        if config.sha256 in configs:
            twin = versions[configs[config.sha256].version]
            raise QualityConfigError(f"{path.name} has the same content as {twin.name}")
        if config.version in versions:
            raise QualityConfigError(
                f"{config.version} is defined twice ({versions[config.version].name}, {path.name}) "
                "with different content"
            )
        configs[config.sha256] = config
        versions[config.version] = path
    if not configs:
        raise QualityConfigError(f"no quality configuration found in {directory}")
    records = _load_approvals(directory)
    approved = _match_approvals(configs, records)
    return configs, approved, trust_root(configs, records)


def load_quality_registry() -> QualityConfigRegistry:
    """The packaged configurations (``QUALITY_CONFIG_DIR``), only if they have the pinned
    trust root. There is no directory argument: production cannot choose where approval
    comes from, and a directory with any other content is refused."""
    configs, approved, root = _read_directory(QUALITY_CONFIG_DIR)
    if root != TRUST_ROOT_SHA256:
        raise QualityConfigError(_trust_root_mismatch(root))
    return QualityConfigRegistry(
        configs, approved, trust_root=root, origin=CANONICAL, _key=_REGISTRY_KEY
    )


def load_fixture_registry(directory: Path) -> QualityConfigRegistry:
    """A registry over a test directory, under the same rules but without the pinned trust
    root. Refused outside a test session, and the registry it returns authorises nothing
    outside one either (``verify``)."""
    _require_test_session("load_fixture_registry")
    configs, approved, root = _read_directory(directory)
    return QualityConfigRegistry(
        configs, approved, trust_root=root, origin=FIXTURE, _key=_REGISTRY_KEY
    )
