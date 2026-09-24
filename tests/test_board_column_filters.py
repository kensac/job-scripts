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
