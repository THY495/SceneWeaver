"""Fixed transport metadata; never serialize exception messages or close reasons."""

from websockets.exceptions import ConnectionClosed

PHASES = frozenset(
    {
        "connect",
        "submit_task",
        "await_start",
        "submit_text",
        "receive_audio",
        "deliver_audio",
        "authorize",
        "reserve",
        "validate_pcm",
        "commit_pcm",
        "completed",
    }
)


def failure_fields(exc, phase):
    result = {"failure_type": type(exc).__name__, "failure_phase": phase if phase in PHASES else "unknown"}
    if isinstance(exc, ConnectionClosed):
        for frame, key in ((exc.rcvd, "received_close_code"), (exc.sent, "sent_close_code")):
            code = getattr(frame, "code", None)
            if type(code) is int and 1000 <= code <= 4999:
                result[key] = code
        if exc.rcvd is not None and exc.sent is not None and type(exc.rcvd_then_sent) is bool:
            result["received_close_first"] = exc.rcvd_then_sent
    return result


def trace_fields(fields):
    return {
        target: fields[source]
        for source, target in (
            ("failure_type", "audio_error_type"),
            ("failure_phase", "audio_failure_phase"),
            ("received_close_code", "audio_received_close_code"),
            ("sent_close_code", "audio_sent_close_code"),
            ("received_close_first", "audio_received_close_first"),
        )
        if source in fields
    }
