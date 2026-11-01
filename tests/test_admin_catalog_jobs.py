import json

from api import db


def _catalog(client, headers, **params):
    response = client.get("/v1/admin/jobs/catalog", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_catalog_lists_unchecked_postings_under_typed_filters(client, admin_headers, f):
    ramp = f.make_job(company="Ramp", source="ashby_ramp")
    other = f.make_job(company="Stripe", source="gh_stripe", active=False)
    db.execute("UPDATE jobs SET locations = %s WHERE id = %s", (["New York, NY"], ramp))

    body = _catalog(client, admin_headers)
    ids = [row["job_id"] for row in body["rows"]]
    # Nothing has checked either posting; the catalog lists them anyway, newest first.
    assert ids[:2] == [other, ramp]
    assert "notes" not in {field["key"] for field in body["filter_fields"]}

    rules = [
        {"field": "company", "operator": "equals", "value": "ramp"},
        {"field": "locations", "operator": "contains", "value": "new york"},
    ]
    body = _catalog(client, admin_headers, column_filters=json.dumps(rules), with_total="true")
    assert [row["job_id"] for row in body["rows"]] == [ramp]
    assert body["total"] == 1
    assert body["rows"][0]["on_board"] is None

    closed = [{"field": "active", "operator": "equals", "value": "false"}]
    body = _catalog(client, admin_headers, column_filters=json.dumps(closed), sources="gh_stripe")
    assert [row["job_id"] for row in body["rows"]] == [other]


def test_catalog_joins_pay_only_for_a_pay_filter(client, admin_headers, f):
    """The read joins job_comp only when a filter names pay; a pay filter
    still narrows the rows and the total to postings with that pay."""
    paid = f.make_job(company="Paid")
    f.make_job(company="Unpaid")
    url = db.query_one("SELECT url FROM jobs WHERE id = %s", (paid,))["url"]
    f.make_comp(url, comp_min=150000, comp_currency="USD")

    rules = [{"field": "comp_currency", "operator": "equals", "value": "usd"}]
    body = _catalog(client, admin_headers, column_filters=json.dumps(rules), with_total="true")
    assert [row["job_id"] for row in body["rows"]] == [paid]
    assert body["total"] == 1

    assert _catalog(client, admin_headers, with_total="true")["total"] >= 2


def test_catalog_refuses_a_persons_own_fields(client, admin_headers):
    rule = [{"field": "notes", "operator": "is_empty"}]
    response = client.get(
        "/v1/admin/jobs/catalog", headers=admin_headers, params={"column_filters": json.dumps(rule)}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_COLUMN_FILTER"


def test_catalog_reports_and_filters_one_persons_board(client, admin_headers, user_headers, f):
    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    shown, hidden = f.make_job(), f.make_job()
    db.execute("INSERT INTO board_visible (user_id, job_id) VALUES (%s, %s)", (uid, shown))

    body = _catalog(client, admin_headers, board_user=uid, on_board="true")
    assert [(row["job_id"], row["on_board"]) for row in body["rows"]] == [(shown, True)]
    body = _catalog(client, admin_headers, board_user=uid, on_board="false")
    assert hidden in [row["job_id"] for row in body["rows"]]
    assert shown not in [row["job_id"] for row in body["rows"]]

    missing = client.get(
        "/v1/admin/jobs/catalog", headers=admin_headers, params={"on_board": "true"}
    )
    assert missing.status_code == 422

    url = db.query_one("SELECT url FROM jobs WHERE id = %s", (hidden,))["url"]
    path = client.get(
        "/v1/admin/jobs/path", headers=admin_headers, params={"url": url, "board_user": uid}
    )
    assert path.status_code == 200, path.text
    assert "board" in [consumer["kind"] for consumer in path.json()["consumers"]]
