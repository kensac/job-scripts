"""Shared setup and response helpers for filter insight tests."""

from core.store import add_ai_result


def _reject(url: str, reason: str, prompt_hash: str, filter_name: str) -> None:
    add_ai_result(
        url,
        "rejected",
        reason,
        "custom",
        prompt_hash=prompt_hash,
        filter_name=filter_name,
    )


def _get(client, admin_headers, **params):
    resp = client.get(
        "/v1/admin/filter-insights/rejection-reasons",
        params={"min_decisions": 1, **params},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _phrasings(client, admin_headers, **params):
    resp = client.get("/v1/admin/filter-insights/phrasings", params=params, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    return resp.json()
