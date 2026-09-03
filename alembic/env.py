from __future__ import annotations

import os

import dotenv
from alembic import context
from sqlalchemy import create_engine

from api.orm import Base

dotenv.load_dotenv()

# Tables owned elsewhere: ai_queries belongs to core/store.py (the tracker CLI
# creates it standalone); alembic must neither create nor drop it.
_FOREIGN_TABLES = {"ai_queries"}


def _include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name in _FOREIGN_TABLES:
        return False
    return True


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
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
