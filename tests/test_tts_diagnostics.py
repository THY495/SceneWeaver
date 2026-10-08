import json

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from chatbot.tts_diagnostics import failure_fields, trace_fields


@pytest.mark.parametrize(
    "received,sent,first,expected",
    [
        (
            Close(1011, "private-provider-reason"),
            Close(1000, "secret"),
            True,
            {"received_close_code": 1011, "sent_close_code": 1000, "received_close_first": True},
        ),
        (None, Close(1011, "secret"), None, {"sent_close_code": 1011}),
        (None, None, None, {}),
        (Close(9999, "secret"), Close(True, "secret"), False, {"received_close_first": False}),
    ],
)
def test_close_diagnostics_do_not_invent_missing_frames_or_copy_reason(received, sent, first, expected):
    exc = ConnectionClosedError(received, sent, first)
    value = failure_fields(exc, "receive_audio")
    assert value == {"failure_type": "ConnectionClosedError", "failure_phase": "receive_audio", **expected}
    assert "secret" not in json.dumps(value) and "private" not in json.dumps(value)
    assert all(key.startswith("audio_") for key in trace_fields(value))


def test_other_exceptions_cannot_spoof_websocket_frames_or_include_unbounded_phase():
    error = RuntimeError("secret token in message")
    error.rcvd = Close(1011, "secret")
    assert failure_fields(error, "private url") == {
        "failure_type": "RuntimeError",
        "failure_phase": "unknown",
    }
