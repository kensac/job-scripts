from api import db
from core import store


def test_identical_instructions_share_storage_without_changing_verdict_identity():
    ids = [
        store.add_ai_result(
            f"https://example.test/{index}",
            "passed",
            check_type="custom",
            prompt_hash=f"verdict-identity-{index}",
            instructions="Preserve exact instructions.\n",
            input_content="Cached page content",
        )
        for index in range(2)
    ]
    rows = db.query(
        "SELECT id,instructions,input_content,prompt_hash,to_jsonb(q)->'instructions_id' AS reference "
        "FROM ai_queries q WHERE id=ANY(%s) ORDER BY id",
        (ids,),
    )
    assert rows[0]["reference"] is not None
    assert rows[0]["reference"] == rows[1]["reference"]
    assert [r["prompt_hash"] for r in rows] == ["verdict-identity-0", "verdict-identity-1"]
    assert all(r["instructions"] == "Preserve exact instructions.\n" for r in rows)
    assert all(r["input_content"] == "Cached page content" for r in rows)


def test_null_empty_and_whitespace_instruction_values_remain_distinct():
    values = [None, "", "x", "x "]
    ids = [
        store.add_ai_result("https://example.test/values", "passed", instructions=s) for s in values
    ]
    rows = db.query(
        "SELECT instructions,to_jsonb(q)->'instructions_id' AS reference "
        "FROM ai_queries q WHERE id=ANY(%s) ORDER BY id",
        (ids,),
    )
    assert [r["instructions"] for r in rows] == values
    assert rows[0]["reference"] is None
    assert all(r["reference"] is not None for r in rows[1:])
    assert len({r["reference"] for r in rows[1:]}) == 3
