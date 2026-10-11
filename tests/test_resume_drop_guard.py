"""The final resume drop verifies canonical copies and retains old run history."""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from api import db

_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "5a04926d6434_drop_legacy_resume_values_after_.py"
)


def _check() -> None:
    spec = importlib.util.spec_from_file_location("resume_drop", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine(
        os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+psycopg://")
    )
    try:
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            module._require_canonical_copies()
    finally:
        engine.dispose()


def test_text_and_pdf_gaps_cannot_pass_matching_counts_and_sizes(f):
    user_id = f.make_user()
    version_id = uuid.uuid4()
    db.execute(
        "INSERT INTO resume_text_versions (id, user_id, chars, part_count, ready) "
        "VALUES (%s, %s, 2, 2, TRUE)",
        (version_id, user_id),
    )
    for index, value in ((0, "a"), (2, "b")):
        db.execute(
            "INSERT INTO resume_text_parts (version_id, part_index, text) VALUES (%s, %s, %s)",
            (version_id, index, value),
        )
    resume = db.query_one(
        "INSERT INTO user_resumes (user_id, name, text_version_id) "
        "VALUES (%s, 'gap', %s) RETURNING id",
        (user_id, version_id),
    )
    assert resume is not None
    with pytest.raises(RuntimeError, match="incomplete canonical resume copies"):
        _check()

    db.execute(
        "UPDATE resume_text_parts SET part_index = 1 WHERE version_id = %s AND part_index = 2",
        (version_id,),
    )
    upload_id = uuid.uuid4()
    db.execute(
        "INSERT INTO resume_uploads "
        "(id, user_id, name, filename, size, received_size, next_part, status) "
        "VALUES (%s, %s, 'gap', 'gap.pdf', 2, 2, 2, 'ready')",
        (upload_id, user_id),
    )
    for index in (0, 2):
        db.execute(
            "INSERT INTO resume_upload_parts "
            "(upload_id, part_index, sha256, size, verified) "
            "VALUES (%s, %s, %s, 1, TRUE)",
            (upload_id, index, "0" * 64),
        )
    db.execute(
        "UPDATE user_resumes SET pdf_upload_id = %s WHERE id = %s", (upload_id, resume["id"])
    )
    with pytest.raises(RuntimeError, match="incomplete canonical resume copies"):
        _check()


def test_cleared_source_passes_with_historical_failed_run(f):
    user_id = f.make_user()
    version_id = uuid.uuid4()
    db.execute(
        "INSERT INTO resume_text_versions (id, user_id, chars, part_count, ready) "
        "VALUES (%s, %s, 3, 1, TRUE)",
        (version_id, user_id),
    )
    db.execute(
        "INSERT INTO resume_text_parts (version_id, part_index, text) VALUES (%s, 0, 'abc')",
        (version_id,),
    )
    resume = db.query_one(
        "INSERT INTO user_resumes (user_id, name, text_version_id) "
        "VALUES (%s, 'cleared', %s) RETURNING id",
        (user_id, version_id),
    )
    assert resume is not None
    db.execute(
        "INSERT INTO resume_legacy_clear_runs "
        "(id, source_kind, source_id, field, source_xmin, canonical_id, "
        "total, part_count, verified_digest, stage, failed_at, last_error) "
        "VALUES (%s, 'current', %s, 'text', 'obsolete', %s, "
        "3, 1, %s, 'verify', now(), 'source changed')",
        (uuid.uuid4(), resume["id"], version_id, "0" * 64),
    )
    _check()
