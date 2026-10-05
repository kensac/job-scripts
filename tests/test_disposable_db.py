from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DevApiGuardTests(unittest.TestCase):
    def run_dev_api(self, dsn: str, target: str = "dev-api") -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            launcher = Path(directory) / "uvicorn"
            launcher.write_text('#!/bin/sh\nprintf "API_STARTED\\n"\n')
            launcher.chmod(0o755)
            return subprocess.run(
                ["make", "--no-print-directory", target],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": f"{directory}:{os.environ['PATH']}",
                    "JOBTRACKER_DEV_DATABASE_URL": dsn,
                },
                capture_output=True,
                text=True,
            )

    def test_rejects_names_outside_the_disposable_database(self):
        for dsn in (
            "postgresql://example_test_user@localhost/production",
            "postgresql://user:secret_test_password@localhost/production",
            "postgresql://user@test_dev_host/production",
            "postgresql://user@localhost/production?application_name=client_test",
            "postgresql://user@localhost/jobtracker_test_backup",
            "postgresql://user@localhost/jobtracker_test?dbname=production",
            "postgresql://user@localhost/jobtracker_test?dbname=postgresql://localhost/production",
        ):
            with self.subTest(dsn=dsn):
                result = self.run_dev_api(dsn)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("API_STARTED", result.stdout)
                self.assertNotIn("secret_test_password", result.stdout + result.stderr)

    def test_every_local_database_target_refuses_production(self):
        # The guard is shared, so one production-shaped name per target is
        # enough to show each target runs it before connecting.
        for target in ("dev-worker", "migrate"):
            with self.subTest(target=target):
                result = self.run_dev_api(
                    "postgresql://user:secret_test_password@localhost/jobtracker", target
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("refusing database", result.stderr)
                self.assertNotIn("secret_test_password", result.stdout + result.stderr)

    def test_accepts_disposable_names(self):
        for name in (
            "jobtracker_test",
            "jobtracker_ci",
            "test_feature",
            "jobtracker_dev",
            "dev_feature",
        ):
            with self.subTest(name=name):
                result = self.run_dev_api(f"postgresql://user@localhost/{name}")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("API_STARTED", result.stdout)


class SyncTargetTests(unittest.TestCase):
    def test_database_swap_removes_query_override(self):
        from psycopg.conninfo import conninfo_to_dict
        from scripts.sync_testdb import _swap_db

        dsn = "postgresql://user@localhost/original?dbname=production&application_name=sync"
        target = conninfo_to_dict(_swap_db(dsn, "jobtracker_test"))
        self.assertEqual(target["dbname"], "jobtracker_test")
        self.assertEqual(target["application_name"], "sync")
