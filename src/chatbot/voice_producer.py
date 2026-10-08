"""Own speculative PCM producers until publication or disposal."""

import asyncio
from dataclasses import replace

from . import voice_spool
from .observability.timing import annotate, measure, timed
from .providers import ProviderError
from .storage import Operation


class CandidateProducers:
    def __init__(self, service):
        self.service = service
        self.entries = []

    async def start(self, task, text, voice, direction):
        ready = asyncio.get_running_loop().create_future()
        entry = {"key": None, "task_id": task.id, "offset": 0, "audio": None}
        self.entries.append(entry)

        def append(audio):
            aligned = len(audio.pcm) // 2 * 2
            if aligned <= entry["offset"]:
                return
            with self.service.db.session.begin() as s:
                if entry["key"] is None:
                    entry["key"] = voice_spool.create(
                        s,
                        task,
                        text_value=audio.text,
                        voice=audio.voice,
                        direction=audio.direction,
                        scope=audio.scope,
                    )
                    s.flush()
                entry["offset"] = voice_spool.append(
                    s, entry["key"], task.id, entry["offset"], audio.pcm[entry["offset"] : aligned]
                )
            entry["audio"] = replace(audio, pcm=audio.pcm[:aligned], spool_id=entry["key"])
            # 100 ms of complete samples avoids exposing a single-byte fragment.
            if aligned >= 4800 and not ready.done():
                ready.set_result(entry["audio"])

        async def pump():
            try:
                final = await self.service.buffer_candidate(task, text, voice, direction, on_audio=append)
                append(final)
                with self.service.db.session.begin() as s:
                    voice_spool.finish(s, entry["key"], task.id, successful=True)
                annotate(audio_status="completed")
                if not ready.done():
                    ready.set_result(entry["audio"])
            except BaseException as exc:
                # The exception class is local code metadata; never export its
                # message, provider frame, URL or credentials into a timing trace.
                annotate(
                    audio_status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                    audio_error_type=type(exc).__name__,
                )
                if entry["key"]:
                    with self.service.db.session.begin() as s:
                        row = s.get(Operation, entry["key"])
                        if row and row.payload["status"] == "producing":
                            voice_spool.finish(s, entry["key"], task.id, successful=False)
                if not ready.done():
                    ready.set_exception(
                        exc if isinstance(exc, Exception) else ProviderError("语音生成已取消。")
                    )

        async def produce():
            with measure("voice.candidate_stream"):
                annotate(role="tts", task_id=task.id)
                await pump()

        entry["producer"] = asyncio.create_task(produce())
        try:
            return await asyncio.shield(ready)
        except BaseException:
            ready.cancel()
            await self._discard(entry)
            raise

    async def _discard(self, entry):
        producer = entry["producer"]
        if not producer.done():
            producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
        if entry["key"]:
            with self.service.db.session.begin() as s:
                row = s.get(Operation, entry["key"])
                if row and row.payload["event_id"] is None:
                    voice_spool.discard(s, entry["key"], entry["task_id"])

    async def discard(self, audio):
        for entry in self.entries:
            if entry["key"] == audio.spool_id:
                await self._discard(entry)
                return

    @timed("voice.producer_finalize")
    async def close(self):
        """Drain published tails while the turn lease is alive; discard the rest."""
        try:
            published = []
            for entry in self.entries:
                with self.service.db.session() as s:
                    row = s.get(Operation, entry["key"]) if entry["key"] else None
                    bound = bool(row and row.payload["event_id"])
                if bound:
                    published.append(entry["producer"])
                else:
                    with measure("voice.unpublished_discard"):
                        await self._discard(entry)
            with measure("voice.published_drain"):
                annotate(
                    published_producer_count=len(published),
                    pending_published_producer_count=sum(not producer.done() for producer in published),
                )
                if published:
                    await asyncio.gather(*published)
        finally:
            with measure("voice.producer_cleanup"):
                for entry in self.entries:
                    await self._discard(entry)
                self.entries.clear()
