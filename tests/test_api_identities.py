"""The identity set, shown to the person it is about.

core.identity derives which addresses a mailbox belongs to, and until now its
output had never been shown to the owner - the derivation ran inside the
classify sweep and nobody ever confirmed it. That is what the 1,175 wrongly
booked events cost.
"""

from __future__ import annotations

from api import db


def _user_id(headers: dict) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))
    assert row is not None
    return row["id"]


def _mail(user_id: int, frm: str, to: list[str], n: int = 1) -> None:
    for i in range(n):
        db.execute(
            "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
            "to_emails, subject, sent_at) VALUES (%s, %s, 'gmail', %s, %s, 'x', now())",
            (user_id, f"m-{frm}-{'-'.join(to)}-{i}-{user_id}", frm, to),
        )


class TestTheDerivedSetIsVisible:
    def test_candidates_carry_the_evidence_not_just_the_address(self, client, user_headers, f):
        """A list of addresses asks the user to trust it. The counts are what
        make it self-explanatory - the real corpus separates at 50.6% and 34.0%
        with a 4.29x drop, which is only obvious when the shares are shown."""
        uid = _user_id(user_headers)
        _mail(uid, "me@gmail.com", ["them@corp.com"], n=60)
        _mail(uid, "them@corp.com", ["me@psu.edu"], n=40)

        body = client.get("/v1/user/identities", headers=user_headers).json()
        by_address = {c["address"]: c for c in body["candidates"]}
        assert by_address["me@gmail.com"]["messages"] == 60
        assert 0 < by_address["me@gmail.com"]["share"] <= 1
        assert body["total_messages"] == 100

    def test_nothing_is_confirmed_until_the_user_answers(self, client, user_headers, f):
        body = client.get("/v1/user/identities", headers=user_headers).json()
        assert body["confirmed"] is None
        assert body["confirmed_at"] is None

    def test_a_mailbox_too_small_to_read_says_so(self, client, user_headers, f):
        """An empty candidate list is not an answer. Below the floor the
        derivation deliberately returns nothing, and the page has to ask rather
        than render emptiness as though it had looked and found nobody."""
        uid = _user_id(user_headers)
        _mail(uid, "me@gmail.com", ["them@corp.com"], n=3)
        body = client.get("/v1/user/identities", headers=user_headers).json()
        assert body["fallback_reason"] == "mailbox_too_small"

    def test_a_readable_mailbox_has_no_fallback_reason(self, client, user_headers, f):
        uid = _user_id(user_headers)
        _mail(uid, "me@gmail.com", ["them@corp.com"], n=60)
        _mail(uid, "them@corp.com", ["me@psu.edu"], n=40)
        body = client.get("/v1/user/identities", headers=user_headers).json()
        assert body["fallback_reason"] is None


class TestConfirming:
    def test_the_step_is_done_iff_confirmed_is_not_null(self, client, user_headers, f):
        """Derived from the row the feature already reads, so the checklist
        cannot report done while the data says otherwise."""
        assert client.get("/v1/user/identities", headers=user_headers).json()["confirmed"] is None
        client.put(
            "/v1/user/identities", headers=user_headers, json={"addresses": ["me@gmail.com"]}
        )
        body = client.get("/v1/user/identities", headers=user_headers).json()
        assert body["confirmed"] == ["me@gmail.com"]
        assert body["confirmed_at"] is not None

    def test_the_server_owns_the_canonical_form(self, client, user_headers, f):
        """The UI renders the echo, never its own input, so normalisation has
        to happen here or the two drift."""
        resp = client.put(
            "/v1/user/identities",
            headers=user_headers,
            json={"addresses": ["  Me@GMAIL.com ", "me@gmail.com", "Other@psu.edu"]},
        )
        assert resp.json()["confirmed"] == ["me@gmail.com", "other@psu.edu"]

    def test_an_empty_set_is_refused_with_a_printable_reason(self, client, user_headers, f):
        """ "Nothing is me" would make every message the owner sent look like
        mail from a stranger - the exact error this feature exists to stop."""
        resp = client.put("/v1/user/identities", headers=user_headers, json={"addresses": []})
        assert resp.status_code == 400
        assert "misfile every message you sent" in resp.json()["detail"]

    def test_whitespace_only_is_an_empty_set_not_an_address(self, client, user_headers, f):
        resp = client.put("/v1/user/identities", headers=user_headers, json={"addresses": ["  "]})
        assert resp.status_code == 400

    def test_a_user_may_claim_an_address_the_corpus_never_showed(self, client, user_headers, f):
        """The write is not a subset of candidates. An old address with no mail
        left in the mailbox is still theirs, and only they know that."""
        uid = _user_id(user_headers)
        _mail(uid, "me@gmail.com", ["them@corp.com"], n=60)
        resp = client.put(
            "/v1/user/identities", headers=user_headers, json={"addresses": ["ancient@old.edu"]}
        )
        assert resp.json()["confirmed"] == ["ancient@old.edu"]

    def test_confirming_again_replaces_rather_than_appends(self, client, user_headers, f):
        client.put("/v1/user/identities", headers=user_headers, json={"addresses": ["a@x.com"]})
        resp = client.put(
            "/v1/user/identities", headers=user_headers, json={"addresses": ["b@x.com"]}
        )
        assert resp.json()["confirmed"] == ["b@x.com"]


class TestPrecedence:
    """A confirmation that only applied where it agreed with the derivation
    would be theatre. The person reading their own mailbox knows things the
    corpus cannot show us."""

    def test_a_confirmed_set_beats_the_derivation(self, client, user_headers, f):
        from api.tasks.mail_classify import identities_for

        uid = _user_id(user_headers)
        # The owner's address is the one on BOTH sides of the mailbox, which is
        # what the derivation keys on; a correspondent appears on one side only.
        _mail(uid, "me@gmail.com", ["them@corp.com"], n=60)
        _mail(uid, "other@corp.com", ["me@gmail.com"], n=40)
        assert identities_for(uid) == ["me@gmail.com"]

        client.put(
            "/v1/user/identities", headers=user_headers, json={"addresses": ["only@psu.edu"]}
        )
        assert identities_for(uid) == ["only@psu.edu"]

    def test_a_confirmed_set_beats_the_configured_address_fallback(self, client, user_headers, f):
        """Confirm only gmail while logging in with psu and psu stops counting
        as you. That is the whole point of being asked."""
        from api.tasks.mail_classify import identities_for

        uid = _user_id(user_headers)
        assert identities_for(uid) == ["user@example.com"]
        client.put(
            "/v1/user/identities", headers=user_headers, json={"addresses": ["chosen@gmail.com"]}
        )
        assert identities_for(uid) == ["chosen@gmail.com"]

    def test_one_users_confirmation_does_not_reach_another(
        self, client, user_headers, other_user_headers, f
    ):
        from api.tasks.mail_classify import identities_for

        client.put(
            "/v1/user/identities", headers=user_headers, json={"addresses": ["mine@gmail.com"]}
        )
        assert identities_for(_user_id(other_user_headers)) != ["mine@gmail.com"]


class TestTheCountInTheButton:
    """The surface has to say what confirming will do BEFORE the click, not
    report it afterwards. Counted with the predicate the healer itself acts on,
    so the number cannot drift from the action."""

    def _booked(self, user_id: int, frm: str, to: list[str]) -> int:
        db.execute(
            "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
            "to_emails, subject, sent_at) VALUES (%s, %s, 'gmail', %s, %s, 'x', now())",
            (user_id, f"booked-{frm}-{to}-{user_id}", frm, to),
        )
        row = db.query_one("SELECT id FROM email_messages ORDER BY id DESC LIMIT 1")
        assert row is not None
        db.execute(
            "INSERT INTO email_events (message_id, kind, confidence, detail, model) "
            "VALUES (%s, 'interview', 'high', '{}'::jsonb, NULL)",
            (row["id"],),
        )
        return row["id"]

    def test_it_counts_events_a_proposed_set_would_supersede(self, client, user_headers, f):
        uid = _user_id(user_headers)
        self._booked(uid, "me@gmail.com", ["someone@corp.com"])
        resp = client.get(
            "/v1/user/identities", headers=user_headers, params={"proposed": ["me@gmail.com"]}
        )
        assert resp.json()["would_reexamine"] == 1

    def test_a_set_that_does_not_claim_the_sender_reexamines_nothing(self, client, user_headers, f):
        uid = _user_id(user_headers)
        self._booked(uid, "me@gmail.com", ["someone@corp.com"])
        resp = client.get(
            "/v1/user/identities", headers=user_headers, params={"proposed": ["other@psu.edu"]}
        )
        assert resp.json()["would_reexamine"] == 0

    def test_narrowing_reexamines_nothing_because_healing_is_one_way(self, client, user_headers, f):
        """Widening is retroactive; narrowing is forward-only. The healer only
        adds corrections for mail that IS self-sent, so dropping an address
        leaves its old corrections standing - and the count must not imply a
        retraction undoes anything."""
        uid = _user_id(user_headers)
        message_id = self._booked(uid, "me@gmail.com", ["someone@corp.com"])
        db.execute(
            "INSERT INTO email_events (message_id, kind, confidence, detail, model) VALUES "
            "(%s, 'not_job_related', 'high', '{\"reason\": \"self_sent\"}'::jsonb, NULL)",
            (message_id,),
        )
        resp = client.get(
            "/v1/user/identities", headers=user_headers, params={"proposed": ["other@psu.edu"]}
        )
        assert resp.json()["would_reexamine"] == 0

    def test_a_message_already_corrected_is_not_counted_again(self, client, user_headers, f):
        """Otherwise the number never converges and the button promises work
        that will not happen."""
        uid = _user_id(user_headers)
        message_id = self._booked(uid, "me@gmail.com", ["someone@corp.com"])
        db.execute(
            "INSERT INTO email_events (message_id, kind, confidence, detail, model) VALUES "
            "(%s, 'not_job_related', 'high', '{\"reason\": \"self_sent\"}'::jsonb, NULL)",
            (message_id,),
        )
        resp = client.get(
            "/v1/user/identities", headers=user_headers, params={"proposed": ["me@gmail.com"]}
        )
        assert resp.json()["would_reexamine"] == 0

    def test_another_users_mail_is_never_counted(self, client, user_headers, other_user_headers, f):
        self._booked(_user_id(other_user_headers), "me@gmail.com", ["someone@corp.com"])
        resp = client.get(
            "/v1/user/identities", headers=user_headers, params={"proposed": ["me@gmail.com"]}
        )
        assert resp.json()["would_reexamine"] == 0
