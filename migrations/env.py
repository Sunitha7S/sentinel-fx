"""Alembic environment.

Migrations run as a PostgreSQL role that can create roles (a superuser, or a role with
CREATEROLE that is a member of ``sentinel_owner``). Every object is created while acting as
``sentinel_owner``, so no service role and no login role owns any table (ADR 0011).
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config


def _url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url")
    url = url or config.get_main_option("sqlalchemy.url") or os.environ.get("SENTINEL_DATABASE_URL")
    if not url:
        raise RuntimeError("set SENTINEL_DATABASE_URL (or pass -x url=...) to run migrations")
    return url


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=False)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("offline (SQL script) migrations are not supported")
run_migrations_online()
