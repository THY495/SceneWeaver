import asyncio

import pytest

from chatbot.performance_candidate import CandidateIdentity, prepare_candidate


def identity():
    return CandidateIdentity("player", 1, "plan", 1, "job", 1, "text-sha", "voice-scope")


@pytest.mark.asyncio
async def test_audio_ready_before_review_does_not_release_candidate():
    approved, audio_started = asyncio.Event(), asyncio.Event()

    async def review():
        await audio_started.wait()
        await approved.wait()
        return True

    async def audio():
        audio_started.set()
        return b"pcm"

    task = asyncio.create_task(prepare_candidate(identity(), review, audio, lambda: True))
    await audio_started.wait()
    assert not task.done()
    approved.set()
    result = await task
    assert result.status == "approved" and result.audio == b"pcm"


@pytest.mark.asyncio
async def test_rejected_review_cancels_inflight_audio():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def review():
        await started.wait()
        return False

    async def audio():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    result = await prepare_candidate(identity(), review, audio, lambda: True)
    assert result.status == "rejected" and result.audio is None
    assert result.audio_status == "discarded"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_rejected_review_discards_audio_that_finished_first():
    finished, release_review = asyncio.Event(), asyncio.Event()
    discarded = []

    async def review():
        await finished.wait()
        await release_review.wait()
        return False

    async def audio():
        finished.set()
        return b"private-pcm"

    async def discard(value):
        discarded.append(value)

    task = asyncio.create_task(
        prepare_candidate(identity(), review, audio, lambda: True, discard_audio=discard)
    )
    await finished.wait()
    release_review.set()
    result = await task
    assert result.status == "rejected" and result.audio is None
    assert result.audio_status == "discarded"
    assert discarded == [b"private-pcm"]


@pytest.mark.asyncio
async def test_state_change_while_audio_finishes_discards_result():
    current = True

    async def review():
        return True

    async def audio():
        nonlocal current
        current = False
        return b"stale"

    result = await prepare_candidate(identity(), review, audio, lambda: current)
    assert result.status == "expired" and result.audio is None


@pytest.mark.asyncio
async def test_audio_timeout_allows_explicit_text_fallback():
    async def review():
        return True

    async def audio():
        await asyncio.Event().wait()

    result = await prepare_candidate(identity(), review, audio, lambda: True, audio_wait_seconds=0.01)
    assert result.status == "approved" and result.audio_status == "timeout" and result.audio is None


@pytest.mark.asyncio
async def test_caller_cancellation_cleans_up_synthesis():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def review():
        await asyncio.Event().wait()

    async def audio():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(prepare_candidate(identity(), review, audio, lambda: True))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
