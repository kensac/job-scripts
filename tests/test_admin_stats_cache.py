"""GET /admin/stats is a full scan of ai_queries, asked for on every
worker event. It answers from a cache for admin_stats_cache_seconds."""

from __future__ import annotations

from api.routers.admin import ledger_stats


def test_stats_are_served_from_cache_within_the_window(client, admin_headers, monkeypatch):
    ledger_stats._stats.clear()
    scans: list[int] = []
    real = ledger_stats._compute_stats

    def counted():
        scans.append(1)
        return real()

    monkeypatch.setattr(ledger_stats, "_compute_stats", counted)

    first = client.get("/v1/admin/stats", headers=admin_headers)
    second = client.get("/v1/admin/stats", headers=admin_headers)
    assert first.status_code == 200 and first.json() == second.json()
    assert len(scans) == 1

    ledger_stats._stats.clear()
    client.get("/v1/admin/stats", headers=admin_headers)
    assert len(scans) == 2
    assert (
        client.put(
            "/v1/admin/config/admin_stats_cache_seconds", json={"value": 1}, headers=admin_headers
        ).status_code
        == 200
    )


def test_a_stale_cache_is_served_while_one_thread_recomputes(client, admin_headers, monkeypatch):
    from api import stale_cache

    first = ledger_stats._compute_stats()
    ledger_stats._stats._value = (-1e9, first)  # long expired
    started: list[object] = []

    class Thread:
        def __init__(self, target, daemon):
            started.append(target)

        def start(self):
            pass

    monkeypatch.setattr(stale_cache.threading, "Thread", Thread)
    got = client.get("/v1/admin/stats", headers=admin_headers)
    again = client.get("/v1/admin/stats", headers=admin_headers)
    assert got.json() == again.json() == first.model_dump(mode="json")
    # One refresh is in flight; the second request did not start another.
    assert len(started) == 1
    started[0]()  # the refresh runs, stores, and frees the lock
    assert ledger_stats._stats._value[0] > 0
    assert ledger_stats._stats._refreshing.acquire(blocking=False)
    ledger_stats._stats._refreshing.release()
