"""Pipeline API behavior: conversations."""

from __future__ import annotations

from api import db
from tests import mail_log
from tests.pipeline_data import (
    insert_application,
    insert_gmail_message,
    insert_gmail_thread,
    insert_message_match,
    next_message_suffix,
)


def test_mail_can_be_asked_for_one_applications_messages(client, user_headers):
    """Without it an application can only link to a company text search, which
    returns a DIFFERENT set and would quietly lie about being the messages
    behind this application."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mine = insert_application(uid, company="Acme", title="Engineer")
    other = insert_application(uid, company="Acme", title="Other")
    a = insert_gmail_message(uid, kind="rejection", company="Acme")
    b = insert_gmail_message(uid, kind="rejection", company="Acme")
    insert_message_match(a, mine)
    insert_message_match(b, other)

    body = client.get(f"/v1/user/mail?application_id={mine}", headers=user_headers).json()
    assert [m["id"] for m in body["messages"]] == [a]


def test_the_pipeline_filters_on_whether_there_is_evidence(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    heard = insert_application(uid, company="Heard", title="Engineer")
    insert_application(uid, company="Silent", title="Engineer")
    mid = insert_gmail_message(uid, kind="acknowledgement", company="Heard")
    insert_message_match(mid, heard)

    assert client.get("/v1/user/pipeline?evidence=true", headers=user_headers).json()["total"] == 1
    assert client.get("/v1/user/pipeline?evidence=false", headers=user_headers).json()["total"] == 1


def test_a_lens_is_a_set_of_stages_not_one(client, user_headers):
    """Every view worth having is multi-valued - "waiting" is applied AND
    acknowledged, "over" is rejected AND closed AND withdrawn. A single-valued
    filter makes a lens either impossible or a client-side re-derivation of a
    set the server already knows."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    for company, kind in (("A", "acknowledgement"), ("B", "rejection"), ("C", "offer")):
        app_id = insert_application(uid, company=company, title="Engineer")
        mid = insert_gmail_message(uid, kind=kind, company=company)
        insert_message_match(mid, app_id)

    over = client.get(
        "/v1/user/pipeline?stage=rejected,closed,withdrawn&include_closed=true",
        headers=user_headers,
    ).json()
    assert over["total"] == 1
    live = client.get("/v1/user/pipeline?stage=interviewing,assessment,offer", headers=user_headers)
    assert live.json()["total"] == 1


def test_mail_kinds_compose_and_carry_their_counts(client, user_headers):
    """A tab's number and its contents come from the same predicate, so they
    cannot disagree - and without the counts a tab either shows none or costs a
    request each to display a number the server already had."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    insert_gmail_message(uid, kind="rejection", company="A")
    insert_gmail_message(uid, kind="offer", company="B")
    insert_gmail_message(uid, kind="not_job_related")

    replies = client.get("/v1/user/mail?kind=rejection,offer", headers=user_headers).json()
    assert replies["total"] == 2
    assert replies["by_kind"] == {"rejection": 1, "offer": 1}

    # The counts follow the lens rather than the mailbox, which is the same
    # property one level up: not_job_related is absent from the default's
    # counts because it is absent from the default's rows.
    default = client.get("/v1/user/mail", headers=user_headers).json()
    assert default["by_kind"] == {"rejection": 1, "offer": 1}


def test_mail_defaults_to_job_mail_and_hides_nothing(client, user_headers):
    """Nothing is filtered at ingest on purpose - a missed job email is
    unrecoverable - and that rule about what to STORE was being read as a rule
    about what to SHOW. 86.6% of the corpus is not_job_related, so the
    unfiltered default handed a person their whole mailbox (#301).

    Everything excluded stays reachable by its own name, and naming a
    narrowing replaces the default lens instead of stacking with it."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    job = insert_gmail_message(
        uid, subject="Thanks for applying", kind="acknowledgement", company="Acme"
    )
    noise = insert_gmail_message(uid, subject="RE: Happy Diwali", kind="not_job_related")
    db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, sent_at) "
        "VALUES (%s,%s,'gmail','nobody has looked',now()) RETURNING id",
        (uid, f"unclass-{next_message_suffix()}"),
    )

    default = client.get("/v1/user/mail", headers=user_headers).json()
    assert [m["id"] for m in default["messages"]] == [job]

    noise_only = client.get("/v1/user/mail?kind=not_job_related", headers=user_headers).json()
    assert noise_only["total"] == 1
    backlog = client.get("/v1/user/mail?classified=false", headers=user_headers).json()
    assert [m["subject"] for m in backlog["messages"]] == ["nobody has looked"]

    # An application's own messages are exempt: 638 matched messages in
    # production classify not_job_related (measured 2026-09-03), and dropping
    # them would make "the messages behind this application" untrue.
    app_id = insert_application(uid, company="Acme", title="Engineer")
    insert_message_match(noise, app_id)
    attached = client.get(f"/v1/user/mail?application_id={app_id}", headers=user_headers).json()
    assert [m["id"] for m in attached["messages"]] == [noise]


def test_the_pipeline_sorts_on_the_set_not_the_page(client, user_headers):
    """A client sorting what it was handed sorts one page of a filtered whole
    and reads as though it sorted everything, which is a quiet lie rather than
    a missing feature. Longest-silence-first IS the waiting question."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    old = insert_application(uid, company="Old", title="Engineer")
    recent = insert_application(uid, company="Recent", title="Engineer")
    for app_id, when in ((old, "2024-01-01"), (recent, "2026-01-01")):
        mid = db.query_one(
            "INSERT INTO email_messages (user_id, provider_message_id, source, subject, sent_at) "
            "VALUES (%s,%s,'gmail','s',%s) RETURNING id",
            (uid, f"srt-{next_message_suffix()}", when),
        )["id"]
        mail_log.execute(
            "INSERT INTO email_events (message_id, kind, confidence) VALUES (%s,'acknowledgement','high')",
            (mid,),
        )
        insert_message_match(mid, app_id)

    quietest = client.get(
        "/v1/user/pipeline?sort=last_event_at&dir=asc", headers=user_headers
    ).json()
    assert quietest["applications"][0]["id"] == old


def test_a_thread_reads_as_a_conversation_including_its_first_message(client, user_headers):
    """Mail is a flat list and the unit a person thinks in is the exchange:
    19,995 messages sit in conversations of more than one, so roughly a third
    of the corpus is shown out of its context.

    The root is included because the thread key coalesces to the message's own
    id - a reply carries the starter's Message-ID and the starter carries
    nothing, so keying on the provider field alone leaves the original out of
    its own conversation."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    root = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, provider_thread_id, source, "
        "subject, from_email, sent_at) VALUES (%s,'<r@acme>',NULL,'takeout','Your application',"
        "'a@acme.com','2025-06-01') RETURNING id",
        (uid,),
    )["id"]
    reply = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, provider_thread_id, source, "
        "subject, from_email, sent_at) VALUES (%s,'<r2@acme>','<r@acme>','takeout',"
        "'Re: Your application','a@acme.com','2025-06-02') RETURNING id",
        (uid,),
    )["id"]

    body = client.get(f"/v1/user/messages/{reply}/thread", headers=user_headers).json()
    assert [m["id"] for m in body["messages"]] == [root, reply], "oldest first, root included"
    assert body["truncated"] is False


def test_a_thread_says_when_it_was_cut(client, user_headers):
    """The longest key in this corpus holds 474 messages - a mailing list
    reusing a thread id rather than a conversation. A thread silently cut reads
    as a thread that ended."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    ids = insert_gmail_thread(uid, "long-thread", 5)
    body = client.get(f"/v1/user/messages/{ids[0]}/thread?limit=2", headers=user_headers).json()
    assert len(body["messages"]) == 2
    assert body["truncated"] is True


def test_a_message_with_no_conversation_is_a_thread_of_one(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    alone = insert_gmail_message(uid, subject="Standalone", kind="acknowledgement")
    body = client.get(f"/v1/user/messages/{alone}/thread", headers=user_headers).json()
    assert [m["id"] for m in body["messages"]] == [alone]


def test_another_users_thread_is_not_readable(client, user_headers, other_user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    mid = insert_gmail_message(uid, kind="rejection")
    assert (
        client.get(f"/v1/user/messages/{mid}/thread", headers=other_user_headers).status_code == 404
    )


def test_threads_list_as_conversations_not_messages(client, user_headers):
    """Grouping a page of messages client-side produces partial threads at the
    page boundaries - a conversation cut in half by pagination reads as a
    conversation that is half that long, which is a lie exactly where nobody
    looks for one."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    insert_gmail_thread(uid, "convo-a", 3, kind="rejection", company="Acme")
    insert_gmail_thread(uid, "convo-b", 2, kind="acknowledgement", company="Beta")

    body = client.get("/v1/user/threads", headers=user_headers).json()
    assert body["total"] == 2
    counts = sorted(t["message_count"] for t in body["threads"])
    assert counts == [2, 3], "one row per conversation, carrying its size"


def test_needs_attention_is_the_correctable_pile(client, user_headers):
    """Job-related mail that reached no application. Not 'unread' - we have no
    such concept and inventing one would be a second inbox to maintain."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    app_id = insert_application(uid, company="Matched", title="Engineer")
    done = insert_gmail_thread(uid, "convo-done", 1, kind="rejection", company="Matched")
    insert_message_match(done[0], app_id)
    insert_gmail_thread(uid, "convo-open", 2, kind="rejection", company="Orphan")

    needs = client.get("/v1/user/threads?needs_attention=true", headers=user_headers).json()
    assert needs["total"] == 1
    assert needs["threads"][0]["message_count"] == 2

    settled = client.get("/v1/user/threads?needs_attention=false", headers=user_headers).json()
    assert settled["total"] == 1, "the filter and its inverse are complements"


def test_a_thread_row_carries_what_a_row_needs(client, user_headers):
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    ids = insert_gmail_thread(uid, "convo-rich", 2, kind="offer", company="Acme")
    row = client.get("/v1/user/threads", headers=user_headers).json()["threads"][0]
    assert row["subject"] is not None
    assert row["participants"] == ["a@acme.com"]
    assert "offer" in row["kinds"]
    assert row["latest_message_id"] == ids[-1]
    assert row["last_activity_at"] is not None


def test_silence_is_a_filter_because_it_is_the_question_people_ask(client, user_headers):
    """`evidence=false` is "no mail at all". This is "no mail LATELY", which is
    the ghosting question - and silence since an acknowledgement is still
    silence, so it measures from the last thing that happened rather than from
    applying."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    quiet = db.query_one(
        "INSERT INTO applications (user_id, company_name, title, source_provenance, applied_at) "
        "VALUES (%s,'Quiet','Engineer','tracker', now() - interval '200 days') RETURNING id",
        (uid,),
    )["id"]
    recent = db.query_one(
        "INSERT INTO applications (user_id, company_name, title, source_provenance, applied_at) "
        "VALUES (%s,'Recent','Engineer','tracker', now() - interval '3 days') RETURNING id",
        (uid,),
    )["id"]
    # Applied long ago but acknowledged yesterday: not silent.
    talking = db.query_one(
        "INSERT INTO applications (user_id, company_name, title, source_provenance, applied_at) "
        "VALUES (%s,'Talking','Engineer','tracker', now() - interval '200 days') RETURNING id",
        (uid,),
    )["id"]
    mid = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, sent_at) "
        "VALUES (%s,%s,'gmail','hi', now() - interval '1 day') RETURNING id",
        (uid, f"quiet-{next_message_suffix()}"),
    )["id"]
    mail_log.execute(
        "INSERT INTO email_events (message_id, kind, confidence) "
        "VALUES (%s,'acknowledgement','high')",
        (mid,),
    )
    insert_message_match(mid, talking)

    body = client.get("/v1/user/pipeline?silent_days=60", headers=user_headers).json()
    assert [a["id"] for a in body["applications"]] == [quiet]
    assert recent not in [a["id"] for a in body["applications"]]
    assert talking not in [a["id"] for a in body["applications"]], (
        "silence is measured from the last thing that happened, not from applying"
    )


def test_a_finished_application_is_not_silent(client, user_headers):
    """A rejection is an answer. Listing it as unanswered would put closed
    applications in the pile of things still waiting."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    done = db.query_one(
        "INSERT INTO applications (user_id, company_name, title, source_provenance, applied_at) "
        "VALUES (%s,'Done','Engineer','tracker', now() - interval '300 days') RETURNING id",
        (uid,),
    )["id"]
    mid = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, sent_at) "
        "VALUES (%s,%s,'gmail','no', now() - interval '250 days') RETURNING id",
        (uid, f"done-{next_message_suffix()}"),
    )["id"]
    mail_log.execute(
        "INSERT INTO email_events (message_id, kind, confidence) VALUES (%s,'rejection','high')",
        (mid,),
    )
    insert_message_match(mid, done)

    body = client.get("/v1/user/pipeline?silent_days=60", headers=user_headers).json()
    assert done not in [a["id"] for a in body["applications"]]


def test_conversations_sort_on_the_set(client, user_headers):
    """Oldest-conversation-first is how a backlog is worked, and it cannot be
    done client-side without sorting one page of a filtered whole."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]
    for key, when in (("old-convo", "2024-01-01"), ("new-convo", "2026-01-01")):
        mid = db.query_one(
            "INSERT INTO email_messages (user_id, provider_message_id, provider_thread_id, "
            "source, subject, from_email, sent_at) VALUES (%s,%s,%s,'gmail','s','a@b.com',%s) "
            "RETURNING id",
            (uid, f"ts-{next_message_suffix()}", key, when),
        )["id"]
        mail_log.execute(
            "INSERT INTO email_events (message_id, kind, confidence) "
            "VALUES (%s,'acknowledgement','high')",
            (mid,),
        )

    oldest = client.get("/v1/user/threads?sort=started_at&dir=asc", headers=user_headers).json()
    assert oldest["threads"][0]["thread_id"] == "old-convo"
    newest = client.get("/v1/user/threads", headers=user_headers).json()
    assert newest["threads"][0]["thread_id"] == "new-convo"


def test_the_unmatched_queue_is_work_not_the_whole_mailbox(client, user_headers):
    """It shipped showing 63,598 messages - essentially the entire mailbox -
    because personal mail correctly has no application and always will. A queue
    of things to fix must exclude the three populations that are already right:
    not-job-related, unclassified, and the matcher's deliberate refusals."""
    uid = db.query_one("SELECT id FROM users WHERE email = %s", ("user@example.com",))["id"]

    real = insert_gmail_message(
        uid, subject="An update from Acme", kind="rejection", company="Acme"
    )
    insert_gmail_message(uid, subject="RE: Happy Diwali", kind="not_job_related")
    db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject, sent_at) "
        "VALUES (%s,%s,'gmail','test mail',now()) RETURNING id",
        (uid, f"unclass-{next_message_suffix()}"),
    )
    approach = insert_gmail_message(
        uid, subject="Still looking?", kind="recruiter_outreach", company="Globex"
    )
    mail_log.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence) "
        "VALUES (%s, NULL, 'not_an_application', 'none')",
        (approach,),
    )

    queue = client.get("/v1/user/mail?matched=false", headers=user_headers).json()
    assert [m["id"] for m in queue["messages"]] == [real]
    assert queue["total"] == 1, "one thing to fix, not four"

    # Each excluded population stays reachable on purpose, by its own name.
    backlog = client.get("/v1/user/mail?classified=false", headers=user_headers).json()
    assert [m["subject"] for m in backlog["messages"]] == ["test mail"]
    assert (
        client.get("/v1/user/mail?kind=not_job_related", headers=user_headers).json()["total"] == 1
    )
