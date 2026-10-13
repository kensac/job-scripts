"""api.stale_cache: one value per key, served stale, warmable."""

import threading

from api.stale_cache import Stale, StaleByKey


def test_keys_are_separate_and_cached():
    calls: list[int] = []
    cache: StaleByKey[int, int] = StaleByKey(lambda: 3600)

    def compute(n):
        calls.append(n)
        return n * 10

    assert cache.get(1, lambda: compute(1)) == 10
    assert cache.get(2, lambda: compute(2)) == 20
    assert cache.get(1, lambda: compute(1)) == 10
    assert calls == [1, 2]


def test_warm_computes_in_the_background():
    done = threading.Event()

    def compute():
        done.set()
        return "ready"

    cache = Stale(compute, lambda: 3600)
    cache.warm()
    assert done.wait(5)
    assert cache.get() == "ready"
