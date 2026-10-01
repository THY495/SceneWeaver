"""Private first-sentence audio values; no player-facing identifiers or routes."""

import re
from dataclasses import dataclass

from .voice import validate_spoken_parts


def first_sentence(text: str, spoken_parts: list[str], limit=80) -> str:
    validate_spoken_parts(text, spoken_parts)
    if not spoken_parts:
        return ""
    part = spoken_parts[0]
    sentence = re.match(r"[^。！？!?；;\n]+[。！？!?；;\n]*", part)
    return (sentence.group() if sentence else part)[:limit]


def aligned_performance(spoken_parts, performance):
    """Use one validated projection for both speculative TTS and publication."""
    return performance if [part["text"] for part in performance] == spoken_parts else []


@dataclass(frozen=True)
class CandidateAudio:
    text: str
    voice: str
    direction: str
    scope: str
    pcm: bytes
    spool_id: str | None = None
