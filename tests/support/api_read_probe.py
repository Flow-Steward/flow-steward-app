"""Exercise HTTP response ordering while a synchronous read waits on a barrier."""

from __future__ import annotations

import asyncio
import threading

import httpx


class BlockedRead:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def returning(self, value):
        def read(*_args, **_kwargs):
            self.started.set()
            # Only a deadlock escape for the broken handler: the heartbeat
            # releases a correctly offloaded read without waiting this long.
            self.release.wait(timeout=1.0)
            self.finished.set()
            return value

        return read


async def probe_while_read_waits(app, path, reader):
    @app.get("/test-heartbeat")
    async def heartbeat():
        read_finished = reader.finished.is_set()
        reader.release.set()
        return {"read_finished": read_finished}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        pending = asyncio.create_task(client.get(path))
        try:
            assert await asyncio.to_thread(reader.started.wait, 5.0), "Read did not start"
            heartbeat_response = await client.get("/test-heartbeat")
            response = await pending
        finally:
            reader.release.set()
            if not pending.done():
                await pending
    return heartbeat_response, response
