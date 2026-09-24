import json

import pytest

from api import db


@pytest.mark.parametrize(
    "rule",
    [
        {"field": "company", "operator": "contains", "value": "Acme"},
        {"field": "date_applied", "operator": "is_empty"},
        {"field": "comp_max", "operator": "gte", "value": "150000"},
    ],
)
def test_board_echoes_supported_typed_filters(client, user_headers, rule):
    rules = [rule]
    if rule["field"] == "comp_max":
        rules += [
            {"field": "comp_currency", "operator": "equals", "value": "USD"},
            {"field": "comp_period", "operator": "equals", "value": "year"},
        ]
    response = client.get(
        "/v1/user/jobs",
        headers=user_headers,
        params={"column_filters": json.dumps(rules), "with_total": "true"},
    )
    assert response.status_code == 200
    assert response.json()["column_filters"] == [
        dict(rule, value=rule.get("value")) for rule in rules
    ]
    assert any(field["key"] == rule["field"] for field in response.json()["filter_fields"])


@pytest.mark.parametrize(
    "rules",
    [
        [{"field": "company; DROP TABLE jobs", "operator": "equals", "value": "x"}],
        [{"field": "company", "operator": "gte", "value": "x"}],
        [{"field": "added_at", "operator": "gte", "value": "yesterday"}],
        [{"field": "comp_max", "operator": "gte", "value": "NaN"}],
        [{"field": "comp_max", "operator": "gte", "value": "150000"}],
    ],
)
def test_invalid_filters_are_rejected_not_silently_ignored(client, user_headers, rules):
    response = client.get(
        "/v1/user/jobs", headers=user_headers, params={"column_filters": json.dumps(rules)}
    )
    assert response.status_code == 422


def test_column_filters_apply_before_pagination_and_totals(client, user_headers, f):
    uid = db.query_one("SELECT id FROM users WHERE sub='test-user'")["id"]
    for company in ["Acme", "Other", "Acme"]:
        jid = f.make_job(company=company)
        db.execute(
            "INSERT INTO user_jobs (user_id,job_id,status) VALUES (%s,%s,'saved')", (uid, jid)
        )
    response = client.get(
        "/v1/user/jobs",
        headers=user_headers,
        params={
            "limit": 1,
            "with_total": "true",
            "column_filters": json.dumps(
                [{"field": "company", "operator": "equals", "value": "Acme"}]
            ),
        },
    )
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert len(response.json()["rows"]) == 1
    assert response.json()["rows"][0]["company"] == "Acme"


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [
        ("contains", "100%_", ["100%_ Inc"]),
        ("not_contains", "Acme", ["100%_ Inc", "100XX Inc"]),
        ("not_equals", "Acme", ["100%_ Inc", "100XX Inc"]),
        ("is_empty", None, ["", "   "]),
    ],
)
def test_text_filters_keep_literal_wildcards_and_explicit_empty_semantics(
    client, user_headers, f, operator, value, expected
):
    uid = db.query_one("SELECT id FROM users WHERE sub='test-user'")["id"]
    for company in ["100%_ Inc", "100XX Inc", "Acme", "", "   "]:
        jid = f.make_job(company=company)
        db.execute(
            "INSERT INTO user_jobs (user_id,job_id,status) VALUES (%s,%s,'saved')", (uid, jid)
        )
    response = client.get(
        "/v1/user/jobs",
        headers=user_headers,
        params={
            "column_filters": json.dumps(
                [{"field": "company", "operator": operator, "value": value}]
            )
        },
    )
    assert response.status_code == 200
    actual = [row["company"] for row in response.json()["rows"]]
    assert len(actual) == len(expected)
    assert set(actual) == set(expected)


def test_private_column_predicates_cannot_read_another_users_notes(client, user_headers, f):
    uid = db.query_one("SELECT id FROM users WHERE sub='test-user'")["id"]
    other = db.query_one(
        "INSERT INTO users (sub,email) VALUES ('other-filter-user','other@example.test') RETURNING id"
    )["id"]
    jid = f.make_job(company="Acme")
    db.execute(
        "INSERT INTO user_jobs (user_id,job_id,status,notes) VALUES (%s,%s,'saved','mine'), (%s,%s,'saved','private')",
        (uid, jid, other, jid),
    )
    for value, count in [("private", 0), ("mine", 1)]:
        response = client.get(
            "/v1/user/jobs",
            headers=user_headers,
            params={
                "column_filters": json.dumps(
                    [{"field": "notes", "operator": "contains", "value": value}]
                ),
                "with_total": "true",
            },
        )
        assert response.status_code == 200
        assert response.json()["total"] == count
