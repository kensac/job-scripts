"""The publisher and packaged extension consume the same recipe format."""

from tools.publish_recipes import ATS, tables

from api.apply import recipes


def test_every_bundled_adapter_is_publishable_without_javascript_wrappers():
    bundled = tables()
    files = list(ATS.glob("*.json"))
    assert files, "an empty package must not make the validation vacuously pass"
    assert len(bundled) == len(files), "duplicate adapter identities would overwrite a recipe"
    for adapter, config in bundled.items():
        assert recipes.validate(adapter, config) == recipes.canonical(config)
