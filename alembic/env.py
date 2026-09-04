from __future__ import annotations

import os

import dotenv
from alembic import context
from sqlalchemy import create_engine

from api.orm import Base

dotenv.load_dotenv()


def _url() -> str:
    url = os.environ["DATABASE_URL"]
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=Base.metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
