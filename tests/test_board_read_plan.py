import pytest

from api import db
from api.board import visibility


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


@pytest.mark.no_board_recompute
def test_small_board_read_does_not_walk_unrelated_catalog(f):
    owner = f.make_user()
    visible = f.make_job()
    db.execute("INSERT INTO board_visible(user_id,job_id) VALUES (%s,%s)", (owner, visible))
    # Catalog growth must not enlarge one person's membership lookup.
    db.execute(
        "INSERT INTO jobs(url,raw_url,source,company,title,active) "
        "SELECT 'https://unrelated.test/'||i, 'https://unrelated.test/'||i, "
        "'unrelated', 'Unrelated', 'Engineer', TRUE FROM generate_series(1,10000) i"
    )
    for table in ["jobs", "board_visible", "user_jobs"]:
        db.execute(f"ANALYZE {table}")
    sql = visibility.FAST.format(columns="j.id", extra="")
    rows = db.query(sql, {"uid": owner})
    assert rows == [{"id": visible}]
    plan = db.query_one("EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) " + sql, {"uid": owner})[
        "QUERY PLAN"
    ][0]["Plan"]
    examined = sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
        for node in _nodes(plan)
        if node.get("Relation Name") == "jobs"
    )
    # The only catalog row this read may need is the visible posting (plus
    # the indexed upload branch). No elapsed-time assertion depends on CI load.
    assert examined < 10000, plan


@pytest.mark.no_board_recompute
def test_membership_branches_deduplicate_and_keep_private_state_scoped(f):
    owner, other = f.make_user(), f.make_user()
    overlap = f.make_job(uploaded_by=owner)
    noted, applied, untouched, foreign, hidden = [f.make_job() for _ in range(5)]
    db.execute(
        "INSERT INTO board_visible(user_id,job_id) VALUES (%s,%s),(%s,%s),(%s,%s)",
        (owner, overlap, owner, hidden, other, foreign),
    )
    db.execute(
        "INSERT INTO user_jobs(user_id,job_id,notes,status,date_applied,hidden) VALUES "
        "(%s,%s,'mine','saved',NULL,FALSE), (%s,%s,'note',NULL,NULL,FALSE), "
        "(%s,%s,NULL,NULL,CURRENT_DATE,FALSE), (%s,%s,NULL,NULL,NULL,FALSE), "
        "(%s,%s,'private',NULL,NULL,FALSE), (%s,%s,'hidden','saved',NULL,TRUE)",
        (
            owner,
            overlap,
            owner,
            noted,
            owner,
            applied,
            owner,
            untouched,
            other,
            overlap,
            owner,
            hidden,
        ),
    )
    rows = db.query(
        visibility.FAST.format(
            columns="j.id, uj.notes", extra="AND NOT COALESCE(uj.hidden,FALSE) ORDER BY j.id"
        ),
        {"uid": owner},
    )
    assert [row["id"] for row in rows] == sorted([overlap, noted, applied])
    assert next(row for row in rows if row["id"] == overlap)["notes"] == "mine"
    single = db.query(
        visibility.FAST.format(columns="j.id", extra="AND j.id = %(job_id)s"),
        {"uid": owner, "job_id": foreign},
    )
    assert single == []
