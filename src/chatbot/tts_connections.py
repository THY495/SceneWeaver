"""Exclusive, bounded WebSocket leases; only completed synthesis is reusable."""

import asyncio
import hashlib
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from websockets.protocol import State


@dataclass(eq=False)
class ConnectionLease:
    context: Any
    socket: Any
    key: str
    reusable: bool = False
    reused: bool = False
    idle_at: float = 0


class TTSConnections:
    def __init__(self, limit=4, idle_seconds=45):
        if limit < 1 or not 0 < idle_seconds < 60:
            raise ValueError("Invalid TTS connection bounds")
        self.slots = asyncio.Semaphore(limit)
        self.idle_seconds = idle_seconds
        self.idle = []
        self.active = set()
        self.closed = False

    @asynccontextmanager
    async def lease(self, endpoint, credential, factory):
        key = hashlib.sha256((endpoint + "\0" + credential).encode()).hexdigest()
        async with self.slots:
            if self.closed:
                raise RuntimeError("TTS connections closed")
            lease = None
            # No awaits while detaching idle entries: concurrent borrowers cannot share one.
            stale = []
            while self.idle:
                candidate = self.idle.pop()
                if (
                    candidate.key == key
                    and time.monotonic() - candidate.idle_at < self.idle_seconds
                    and getattr(candidate.socket, "state", State.OPEN) == State.OPEN
                ):
                    lease = candidate
                    lease.reused = True
                    break
                stale.append(candidate)
            completed = False
            try:
                while stale:
                    await stale[-1].context.__aexit__(None, None, None)
                    stale.pop()
                if lease is None:
                    context = factory()
                    socket = await context.__aenter__()
                    lease = ConnectionLease(context, socket, key)
                lease.reusable = False
                self.active.add(lease)
                if self.closed:
                    raise RuntimeError("TTS connections closed")
                yield lease
                completed = True
            finally:
                self.active.discard(lease)
                if lease and completed and lease.reusable and not self.closed:
                    lease.idle_at = time.monotonic()
                    self.idle.append(lease)
                elif lease:
                    stale.append(lease)
                await asyncio.gather(*(old.context.__aexit__(None, None, None) for old in stale))

    async def close(self):
        self.closed = True
        idle, self.idle = self.idle, []
        await asyncio.gather(*(entry.context.__aexit__(None, None, None) for entry in idle))
        # Active borrowers retain ownership and close in their finally blocks.
