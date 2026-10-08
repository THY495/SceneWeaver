"""Join semantic review and optional private audio without publishing either."""

from ..observability.timing import annotate, measure, timed
from ..performance_candidate import prepare_candidate
from ..runtime import StaleTask


@timed("npc.review_audio")
async def reviewed_audio(
    identity, review_call, synthesize, is_current, *, discard_audio=None, synthesize_after_review=False
):
    result = None
    annotate(job_id=identity.job_id, candidate_revision=identity.candidate_revision)

    async def review():
        nonlocal result
        result = await review_call()
        return (
            result["accepted"]
            and not result.get("issues")
            and all(c["supported"] for c in result.get("checks", []))
        )

    async def audio():
        with measure("voice.candidate_prepare"):
            annotate(role="tts", job_id=identity.job_id, candidate_revision=identity.candidate_revision)
            return await synthesize()

    prepared = await prepare_candidate(
        identity,
        review,
        audio if synthesize else None,
        is_current,
        discard_audio=discard_audio,
        synthesize_after_review=synthesize_after_review,
        # A repaired line starts TTS only after review, so the normal three
        # second *post-review* grace period otherwise cancels a healthy first
        # chunk. The original candidate remains speculative and short-waited.
        audio_wait_seconds=8 if synthesize_after_review else 3,
    )
    annotate(candidate_status=prepared.status, audio_status=prepared.audio_status)
    if prepared.status == "expired":
        raise StaleTask("角色候选准备期间状态已变化。")
    return result, prepared.audio, prepared.audio_status
