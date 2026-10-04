"""Database test harness.

* ``SENTINEL_TEST_DATABASE_URL`` points at a PostgreSQL server as a role that can create
  databases and roles (a superuser on a disposable server).
* If it is missing or unreachable, tests **fail** when ``SENTINEL_REQUIRE_DB=1`` (set in
  CI) and are skipped with an explicit reason otherwise.
* One template database is migrated per session; every test gets a fresh copy, because the
  append-only tables cannot be cleaned between tests (by design).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from sentinel.store.postgres.engine import role_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
URL_ENV = "SENTINEL_TEST_DATABASE_URL"
REQUIRE_ENV = "SENTINEL_REQUIRE_DB"


def _unavailable(reason: str) -> None:
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"database required ({REQUIRE_ENV}=1) but {reason}", pytrace=False)
    pytest.skip(f"database tests not run: {reason}")


def _with_database(url: str, name: str) -> str:
    return make_url(url).set(database=name).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def admin_url() -> str:
    url = os.environ.get(URL_ENV)
    if not url:
        _unavailable(f"{URL_ENV} is not set")
        raise AssertionError("unreachable")
    try:
        engine = create_engine(url, poolclass=NullPool)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except OperationalError as exc:
        _unavailable(f"cannot connect to {make_url(url).render_as_string()}: {exc.orig}")
    return url


def _admin(url: str) -> Engine:
    return create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")


@pytest.fixture(scope="session")
def template_db(admin_url: str) -> Iterator[str]:
    name = f"sentinel_tpl_{uuid.uuid4().hex[:8]}"
    admin = _admin(admin_url)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = _with_database(admin_url, name)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    yield name
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@dataclass
class Db:
    url: str
    name: str
    _engines: dict[str | None, Engine] = field(default_factory=dict)

    def engine(self, role: str | None = None) -> Engine:
        """Engine acting as ``role``; ``None`` is the superuser (for tamper simulations)."""
        if role not in self._engines:
            self._engines[role] = role_engine(self.url, role, poolclass=NullPool)
        return self._engines[role]

    def dispose(self) -> None:
        for e in self._engines.values():
            e.dispose()


@pytest.fixture
def db(admin_url: str, template_db: str) -> Iterator[Db]:
    name = f"sentinel_t_{uuid.uuid4().hex[:10]}"
    admin = _admin(admin_url)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"'))
    handle = Db(url=_with_database(admin_url, name), name=name)
    yield handle
    handle.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()
