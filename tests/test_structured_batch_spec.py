from pydantic import BaseModel

from core import batch


class ExampleAnswer(BaseModel):
    accepted: bool


def test_structured_response_spec_derives_its_schema_and_name_together():
    spec = batch.structured_response_spec(
        "request-1",
        "judge this",
        "the input",
        ExampleAnswer,
        context={"subject": 42},
    )

    assert spec.schema_name == "ExampleAnswer"
    assert spec.schema == {
        "type": "object",
        "properties": {"accepted": {"type": "boolean", "title": "Accepted"}},
        "required": ["accepted"],
        "title": "ExampleAnswer",
        "additionalProperties": False,
    }
    assert (spec.custom_id, spec.instructions, spec.input, spec.context) == (
        "request-1",
        "judge this",
        "the input",
        {"subject": 42},
    )


def test_embedding_specs_remain_the_distinct_batch_shape():
    spec = batch.BatchSpec(
        "embedding-wave",
        inputs=["first", "second"],
        endpoint="/v1/embeddings",
        context={"dimensions": 1536},
    )

    assert spec.inputs == ["first", "second"]
    assert spec.endpoint == "/v1/embeddings"
    assert spec.instructions == "" and spec.schema == {}
