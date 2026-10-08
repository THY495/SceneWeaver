"""Private speculative preparation. This module never publishes story events."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateIdentity:
    session_id: str
    epoch: int
    plan_id: str
    plan_revision: int
    job_id: str
    candidate_revision: int
    text_digest: str
    voice_scope: str


@dataclass(frozen=True)
class PreparedCandidate[Audio]:
    identity: CandidateIdentity
    status: str
    audio: Audio | None = None
    audio_status: str = "not_requested"


async def prepare_candidate[Audio](
    identity: CandidateIdentity,
    review: Callable[[], Awaitable[bool]],
    synthesize: Callable[[], Awaitable[Audio]] | None,
    is_current: Callable[[], bool],
    *,
    audio_wait_seconds: float = 3,
    discard_audio: Callable[[Audio], Awaitable[None]] | None = None,
    synthesize_after_review: bool = False,
) -> PreparedCandidate[Audio]:
    """Run approval and optional TTS together; caller owns budget and publication.

    Audio wait is bounded *after* approval. A ready result is not a speech fact:
    the publication transaction must recheck identity/dependencies/permissions.
    Cancellation does not imply that the supplier refunds submitted text.
    """
    if audio_wait_seconds <= 0:
        raise ValueError("Audio wait must be positive")
    if not is_current():
        return PreparedCandidate(identity, "expired")
    audio_task = asyncio.create_task(synthesize()) if synthesize and not synthesize_after_review else None
    keep_audio = False
    try:
        accepted = await review()
        if not is_current():
            return PreparedCandidate(
                identity, "expired", audio_status="discarded" if audio_task else "not_requested"
            )
        if not accepted:
            return PreparedCandidate(
                identity, "rejected", audio_status="discarded" if audio_task else "not_requested"
            )
        if synthesize and synthesize_after_review:
            audio_task = asyncio.create_task(synthesize())
        if not audio_task:
            return PreparedCandidate(identity, "approved")
        try:
            audio = await asyncio.wait_for(audio_task, audio_wait_seconds)
        except TimeoutError:
            return PreparedCandidate(
                identity, "approved" if is_current() else "expired", audio_status="timeout"
            )
        except Exception:
            return PreparedCandidate(
                identity, "approved" if is_current() else "expired", audio_status="failed"
            )
        if not is_current():
            return PreparedCandidate(identity, "expired", audio_status="discarded")
        keep_audio = True
        return PreparedCandidate(identity, "approved", audio=audio, audio_status="ready")
    finally:
        if audio_task:
            if not audio_task.done():
                audio_task.cancel()
            await asyncio.gather(audio_task, return_exceptions=True)
            if (
                not keep_audio
                and discard_audio
                and not audio_task.cancelled()
                and audio_task.exception() is None
            ):
                await discard_audio(audio_task.result())
