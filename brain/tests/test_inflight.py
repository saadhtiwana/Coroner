"""The in-flight store, in memory and against a fake Redis."""

from __future__ import annotations

import fakeredis
import pytest

from coroner_brain.inflight import INDEX_KEY, KEY_PREFIX, InFlightStore, MemoryStore, RedisStore


class FakeClock:
    t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(params=["memory", "redis"])
def store(request: pytest.FixtureRequest) -> InFlightStore:
    if request.param == "memory":
        return MemoryStore(clock=FakeClock())
    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def test_put_get_delete_and_ids(store: InFlightStore) -> None:
    assert store.ids() == []
    store.put("inc-1", {"incident_id": "inc-1", "n": 1}, ttl_seconds=60)
    store.put("inc-2", {"incident_id": "inc-2", "n": 2}, ttl_seconds=60)
    assert store.get("inc-1") == {"incident_id": "inc-1", "n": 1}
    assert sorted(store.ids()) == ["inc-1", "inc-2"]
    store.delete("inc-1")
    assert store.get("inc-1") is None
    assert store.ids() == ["inc-2"]
    store.delete("inc-missing")


def test_get_returns_a_copy(store: InFlightStore) -> None:
    store.put("inc-1", {"incident_id": "inc-1", "list": [1]}, ttl_seconds=60)
    first = store.get("inc-1")
    assert first is not None
    first["list"].append(2)
    assert store.get("inc-1") == {"incident_id": "inc-1", "list": [1]}


def test_memory_store_expires() -> None:
    clock = FakeClock()
    store = MemoryStore(clock=clock)
    store.put("inc-1", {"incident_id": "inc-1"}, ttl_seconds=10)
    clock.t += 9
    assert store.get("inc-1") is not None
    clock.t += 2
    assert store.get("inc-1") is None
    assert store.ids() == []


def test_redis_store_sets_a_ttl_and_prunes_the_index() -> None:
    r = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(r)
    store.put("inc-1", {"incident_id": "inc-1"}, ttl_seconds=120)
    assert 0 < r.ttl(KEY_PREFIX + "inc-1") <= 120
    assert r.smembers(INDEX_KEY) == {"inc-1"}
    # Simulate Redis expiring the key while the index still names it.
    r.delete(KEY_PREFIX + "inc-1")
    assert store.ids() == []
    assert r.smembers(INDEX_KEY) == set()
