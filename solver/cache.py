"""TTL cache + per-key async lock.

In-memory implementation; swap for Redis-backed in production by replacing
the AsyncTTLCache class while keeping the same interface.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict


class AsyncTTLCache:
    def __init__(self, maxsize: int = 50_000, ttl: float = 24 * 3600) -> None:
        self.maxsize = int(maxsize)
        self.ttl = float(ttl)
        self._store: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._mutex = asyncio.Lock()

    async def get(self, key: str) -> object | None:
        async with self._mutex:
            if key not in self._store:
                return None
            expires, value = self._store[key]
            if time.time() > expires:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return value

    async def set(self, key: str, value: object) -> None:
        async with self._mutex:
            self._store[key] = (time.time() + self.ttl, value)
            self._store.move_to_end(key)
            while len(self._store) > self.maxsize:
                self._store.popitem(last=False)

    async def size(self) -> int:
        async with self._mutex:
            return len(self._store)


class KeyedLocks:
    """One asyncio.Lock per key, created on demand. Cleans up unused locks."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._counts: dict[str, int] = {}
        self._mutex = asyncio.Lock()

    async def acquire(self, key: str) -> asyncio.Lock:
        async with self._mutex:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            self._counts[key] = self._counts.get(key, 0) + 1
        return lock

    async def release(self, key: str) -> None:
        async with self._mutex:
            self._counts[key] = self._counts.get(key, 1) - 1
            if self._counts[key] <= 0:
                self._counts.pop(key, None)
                self._locks.pop(key, None)
