from fastapi.testclient import TestClient

from api import db
from api.app import app


def test_an_unhandled_error_answers_in_the_problem_shape_without_its_text(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("secret detail from the database")

    monkeypatch.setattr(db, "query_one", boom)
    resp = TestClient(app, raise_server_exceptions=False).get("/healthz")

    assert resp.status_code == 500
    assert resp.json()["detail"]["code"] == "INTERNAL_ERROR"
    assert "secret detail" not in resp.text
