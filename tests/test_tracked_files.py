"""Files a thing needs in order to load at all are tracked.

The *.json rule in .gitignore has swallowed four such files so far, each
found by someone noticing an absence: compose.ci.yml, .gitleaks.toml,
pyrightconfig.json, and extension/manifest.json twice in two PRs (the
extension on main was unloadable by anyone but the machine that had the
file on disk). A negation fixes one file; this fails the PR that drops the
next one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Every file whose absence makes something fail to load rather than fail a
# test: the extension's manifest, the type checker's config, the committed
# API schema, Renovate's config. Add to this list when adding such a file.
MUST_BE_TRACKED = (
    "extension/manifest.json",
    "pyrightconfig.json",
    "openapi.json",
    "renovate.json",
    ".gitleaks.toml",
)


def test_the_files_a_thing_needs_to_load_are_tracked():
    tracked = set(
        subprocess.run(
            ["git", "ls-files", "--", *MUST_BE_TRACKED],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )
    missing = [f for f in MUST_BE_TRACKED if f not in tracked and (ROOT / f).exists()]
    assert not missing, f"present on disk but not tracked (a .gitignore rule ate it): {missing}"
    absent = [f for f in MUST_BE_TRACKED if not (ROOT / f).exists()]
    assert not absent, f"listed as required but not in the repository: {absent}"
