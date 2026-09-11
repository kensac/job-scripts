"""The declarative base and the type map every table is built on.

Alone in its own module so a table module can import it without importing
the other table modules, and so `api.orm` can import all of them in turn
to fill one metadata.
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase

_now = text("now()")


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy's declarative API
        datetime.datetime: TIMESTAMP(timezone=True),
        list[str]: ARRAY(Text),
        dict: JSONB,
    }
