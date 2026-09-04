"""The model that runs, beside the model that was chosen.

An owner-credit user whose stored ai_model falls outside their allowlist has it
replaced at call time. The setting saved, rendered back on Preferences, and did
not describe the model that ran - the stored value and the effective value
disagreed and nothing said so. Same class as a flag that gates one of several
code paths: everything readable was true and the thing that mattered was not
readable.
"""

from __future__ import annotations

import pytest

from api import budget, db


@pytest.fixture(autouse=True)
def _server_key(monkeypatch):
    """The substitution only happens on the owner key, so the allowlist is
    empty without one and every path raises before it can be reached."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-test")


def _user_id(headers: dict) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))
    assert row is not None
    return row["id"]


def _choose(user_id: int, model: str | None) -> None:
    db.execute(
        "INSERT INTO user_settings (user_id, ai_model) VALUES (%s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET ai_model = EXCLUDED.ai_model",
        (user_id, model),
    )


class TestASubstitutionIsReported:
    def test_a_disallowed_choice_says_what_will_run_instead(self, client, user_headers, f):
        _choose(_user_id(user_headers), "gpt-5.6-luna")
        body = client.get("/v1/user/settings", headers=user_headers).json()
        assert body["ai_model"] == "gpt-5.6-luna"
        assert body["effective_model"] != "gpt-5.6-luna"
        assert body["substituted_from"] == "gpt-5.6-luna"

    def test_the_reason_is_actionable_not_a_verdict(self, client, user_headers, f):
        """ "Not available" is useless. The user needs to know it is shared
        credits doing it and that their own key removes the limit."""
        _choose(_user_id(user_headers), "gpt-5.6-luna")
        reason = client.get("/v1/user/settings", headers=user_headers).json()["substitution_reason"]
        assert "shared credits" in reason
        assert "your own API key" in reason

    def test_an_allowed_choice_reports_no_substitution(self, client, user_headers, f):
        """A correction on a screen where nothing was corrected is its own
        kind of lie."""
        allowed = budget.owner_allowed_models(["jobtracker-users-internal"])
        assert allowed
        _choose(_user_id(user_headers), allowed[0])
        body = client.get("/v1/user/settings", headers=user_headers).json()
        assert body["effective_model"] == allowed[0]
        assert body["substituted_from"] is None
        assert body["substitution_reason"] is None

    def test_choosing_nothing_is_not_a_substitution(self, client, user_headers, f):
        """A user who never chose has not had anything taken away, even though
        a default is filled in for them."""
        _choose(_user_id(user_headers), None)
        body = client.get("/v1/user/settings", headers=user_headers).json()
        assert body["effective_model"] is not None
        assert body["substituted_from"] is None

    def test_the_effective_model_is_always_present(self, client, user_headers, f):
        """The question "what will actually run" must have an answer on every
        read, or the caller is back to guessing from the stored value."""
        body = client.get("/v1/user/settings", headers=user_headers).json()
        assert "effective_model" in body


class TestItComesFromTheSamePlaceTheWorkDoes:
    def test_the_reported_model_is_the_one_resolve_returns(self, client, user_headers, f):
        """Re-deriving the rule in the router would drift and start describing
        a substitution that does not happen."""
        user_id = _user_id(user_headers)
        _choose(user_id, "gpt-5.6-luna")
        from api.auth import AuthedUser

        user = AuthedUser(
            id=user_id,
            sub="test-user",
            email="user@example.com",
            name="test-user",
            groups=["jobtracker-users-internal"],
        )
        cfg = budget.resolve_ai_config(user_id, budget.get_entitlement(user))
        body = client.get("/v1/user/settings", headers=user_headers).json()
        assert body["effective_model"] == cfg.model
        assert body["substituted_from"] == cfg.substituted_from
