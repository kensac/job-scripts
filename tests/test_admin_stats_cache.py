"""GET /admin/stats is five full scans of ai_queries, asked for on every
worker event. It answers from a cache for admin_stats_cache_seconds."""

from __future__ import annotations

from api.routers import admin


def test_stats_are_served_from_cache_within_the_window(client, admin_headers, monkeypatch):
    admin._stats_cache = None
    scans: list[int] = []
    real = admin._compute_stats

    def counted():
        scans.append(1)
        return real()

    monkeypatch.setattr(admin, "_compute_stats", counted)

    first = client.get("/v1/admin/stats", headers=admin_headers)
    second = client.get("/v1/admin/stats", headers=admin_headers)
    assert first.status_code == 200 and first.json() == second.json()
    assert len(scans) == 1

    admin._stats_cache = None
    client.get("/v1/admin/stats", headers=admin_headers)
    assert len(scans) == 2
    assert (
        client.put(
            "/v1/admin/config/admin_stats_cache_seconds", json={"value": 1}, headers=admin_headers
        ).status_code
        == 200
    )
