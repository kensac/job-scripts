"""health._detect_boards: the ways a board stops delivering with no error.

Every detector gets one case that must fire and one that must not, and the
must-not cases are the shapes that produce alert fatigue if they fire: a board
that never produced, a transient failure, a worker with too few fetches.
"""

from __future__ import annotations

from api import db, health


def _ingest(source, status, *, worker="hetzner", age_hours=1, error=None, **counts):
    db.execute(
        """
        INSERT INTO tasks (kind, payload, status, worker, error, progress, created_at, finished_at)
        VALUES ('ingest_source', %s, %s, %s, %s, %s,
                now() - make_interval(hours => %s), now() - make_interval(hours => %s))
        """,
        (
            db.jsonb({"source": source}),
            status,
            worker,
            error,
            db.jsonb({"done": 0, "total": 0, "label": source, **counts}) if counts else None,
            age_hours,
            age_hours,
        ),
    )


def _content(url, reason, age_hours):
    db.execute(
        """
        INSERT INTO ai_queries (url, check_type, status, reason, input_content, created_at)
        VALUES (%s, 'content', 'passed', %s, 'x', now() - make_interval(hours => %s))
        """,
        (url, reason, age_hours),
    )


def _kinds():
    return {(a["kind"], a["subject"]) for a in health._detect_boards()}


def test_three_failed_ingests_in_a_row_fire_and_a_transient_one_does_not(f):
    f.make_source("broken")
    f.make_source("blip")
    f.make_source("retired", active=False)
    for h in (1, 2, 3):
        _ingest("broken", "failed", age_hours=h, error="404 from boards-api")
        _ingest("retired", "failed", age_hours=h, error="404")
    _ingest("blip", "failed", age_hours=1, error="connection reset")
    _ingest("blip", "done", age_hours=2, fetched=40, kept=40)
    _ingest("blip", "failed", age_hours=3, error="connection reset")

    assert _kinds() == {("ingest_failing", "broken")}


def test_a_feed_that_returned_nothing_fires_only_if_it_ever_returned_anything(f):
    f.make_source("moved")
    f.make_source("never")
    f.make_source("healthy_mirror")
    _ingest("moved", "done", age_hours=1, fetched=0, kept=0)
    _ingest("moved", "done", age_hours=30, fetched=454, kept=161)
    # Never produced: the admin list says so, an alert would say it forever.
    _ingest("never", "done", age_hours=1, fetched=0, kept=0)
    _ingest("never", "done", age_hours=30, fetched=0, kept=0)
    # A mirror whose every posting is already catalogued still fetches plenty.
    _ingest("healthy_mirror", "done", age_hours=1, fetched=2900, kept=2900)
    _ingest("healthy_mirror", "done", age_hours=30, fetched=2900, kept=2900)

    assert _kinds() == {("source_feed_empty", "moved")}


def test_a_pattern_that_admits_nothing_fires_only_when_a_pattern_is_set(f):
    f.make_source("tight")
    f.make_source("no_pattern")
    db.execute("UPDATE sources SET title_pattern = 'new grad' WHERE name = 'tight'")
    for name in ("tight", "no_pattern"):
        _ingest(name, "done", age_hours=1, fetched=300, kept=0)
        _ingest(name, "done", age_hours=30, fetched=300, kept=12)

    assert _kinds() == {("source_pattern_excludes_all", "tight")}


def test_a_worker_whose_fetches_started_failing_fires_against_its_own_week(f):
    f.make_source("a")
    # hetzner: fine all week, then most fetches failing today.
    _ingest(
        "a", "done", worker="hetzner", age_hours=30, cached=95, fetch_failed=5, fetched=1, kept=1
    )
    _ingest(
        "a", "done", worker="hetzner", age_hours=1, cached=10, fetch_failed=30, fetched=1, kept=1
    )
    # oci: always this bad, which is a known cost, not news.
    _ingest("a", "done", worker="oci", age_hours=30, cached=40, fetch_failed=60, fetched=1, kept=1)
    _ingest("a", "done", worker="oci", age_hours=1, cached=15, fetch_failed=25, fetched=1, kept=1)
    # laptop: failing today but on too few fetches to say anything.
    _ingest(
        "a", "done", worker="laptop", age_hours=30, cached=50, fetch_failed=0, fetched=1, kept=1
    )
    _ingest("a", "done", worker="laptop", age_hours=1, cached=2, fetch_failed=10, fetched=1, kept=1)

    assert _kinds() == {("worker_fetches_failing", "hetzner")}


def test_a_resolver_going_quiet_fires_per_ats_not_per_source():
    # Lever: nine in ten postings came from the API last week, none today.
    for i in range(20):
        _content(f"https://jobs.lever.co/acme/{i}", "ats text" if i < 18 else "scraped", 40)
    for i in range(12):
        _content(f"https://jobs.lever.co/acme/today{i}", "scraped", 2)
    # Greenhouse: never had an API share to lose.
    for i in range(20):
        _content(f"https://boards.greenhouse.io/x/jobs/{i}", "scraped", 40)
    for i in range(12):
        _content(f"https://boards.greenhouse.io/x/jobs/today{i}", "scraped", 2)
    # A custom host is nobody's resolver.
    for i in range(30):
        _content(f"https://careers.example.com/{i}", "scraped", 2)

    assert _kinds() == {("resolver_bypassed", "lever")}


def test_every_new_kind_says_what_its_subject_is():
    assert health.subject_kind_for("ingest_failing") == "source"
    assert health.subject_kind_for("source_feed_empty") == "source"
    assert health.subject_kind_for("source_pattern_excludes_all") == "source"
    assert health.subject_kind_for("worker_fetches_failing") == "worker"
    assert health.subject_kind_for("resolver_bypassed") == "ats"
