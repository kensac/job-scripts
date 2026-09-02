from __future__ import annotations

import pytest

from api import db

ENDPOINT = "/v1/analytics/sources"


def _row(payload: dict, source: str) -> dict:
    row = next((r for r in payload["rows"] if r["source"] == source), None)
    assert row is not None, f"{source} missing from {[r['source'] for r in payload['rows']]}"
    return row


def test_requires_admin(client, user_headers):
    assert client.get(ENDPOINT, headers=user_headers).status_code == 403


def test_inventory_counts_active_and_inactive(client, admin_headers, f):
    f.make_source("board-a")
    f.make_job(source="board-a", active=True)
    f.make_job(source="board-a", active=True)
    f.make_job(source="board-a", active=False)

    row = _row(client.get(ENDPOINT, headers=admin_headers).json(), "board-a")
    assert row["inventory"]["total"] == 3
    assert row["inventory"]["active"] == 2
    assert row["inventory"]["inactive"] == 1
    assert row["inventory"]["reports_inactive"] is True


def test_reports_inactive_false_when_feed_never_marks_a_posting_dead(client, admin_headers, f):
    """The distinction the whole active_share caveat rests on: a board with no
    inactive rows has a feed that cannot express inactivity, so its 100% active
    share is a fact about the feed rather than about the board."""
    f.make_source("always-live")
    f.make_job(source="always-live", active=True)
    f.make_job(source="always-live", active=True)

    row = _row(client.get(ENDPOINT, headers=admin_headers).json(), "always-live")
    assert row["inventory"]["reports_inactive"] is False
    assert row["inventory"]["active"] == row["inventory"]["total"]


def test_rate_below_sample_floor_returns_null_but_keeps_its_denominator(client, admin_headers, f):
    f.make_source("tiny")
    for _ in range(3):
        f.make_ready_job(source="tiny", closed="passed", clearance="passed")

    row = _row(client.get(ENDPOINT, headers=admin_headers).json(), "tiny")
    rate = row["funnel"]["closed"]["pass_rate"]
    assert rate["value"] is None
    assert rate["below_floor"] is True
    assert rate["numerator"] == 3
    assert rate["denominator"] == 3


def test_rate_resolves_once_the_denominator_clears_the_floor(client, admin_headers, f):
    f.make_source("big")
    for i in range(40):
        f.make_ready_job(source="big", closed="rejected" if i < 10 else "passed")

    row = _row(client.get(f"{ENDPOINT}?min_sample=40", headers=admin_headers).json(), "big")
    rate = row["funnel"]["closed"]["pass_rate"]
    assert rate["below_floor"] is False
    assert rate["numerator"] == 30
    assert rate["denominator"] == 40
    assert rate["value"] == pytest.approx(0.75)


def test_funnel_uses_the_latest_verdict_per_job(client, admin_headers, f):
    """Verdicts are an append-only log and the newest row wins, so a job whose
    closed-check was re-run must be counted once, at its current answer."""
    f.make_source("revisited")
    _, url = f.make_ready_job(source="revisited", closed="passed")
    f.make_verdict(url, "closed", "rejected")

    row = _row(client.get(ENDPOINT, headers=admin_headers).json(), "revisited")
    assert row["funnel"]["closed"]["checked"] == 1
    assert row["funnel"]["closed"]["rejected"] == 1
    assert row["funnel"]["closed"]["passed"] == 0


def test_dead_on_arrival_reads_the_first_verdict_not_the_latest(client, admin_headers, f):
    """Decay and dead-on-arrival differ only in which end of the log they read:
    a posting that arrived alive and later closed is not dead on arrival."""
    f.make_source("doa")
    _, alive_then_closed = f.make_ready_job(source="doa", closed="passed")
    f.make_verdict(alive_then_closed, "closed", "rejected")
    f.make_ready_job(source="doa", closed="rejected")

    row = _row(client.get(f"{ENDPOINT}?min_sample=1", headers=admin_headers).json(), "doa")
    assert row["decay"]["dead_on_arrival"]["denominator"] == 2
    assert row["decay"]["dead_on_arrival"]["numerator"] == 1
    assert row["funnel"]["closed"]["rejected"] == 2


def test_custom_filters_report_job_level_and_evaluation_level_denominators(
    client, admin_headers, f
):
    """A job judged by two filters is two evaluations but one job, and it only
    passes the board if it passed both."""
    f.make_source("filtered")
    user_id = f.make_user()
    first = f.make_filter(user_id, name="a", prompt="backend")
    second = f.make_filter(user_id, name="b", prompt="remote")

    _, both_pass = f.make_ready_job(source="filtered")
    f.make_verdict(both_pass, "custom", "passed", prompt_hash=first["prompt_hash"])
    f.make_verdict(both_pass, "custom", "passed", prompt_hash=second["prompt_hash"])

    _, one_fails = f.make_ready_job(source="filtered")
    f.make_verdict(one_fails, "custom", "passed", prompt_hash=first["prompt_hash"])
    f.make_verdict(one_fails, "custom", "rejected", prompt_hash=second["prompt_hash"])

    row = _row(client.get(f"{ENDPOINT}?min_sample=1", headers=admin_headers).json(), "filtered")
    custom = row["funnel"]["custom"]
    assert custom["checked"] == 2
    assert custom["passed"] == 1
    assert custom["evaluations"] == 4
    assert custom["evaluations_passed"] == 3


def test_coverage_reports_the_share_of_the_board_a_check_has_seen(client, admin_headers, f):
    f.make_source("partly-checked")
    f.make_ready_job(source="partly-checked", closed="passed")
    for _ in range(3):
        f.make_job(source="partly-checked")

    row = _row(
        client.get(f"{ENDPOINT}?min_sample=1", headers=admin_headers).json(), "partly-checked"
    )
    coverage = row["funnel"]["closed"]["coverage"]
    assert coverage["numerator"] == 1
    assert coverage["denominator"] == 4
    assert coverage["value"] == pytest.approx(0.25)


def test_pseudo_sources_without_a_sources_row_are_not_dropped(client, admin_headers, f):
    """sheet_import and upload have no row in `sources` on purpose. An inner
    join would silently delete the largest board in the catalog."""
    f.make_job(source="sheet_import")
    f.make_job(source="sheet_import")

    row = _row(client.get(ENDPOINT, headers=admin_headers).json(), "sheet_import")
    assert row["configured"] is False
    assert row["listings_url"] is None
    assert row["inventory"]["total"] == 2


def test_configured_source_with_no_postings_is_still_reported(client, admin_headers, f):
    """A board being ingested that yields nothing is the loudest signal here,
    so it must not be filtered out for having no rows to aggregate."""
    f.make_source("silent-board")

    row = _row(client.get(ENDPOINT, headers=admin_headers).json(), "silent-board")
    assert row["configured"] is True
    assert row["inventory"]["total"] == 0
    assert row["funnel"]["closed"]["checked"] == 0


def test_ingest_history_separates_last_success_from_last_new_posting(client, admin_headers, f):
    f.make_source("frozen")
    task_id = f.make_task("ingest_source", {"source": "frozen"}, status="done")
    db.execute("UPDATE tasks SET finished_at = now() WHERE id = %s", (task_id,))
    f.make_task("ingest_source", {"source": "frozen"}, status="failed")

    row = _row(client.get(ENDPOINT, headers=admin_headers).json(), "frozen")
    assert row["ingest"]["runs"] == 2
    assert row["ingest"]["succeeded"] == 1
    assert row["ingest"]["failed"] == 1
    assert row["ingest"]["last_success_at"] is not None


def test_overlap_counts_postings_another_board_also_carries(client, admin_headers, f):
    f.make_source("left")
    f.make_source("right")
    f.make_job(source="left", company="Acme", title="Engineer")
    f.make_job(source="left", company="Acme", title="Designer")
    f.make_job(source="right", company="Acme", title="Engineer")

    payload = client.get(f"{ENDPOINT}?min_sample=1", headers=admin_headers).json()
    left = _row(payload, "left")
    assert left["overlap"]["keyed_jobs"] == 2
    assert left["overlap"]["shared_with_other_source"] == 1
    assert left["overlap"]["exclusive"] == 1


def test_board_yield_counts_applications_per_source(client, admin_headers, f):
    f.make_source("yielding")
    user_id = f.make_user()
    applied = f.make_job(source="yielding")
    tracked = f.make_job(source="yielding")
    f.make_board_row(user_id, applied, status="Application Submitted")
    f.make_board_row(user_id, tracked)
    db.execute("UPDATE user_jobs SET date_applied = now() WHERE job_id = %s", (applied,))

    row = _row(client.get(f"{ENDPOINT}?min_sample=1", headers=admin_headers).json(), "yielding")
    assert row["board_yield"]["board_rows"] == 2
    assert row["board_yield"]["with_status"] == 1
    assert row["board_yield"]["applied"] == 1
    assert row["board_yield"]["users"] == 1


def test_spend_sums_the_cost_recorded_at_call_time(client, admin_headers, f):
    """cost_usd is written when the call is made, against the price in force
    then. Summing it is the only reading that does not restate history when
    the price table changes."""
    f.make_source("spendy")
    _, url = f.make_ready_job(source="spendy")
    db.execute("UPDATE ai_queries SET cost_usd = 0.25 WHERE url = %s AND model IS NOT NULL", (url,))

    row = _row(client.get(ENDPOINT, headers=admin_headers).json(), "spendy")
    priced = db.query_one(
        "SELECT count(cost_usd) AS c, sum(cost_usd) AS s FROM ai_queries WHERE url = %s", (url,)
    )
    assert priced is not None
    assert row["spend"]["cost_usd"] == pytest.approx(float(priced["s"]))
    assert row["spend"]["priced_coverage"]["numerator"] == priced["c"]


def test_spend_coverage_separates_unpriced_calls_from_free_ones(client, admin_headers, f):
    """In production the scrape-only checks carry no model and no cost. Their
    absence must read as "we do not know what this cost", not as a zero that
    quietly understates the board's bill."""
    f.make_source("uncosted")
    _, url = f.make_ready_job(source="uncosted")
    db.execute(
        "UPDATE ai_queries SET model = NULL, cost_usd = NULL "
        "WHERE url = %s AND check_type = 'content'",
        (url,),
    )

    row = _row(client.get(ENDPOINT, headers=admin_headers).json(), "uncosted")
    coverage = row["spend"]["priced_coverage"]
    assert coverage["denominator"] == 3
    assert coverage["numerator"] == 2
    unpriced = [g for g in row["spend"]["by_model"] if g["model"] is None]
    assert unpriced and unpriced[0]["cost_usd"] is None


def test_drill_params_point_at_the_rows_behind_the_aggregate(client, admin_headers, f):
    f.make_source("drillable")
    _, url = f.make_ready_job(source="drillable", closed="rejected")

    row = _row(client.get(ENDPOINT, headers=admin_headers).json(), "drillable")
    drill = row["drill"]
    assert "sources=drillable" in drill["closed_rejected"]
    assert "check_type=closed" in drill["closed_rejected"]
    assert "status=rejected" in drill["closed_rejected"]

    followed = client.get(drill["closed_rejected"], headers=admin_headers)
    assert followed.status_code == 200
    assert [r["url"] for r in followed.json()["rows"]] == [url]


def test_every_drill_link_resolves_to_a_real_route(client, admin_headers, f):
    """A drill that 404s is worse than no drill. `/v1/jobs?source=` shipped in
    #182 and does not exist - the board route is /v1/user/jobs, and it would
    have shown only postings visible to the viewer anyway."""
    f.make_source("linked")
    f.make_ready_job(source="linked", closed="passed")

    row = _row(client.get(ENDPOINT, headers=admin_headers).json(), "linked")
    assert row["drill"], "expected drill links"
    for name, link in row["drill"].items():
        resp = client.get(link, headers=admin_headers)
        assert resp.status_code == 200, f"{name} -> {link} returned {resp.status_code}"


def test_checked_jobs_drill_is_scoped_to_the_board(client, admin_headers, f):
    """It has to actually filter, not just resolve: an endpoint that ignores
    the parameter returns every board's rows under one board's heading."""
    f.make_source("mine")
    f.make_source("theirs")
    _, mine = f.make_ready_job(source="mine", closed="passed")
    f.make_ready_job(source="theirs", closed="passed")

    row = _row(client.get(ENDPOINT, headers=admin_headers).json(), "mine")
    rows = client.get(row["drill"]["checked_jobs"], headers=admin_headers).json()["rows"]
    assert [r["url"] for r in rows] == [mine]


def test_detail_endpoint_names_the_boards_a_source_overlaps(client, admin_headers, f):
    f.make_source("primary")
    f.make_source("mirror")
    f.make_job(source="primary", company="Acme", title="Engineer")
    f.make_job(source="mirror", company="Acme", title="Engineer")

    resp = client.get("/v1/analytics/sources/primary", headers=admin_headers)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["row"]["source"] == "primary"
    assert payload["overlap_partners"] == [{"source": "mirror", "shared_postings": 1}]


def test_detail_endpoint_404s_on_an_unknown_source(client, admin_headers):
    resp = client.get("/v1/analytics/sources/no-such-board", headers=admin_headers)
    assert resp.status_code == 404
