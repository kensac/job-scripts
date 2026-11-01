"""A worker pays for a heavy SDK only once a task uses it
(docs/agents/engineering-standards.md, "Dependencies")."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HEAVY = ("anthropic", "openai", "boto3", "botocore", "pypdf", "fastapi", "starlette")


def test_a_fresh_worker_loads_no_heavy_sdk():
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys, api.worker; print(','.join(m for m in {HEAVY!r} if m in sys.modules))",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
            # Read at import by core.pool; nothing here connects.
            "DATABASE_URL": os.environ.get("DATABASE_URL", "postgresql://x@127.0.0.1/x"),
        },
    )
    assert out.stdout.strip() == ""
