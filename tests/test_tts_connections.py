import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from websockets.protocol import State

from chatbot.tts_connections import TTSConnections


def connection():
    value = AsyncMock()
    value.__aenter__.return_value = object()
    return value


async def test_only_complete_exclusive_tasks_are_reused():
    pool = TTSConnections(limit=2)
    first, second = connection(), connection()
    async with pool.lease("url", "key", lambda: first) as a:
        async with pool.lease("url", "key", lambda: second) as b:
            assert a.socket is not b.socket
            b.reusable = True
        a.reusable = True
    async with pool.lease("url", "key", lambda: pytest.fail("should reuse")) as reused:
        assert reused.reused
        reused.reusable = True
    await pool.close()
    first.__aexit__.assert_awaited_once()
    second.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("mode", ["early", "error", "cancel"])
async def test_partial_failed_and_cancelled_tasks_are_never_reused(mode):
    pool, source = TTSConnections(), connection()
    try:
        async with pool.lease("url", "key", lambda: source) as lease:
            if mode == "error":
                lease.reusable = True
                raise ValueError("failed after completion")
            if mode == "cancel":
                raise asyncio.CancelledError
    except (ValueError, asyncio.CancelledError):
        pass
    assert pool.idle == [] and pool.active == set()
    source.__aexit__.assert_awaited_once()
    await pool.close()


@pytest.mark.parametrize("changed", ["credential", "endpoint", "expired"])
async def test_scope_changes_and_idle_expiry_force_new_connection(changed, monkeypatch):
    pool = TTSConnections()
    old, new = connection(), connection()
    async with pool.lease("url", "key", lambda: old) as lease:
        lease.reusable = True
    if changed == "expired":
        monkeypatch.setattr("chatbot.tts_connections.time.monotonic", lambda: lease.idle_at + 46)
    async with pool.lease(
        "new" if changed == "endpoint" else "url", "new" if changed == "credential" else "key", lambda: new
    ) as lease:
        assert not lease.reused
        lease.reusable = True
    old.__aexit__.assert_awaited_once()
    await pool.close()
    new.__aexit__.assert_awaited_once()


async def test_shutdown_prevents_requeue_and_new_borrowers():
    pool, source = TTSConnections(), connection()
    async with pool.lease("url", "key", lambda: source) as lease:
        await pool.close()
        lease.reusable = True
    assert not pool.idle
    source.__aexit__.assert_awaited_once()
    with pytest.raises(RuntimeError):
        async with pool.lease("url", "key", lambda: pytest.fail("closed")):
            pass


async def test_cancel_one_borrower_does_not_interrupt_another_or_leak_capacity():
    pool = TTSConnections(limit=2)
    cancelled, healthy = connection(), connection()
    started = asyncio.Event()

    async def borrower():
        async with pool.lease("url", "key", lambda: cancelled):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(borrower())
    await asyncio.wait_for(started.wait(), 1)
    async with pool.lease("url", "key", lambda: healthy) as lease:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cancelled.__aexit__.assert_awaited_once()
        healthy.__aexit__.assert_not_awaited()
        lease.reusable = True
    async with pool.lease("url", "key", lambda: pytest.fail("healthy connection lost")) as lease:
        assert lease.reused
        lease.reusable = True
    await pool.close()
    assert not pool.active and not pool.idle


async def test_shutdown_during_connection_establishment_closes_new_socket():
    pool, source = TTSConnections(), connection()
    entered, release = asyncio.Event(), asyncio.Event()

    async def enter():
        entered.set()
        await release.wait()
        return object()

    source.__aenter__.side_effect = enter

    async def borrower():
        async with pool.lease("url", "key", lambda: source):
            pytest.fail("closed service must not hand out a new socket")

    task = asyncio.create_task(borrower())
    await asyncio.wait_for(entered.wait(), 1)
    await pool.close()
    release.set()
    with pytest.raises(RuntimeError):
        await task
    source.__aexit__.assert_awaited_once()
    assert not pool.active and not pool.idle


async def test_cancel_waiting_borrower_does_not_consume_slot_or_create_socket():
    pool, source = TTSConnections(limit=1), connection()
    waiting = asyncio.Event()

    async def borrower():
        waiting.set()
        async with pool.lease("url", "key", lambda: pytest.fail("capacity exceeded")):
            pass

    async with pool.lease("url", "key", lambda: source) as lease:
        task = asyncio.create_task(borrower())
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lease.reusable = True
    async with pool.lease("url", "key", lambda: pytest.fail("slot or connection lost")) as lease:
        lease.reusable = True
    await pool.close()


@pytest.mark.parametrize("state", [State.CLOSING, State.CLOSED])
async def test_server_closed_idle_socket_is_replaced_without_submitting_text_to_it(state):
    pool, old, fresh = TTSConnections(), connection(), connection()
    socket = SimpleNamespace(state=State.OPEN)
    old.__aenter__.return_value = socket
    async with pool.lease("url", "key", lambda: old) as lease:
        lease.reusable = True
    socket.state = state
    async with pool.lease("url", "key", lambda: fresh) as lease:
        assert not lease.reused and lease.socket is not socket
        lease.reusable = True
    old.__aexit__.assert_awaited_once()
    await pool.close()
