"""Every module logs as itself: logging.getLogger(__name__).

A literal name attributes a module's records to another module, so filtering
a log viewer by source finds the wrong code (engineering-standards.md).
"""

from __future__ import annotations

import pathlib
import re

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
_LITERAL = re.compile(r"""getLogger\(\s*["']""")


def test_no_module_logs_under_a_literal_name():
    assert [
        str(path.relative_to(_SRC))
        for path in sorted(_SRC.rglob("*.py"))
        if _LITERAL.search(path.read_text())
    ] == []
