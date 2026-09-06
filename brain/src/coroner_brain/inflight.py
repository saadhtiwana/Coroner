"""In-flight incidents: verdicts waiting for a human decision.

A verdict that offers approval is held here from delivery until the decision
arrives or the deadline passes. Redis is the intended store, so a restart of
the brain does not lose the question a human is about to answer. Without a
Redis URL the store is process memory, which is enough to run the system on
a laptop and is logged at startup as the limitation it is.

The ledger, not this store, is the record. A row here is a pointer to a
pending question; the answer is written to the ledger and the pointer is
removed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol

KEY_PREFIX = "coroner:inflight:"
INDEX_KEY = "coroner:inflight"


class InFlightStore(Protocol):
    name: str

    def put(self, incident_id: str, record: dict[str, Any], ttl_seconds: int) -> None: ...

    def get(self, incident_id: str) -> dict[str, Any] | None: ...

    def delete(self, incident_id: str) -> None: ...

    def ids(self) -> list[str]: ...


class MemoryStore:
    """Process memory. Does not survive a restart."""

    name = "memory"

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}

    def put(self, incident_id: str, record: dict[str, Any], ttl_seconds: int) -> None:
        self._items[incident_id] = (self._clock() + ttl_seconds, json.loads(json.dumps(record)))

    def get(self, incident_id: str) -> dict[str, Any] | None:
        item = self._items.get(incident_id)
        if item is None:
            return None
        expires, record = item
        if self._clock() >= expires:
            del self._items[incident_id]
            return None
        copied: dict[str, Any] = json.loads(json.dumps(record))
        return copied

    def delete(self, incident_id: str) -> None:
        self._items.pop(incident_id, None)

    def ids(self) -> list[str]:
        return [i for i in list(self._items) if self.get(i) is not None]


class RedisStore:
    """Redis. Each record is a JSON string under its own key with a TTL, and
    an index set names the pending ids so they can be swept for expiry."""

    name = "redis"

    def __init__(self, client: Any) -> None:  # noqa: ANN401 - redis.Redis or a fake
        self._r = client

    @classmethod
    def from_url(cls, url: str) -> RedisStore:
        import redis

        client = redis.Redis.from_url(url, decode_responses=True)
        client.ping()
        return cls(client)

    def put(self, incident_id: str, record: dict[str, Any], ttl_seconds: int) -> None:
        pipe = self._r.pipeline()
        pipe.set(KEY_PREFIX + incident_id, json.dumps(record), ex=max(1, ttl_seconds))
        pipe.sadd(INDEX_KEY, incident_id)
        pipe.execute()

    def get(self, incident_id: str) -> dict[str, Any] | None:
        raw = self._r.get(KEY_PREFIX + incident_id)
        if raw is None:
            self._r.srem(INDEX_KEY, incident_id)
            return None
        record = json.loads(raw)
        assert isinstance(record, dict)
        return record

    def delete(self, incident_id: str) -> None:
        pipe = self._r.pipeline()
        pipe.delete(KEY_PREFIX + incident_id)
        pipe.srem(INDEX_KEY, incident_id)
        pipe.execute()

    def ids(self) -> list[str]:
        members = self._r.smembers(INDEX_KEY)
        return sorted(m for m in members if self.get(m) is not None)


def build_store(redis_url: str | None) -> InFlightStore:
    if redis_url:
        return RedisStore.from_url(redis_url)
    return MemoryStore()
