"""`user_settings` has one owner: api.user_settings.

Sixteen files read the table and four wrote it, each with its own spelling of
"no row means these defaults". The reads below go through the owner on a
person with no row and on a person with every kind set, so a default that
drifts from what the route serves fails here.
"""

from __future__ import annotations

import pathlib
import re

from api import crypto, db, user_settings
from api.board import eligibility

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
_OWNER = _SRC / "api" / "user_settings.py"

# Any statement that names the table: a read, a join, an insert, an update.
_RAW = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+user_settings\b", re.IGNORECASE)


def test_no_sql_against_user_settings_outside_the_owner():
    copies = [
        f"{path.relative_to(_SRC)}:{text.count(chr(10), 0, m.start()) + 1}"
        for path in sorted(_SRC.rglob("*.py"))
        if path != _OWNER
        for text in [path.read_text()]
        for m in _RAW.finditer(text)
    ]
    assert not copies, "Read and write user_settings through api.user_settings: " + ", ".join(
        copies
    )


def test_a_person_without_a_row_reads_the_defaults(f):
    uid = f.make_user()
    assert user_settings.served(uid) == user_settings.SERVED_DEFAULTS
    assert user_settings.criteria(uid) == user_settings.Criteria(
        criteria={}, bypass_sponsorship_filter=True
    )
    creds = user_settings.credentials(uid)
    assert (creds.ai_provider, creds.has_key, creds.ai_params) == ("openai", False, {})
    assert user_settings.prefs(uid) == {}
    assert user_settings.profile(uid) == {}
    assert user_settings.writing_style(uid) is None
    assert eligibility.settings_params(uid)["bypass_sponsorship"] is True


def test_every_kind_round_trips(f):
    uid = f.make_user()
    user_settings.ensure(uid)
    user_settings.save(
        uid,
        user_settings.Update(
            column_layout=[{"colId": "company"}],
            column_layout_set=True,
            prefs={"auto_draft": False},
            bypass_sponsorship_filter=False,
            criteria={"included_terms": ["rust"]},
            email_digest=True,
            writing_style="  plain  ",
            writing_style_set=True,
        ),
    )
    user_settings.merge_pref(uid, "recheck_models", "closed", "m1")
    user_settings.merge_pref(uid, "recheck_models", "clearance", "m2")
    user_settings.save_profile(uid, {"first_name": "Ada"})
    user_settings.save_api_key(uid, crypto.encrypt("sk-test"), "anthropic", None)

    assert user_settings.prefs(uid) == {
        "auto_draft": False,
        "recheck_models": {"closed": "m1", "clearance": "m2"},
    }
    assert user_settings.criteria(uid) == user_settings.Criteria(
        criteria={"included_terms": ["rust"]}, bypass_sponsorship_filter=False
    )
    assert user_settings.writing_style(uid) == "plain"
    assert user_settings.profile(uid) == {"first_name": "Ada"}
    creds = user_settings.credentials(uid)
    assert creds.has_key and creds.ai_provider == "anthropic"
    assert crypto.decrypt(creds.api_key_enc) == "sk-test"
    assert db.query_one(
        "SELECT 1 AS hit FROM users u WHERE u.id = %s AND " + user_settings.has_own_key_sql("u.id"),
        (uid,),
    )

    # Turning the digest on minted a token; the token unsubscribes.
    (recipient,) = user_settings.digest_recipients(force=False, only=uid)
    assert recipient.digest_token
    assert user_settings.unsubscribe_digest(recipient.digest_token)
    assert user_settings.digest_recipients(force=False, only=uid) == []
    assert not user_settings.unsubscribe_digest("nobody")

    user_settings.clear_api_key(uid)
    assert not user_settings.has_own_key(uid)
    assert user_settings.credentials(uid).ai_provider == "openai"
