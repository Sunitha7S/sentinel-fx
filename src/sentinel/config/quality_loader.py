"""Load data-quality configurations from YAML, strictly, and resolve them by hash.

Numeric scalars become ``Decimal`` built from the literal text (no float is ever created),
and a mapping that repeats a key is refused instead of silently keeping the last value.

Snapshots record the hash of the configuration they were judged with. The registry is the
set of shipped configuration files, keyed by that hash, so an old snapshot is always
re-judged against exactly its own thresholds, never against whatever is current. Removing
or editing a file whose hash a snapshot records makes that snapshot unloadable (fail
closed); a threshold change is a new version in a new file.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from sentinel.domain.quality import QualityConfig, QualityConfigError

__all__ = [
    "QUALITY_CONFIG_DIR",
    "QualityConfigRegistry",
    "load_quality_config",
    "load_quality_registry",
    "parse_quality_yaml",
]

QUALITY_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config" / "quality"


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


def parse_quality_yaml(text: str) -> QualityConfig:
    try:
        data: Any = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise QualityConfigError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, Mapping):
        raise QualityConfigError("quality configuration must be a mapping")
    return QualityConfig.from_mapping(data)


def load_quality_config(path: Path) -> QualityConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise QualityConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_quality_yaml(text)
    except QualityConfigError as exc:
        raise QualityConfigError(f"{path.name}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class QualityConfigRegistry:
    """Every available configuration, by content hash. Read-only."""

    configs: Mapping[str, QualityConfig]

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


def load_quality_registry(directory: Path = QUALITY_CONFIG_DIR) -> QualityConfigRegistry:
    """Load every ``*.yaml`` in ``directory``. Any invalid or conflicting file fails all."""
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
    return QualityConfigRegistry(configs)
