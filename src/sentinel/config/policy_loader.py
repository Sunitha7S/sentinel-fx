"""Load a ``RiskPolicy`` from YAML without ever passing through binary floats.

PyYAML's safe loader turns ``0.50`` into a float. This loader overrides the float
constructor so numeric scalars become ``Decimal`` built from the literal text, which keeps
values exact and makes the policy hash independent of float formatting.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from sentinel.risk.policy import PolicyViolation, RiskPolicy

__all__ = ["load_policy", "parse_policy_yaml"]


class _DecimalLoader(yaml.SafeLoader):
    pass


def _construct_decimal(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> Decimal:
    text = str(loader.construct_scalar(node)).replace("_", "")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise PolicyViolation(f"not a finite decimal: {text!r}") from exc
    if not value.is_finite():
        raise PolicyViolation(f"not a finite decimal: {text!r}")
    return value


_DecimalLoader.add_constructor("tag:yaml.org,2002:float", _construct_decimal)


def parse_policy_yaml(text: str) -> RiskPolicy:
    try:
        data: Any = yaml.load(text, Loader=_DecimalLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise PolicyViolation(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyViolation("policy document must be a mapping")
    return RiskPolicy.from_mapping(data)


def load_policy(path: Path) -> RiskPolicy:
    return parse_policy_yaml(path.read_text(encoding="utf-8"))
