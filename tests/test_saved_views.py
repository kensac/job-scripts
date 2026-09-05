"""A view is a name that returns a page to a state; several per page, one
default, ordered, and only ever the caller's own."""

from __future__ import annotations


def test_views_are_per_page_ordered_with_one_default(client, user_headers, other_user_headers):
    state = {
        "filters": {"status": ["pending", "running"]},
        "sorts": [{"key": "kind", "dir": "asc"}],
    }
    r = client.post(
        "/v1/user/views",
        json={"page": "admin.queue", "name": "Live work", "state": state, "is_default": True},
        headers=user_headers,
    )
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["is_default"] is True and first["position"] == 0 and first["state"] == state
    second = client.post(
        "/v1/user/views",
        json={
            "page": "admin.queue",
            "name": "Failures",
            "state": {"filters": {"status": ["failed"]}},
        },
        headers=user_headers,
    ).json()
    assert second["position"] == 1 and second["is_default"] is False
    board = client.post(
        "/v1/user/views",
        json={
            "page": "board",
            "name": "US only",
            "state": {"filters": {"statuses": ["not_applied"]}},
        },
        headers=user_headers,
    ).json()

    queue = client.get(
        "/v1/user/views", params={"page": "admin.queue"}, headers=user_headers
    ).json()
    assert [v["name"] for v in queue["views"]] == ["Live work", "Failures"]
    everything = client.get("/v1/user/views", headers=user_headers).json()
    assert {v["page"] for v in everything["views"]} == {"admin.queue", "board"}

    made_default = client.patch(
        f"/v1/user/views/{second['id']}", json={"is_default": True}, headers=user_headers
    ).json()
    assert made_default["is_default"] is True
    queue = client.get(
        "/v1/user/views", params={"page": "admin.queue"}, headers=user_headers
    ).json()
    assert [v["is_default"] for v in queue["views"]] == [False, True]

    dup = client.post(
        "/v1/user/views",
        json={"page": "admin.queue", "name": "Failures", "state": {}},
        headers=user_headers,
    )
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "DUPLICATE_NAME"

    renamed = client.patch(
        f"/v1/user/views/{board['id']}",
        json={
            "name": "US, not applied",
            "state": {
                "filters": {"statuses": ["not_applied"]},
                "sorts": [{"key": "date_posted", "dir": "desc"}],
            },
        },
        headers=user_headers,
    ).json()
    assert (
        renamed["name"] == "US, not applied"
        and renamed["state"]["sorts"][0]["key"] == "date_posted"
    )

    theirs = client.get("/v1/user/views", headers=other_user_headers).json()
    assert theirs["views"] == []
    assert (
        client.patch(
            f"/v1/user/views/{board['id']}", json={"name": "x"}, headers=other_user_headers
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/v1/user/views/{board['id']}", headers=other_user_headers).status_code
        == 404
    )

    assert client.delete(f"/v1/user/views/{board['id']}", headers=user_headers).json() == {
        "deleted": board["id"]
    }
    assert (
        client.get("/v1/user/views", params={"page": "board"}, headers=user_headers).json()["views"]
        == []
    )

    too_big = client.post(
        "/v1/user/views",
        json={"page": "board", "name": "huge", "state": {"x": "y" * 40_000}},
        headers=user_headers,
    )
    assert too_big.status_code == 400 and too_big.json()["detail"]["code"] == "STATE_TOO_LARGE"
