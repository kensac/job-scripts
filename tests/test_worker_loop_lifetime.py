import asyncio

import pytest

from api import worker


def test_worker_keeps_one_event_loop_for_pooled_clients(monkeypatch):
    class StopWorker(Exception):
        pass

    loops = []

    async def run_once():
        if len(loops) == 2:
            raise StopWorker
        loops.append(asyncio.get_running_loop())
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(worker, "run_once", run_once)
    monkeypatch.setattr(worker.db, "init_schema", lambda: None)
    monkeypatch.setattr(worker.db, "execute", lambda *a, **kw: None)
    monkeypatch.setattr(worker.metrics, "serve", lambda: None)
    monkeypatch.setattr(worker, "_seed_gauges", lambda: None)
    monkeypatch.setattr(worker.telemetry, "init", lambda *a: None)
    monkeypatch.setattr(worker.telemetry, "capture", lambda *a, **kw: None)
    monkeypatch.setattr(worker.signal, "signal", lambda *a: None)
    monkeypatch.setattr(worker.time, "monotonic", lambda: 0)
    with pytest.raises(StopWorker):
        worker.main()
    assert len(loops) == 2
    assert loops[0] is loops[1]
    assert loops[0].is_closed()
