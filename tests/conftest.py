from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

settings.register_profile("dev", max_examples=200, deadline=None)
settings.register_profile(
    "ci",
    max_examples=500,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "deep",
    max_examples=5000,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


_DB_DIR = Path(__file__).resolve().parent / "db"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Everything under tests/db needs PostgreSQL and is marked ``db``."""
    for item in items:
        if _DB_DIR in Path(str(item.path)).parents:
            item.add_marker(pytest.mark.db)
