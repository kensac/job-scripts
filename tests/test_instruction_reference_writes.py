from api import db
from core import store
from core.query_instructions import hydrate


def test_new_results_store_only_exact_shared_instruction_text():
    values = [None, "", "Same rules\n", "Same rules\n", "Same rules \n", "café"]
    ids = [
        store.add_ai_result(
            f"https://example.test/reference-only/{index}",
            "passed",
            instructions=value,
            prompt_hash=f"identity-{index}",
        )
        for index, value in enumerate(values)
    ]
    rows = db.query("SELECT * FROM ai_queries WHERE id=ANY(%s) ORDER BY id", (ids,))
    assert len(rows) == len(values)
    assert rows[0]["instructions_id"] is None
    assert all(row["instructions_id"] is not None for row in rows[1:])
    assert rows[2]["instructions_id"] == rows[3]["instructions_id"]
    assert rows[3]["instructions_id"] != rows[4]["instructions_id"]
    assert [row["instructions"] for row in hydrate(rows)] == values
    assert [row["prompt_hash"] for row in rows] == [f"identity-{i}" for i in range(len(values))]
