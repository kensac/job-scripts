"""A changed generator or measured profile must invalidate a built corpus."""

from pathlib import Path

import pytest

from tests import corpus

GENERATOR_FILES = (
    Path(corpus.__file__),
    *sorted((Path(corpus.__file__).parent / "corpus_generator").glob("*.py")),
    corpus.PROFILE_PATH,
)


@pytest.mark.parametrize("source_file", GENERATOR_FILES, ids=lambda path: path.name)
def test_each_corpus_input_changes_the_fingerprint(monkeypatch, source_file):
    before = corpus.fingerprint()
    original_read_bytes = Path.read_bytes

    def changed_read_bytes(path):
        contents = original_read_bytes(path)
        return contents + b"\n# changed" if path == source_file else contents

    monkeypatch.setattr(Path, "read_bytes", changed_read_bytes)
    assert corpus.fingerprint() != before
