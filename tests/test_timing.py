import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from support.scene import world

from chatbot.api import create_app
from chatbot.observability.timing import (
    TimingTrace,
    annotate,
    measure,
    measured_slot,
    read_timings,
    transport_marks,
    transport_summary,
)
from chatbot.providers import Provider
from chatbot.scene_tasks import schedule_scenes
from chatbot.storage import now
from chatbot.worker import Worker


def sample_task():
    return SimpleNamespace(id="sample", attempt=1, lease_version=1, created_at=now(), updated_at=now())


def test_capacity_diagnostics_keep_only_counts():
    trace = TimingTrace(sample_task())
    metrics = dict(estimated_input_tokens=23800, output_reserve_tokens=900,
                   context_hard_tokens=24000, stage_request_tokens=25000,
                   stage_reserved_tokens=12000, stage_workset_tokens=11000, capacity_pass=1)
    with trace.activate(), measure("npc.plan"):
        annotate(**metrics, messages="private", api_key="secret")
    assert trace.spans[0]["attributes"] == metrics


def test_invitation_and_recovery_diagnostics_survive_content_free_trace():
    trace = TimingTrace(sample_task())
    with trace.activate(), measure("npc.assignment"):
        annotate(assignment_purpose="invite_player", director_recovery_feedback="stale",
                 basis="private evidence", rejected_text="private premise")
    assert trace.spans[0]["attributes"] == {
        "assignment_purpose": "invite_player", "director_recovery_feedback": "stale",
    }


def test_candidate_transport_metrics_survive_trace_without_audio_or_secrets():
    trace = TimingTrace(sample_task())
    metrics = dict(
        tts_receive_wait_ms=400,
        tts_max_receive_wait_ms=200,
        tts_consumer_hold_ms=700,
        tts_last_pcm_ms=900,
        tts_pcm_packets=2,
        tts_connection_reused=True,
        projection_status="repaired",
    )
    with trace.activate(), measure("voice.candidate_prepare"):
        annotate(**metrics, pcm="private audio", api_key="secret")
    assert trace.spans[0]["attributes"] == metrics


def test_result_expression_trace_records_selection_without_source_text():
    trace = TimingTrace(sample_task())
    with trace.activate(), measure("npc.perform"):
        annotate(item_result_expression=True, result_event_id="result-id", text="private")
    assert trace.spans[0]["attributes"] == {
        "item_result_expression": True,
        "result_event_id": "result-id",
    }


def test_actor_pipeline_trace_records_mode_without_candidate_contents():
    trace = TimingTrace(sample_task())
    with trace.activate(), measure("npc.plan"):
        annotate(actor_generation="combined_context", generation_fallback="knowledge_documents",
                 combined_speech_reused=False, speech="private", basis_digest="private hash")
    assert trace.spans[0]["attributes"] == {
        "actor_generation": "combined_context", "generation_fallback": "knowledge_documents",
        "combined_speech_reused": False,
    }


async def test_parallel_spans_keep_parent_and_cancellation_without_contents():
    trace = TimingTrace(sample_task())

    async def child(name):
        with measure(name):
            annotate(role="dialogue", prompt="private text never recorded", api_key="secret")
            await asyncio.sleep(0.02)

    with trace.activate():
        with measure("parent"):
            await asyncio.gather(child("a"), child("b"))
            pending = asyncio.create_task(child("cancelled"))
            await asyncio.sleep(0)
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    parent, a, b, cancelled = trace.spans
    assert a["parent_id"] == b["parent_id"] == cancelled["parent_id"] == parent["id"]
    assert b["start_ms"] < a["start_ms"] + a["duration_ms"]
    assert cancelled["status"] == "cancelled"
    assert a["attributes"] == {"role": "dialogue"}
    assert "private text" not in json.dumps(trace.spans)


async def test_slot_cancellation_does_not_leak_or_release_unowned_capacity():
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    trace = TimingTrace(sample_task())

    async def blocked():
        async with measured_slot(semaphore):
            pytest.fail("No capacity was released")

    with trace.activate():
        task = asyncio.create_task(blocked())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert semaphore.locked()
    assert trace.spans[0]["status"] == "cancelled"
    semaphore.release()


async def test_retry_gets_separate_attempt_and_backoff_spans(db, config, runtime):
    c = runtime.create_session()
    runtime.submit(c.id, "local", "retry", "retry")
    task = runtime.claim("retry")
    provider = Provider(db, config, runtime)
    await provider.http.aclose()
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        return (
            httpx.Response(503)
            if calls == 1
            else httpx.Response(
                200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
            )
        )

    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    trace = TimingTrace(task)
    try:
        with trace.activate():
            assert await provider.chat(task.id, "dialogue", []) == "ok"
    finally:
        await provider.close()
    assert [s["status"] for s in trace.spans if s["name"] == "model.attempt"] == ["error", "ok"]
    assert len([s for s in trace.spans if s["name"] == "model.retry_wait"]) == 1


def test_trace_is_bounded_and_failed_flush_does_not_raise():
    trace = TimingTrace(sample_task())
    with trace.activate():
        for _ in range(520):
            with measure("stage"):
                pass
    assert len(trace.spans) == 512 and trace.dropped == 8
    trace.save(None)


class StreamingBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(0.01)
        yield b'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}\n\n'
        await asyncio.sleep(0.01)
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'


@pytest.mark.parametrize("stream", [True, False])
async def test_provider_http_marks_and_cache_preserve_timing_boundaries(db, config, runtime, stream):
    session = runtime.create_session()
    runtime.submit(session.id, "local", "secret-player-text", "timing")
    task = runtime.claim("timing")
    provider = Provider(db, config, runtime)
    await provider.http.aclose()
    requests = []

    def respond(request):
        requests.append(request)
        if stream:
            return httpx.Response(200, stream=StreamingBody())
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}]}
        )

    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    deltas = []

    async def delta(value):
        deltas.append(value)

    trace = TimingTrace(task)
    try:
        with trace.activate():
            for _ in range(2):
                assert (
                    await provider.chat(
                        task.id,
                        "dialogue",
                        [{"role": "user", "content": "secret-player-text"}],
                        on_delta=delta if stream else None,
                        operation="same",
                    )
                    == "Hello"
                )
        trace.save(db)
    finally:
        await provider.close()
    assert len(requests) == 1
    assert callable(requests[0].extensions["trace"])
    assert len(trace.requests) == 1  # The second call was served from cache.
    sent = json.loads(requests[0].content)
    assert trace.requests[0]["message_count"] == len(sent["messages"])
    assert trace.requests[0]["components"][0]["characters"] == len(sent["messages"][0]["content"])
    assert trace.requests[0]["usage_id"]
    http = [s for s in trace.spans if s["name"] == "model.http"]
    assert len(http) == 1
    marks = http[0]["marks"]
    assert "body_complete" in marks
    if stream:
        assert marks["response_headers"] <= marks["first_content"] <= marks["body_complete"]
        assert deltas == ["Hello"]
    else:
        assert "first_content" not in marks and "response_headers" not in marks
    attempts = [s for s in trace.spans if s["name"] == "model.attempt"]
    assert attempts[0]["attributes"]["usage_id"]
    assert attempts[1]["attributes"]["cache_hit"]
    with db.session() as s:
        records = read_timings(s, task.id)
    assert len(records) == 1 and "secret-player-text" not in json.dumps(records)


async def test_httpx_transport_marks_are_observed_on_loopback_without_payload():
    async def respond(_reader, writer):
        await _reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    trace = TimingTrace(sample_task())
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            with trace.activate(), measure("model.http") as span:
                response = await client.get(
                    f"http://127.0.0.1:{port}/secret-path",
                    extensions={"trace": transport_marks(span)},
                )
                assert response.text == "ok"
                span.mark("body_complete")
    finally:
        server.close()
        await server.wait_closed()
    marks = trace.spans[0]["marks"]
    assert marks["tcp_connect_start"] <= marks["tcp_connect_complete"]
    assert marks["tcp_connect_complete"] <= marks["request_body_sent"]
    assert marks["request_body_sent"] <= marks["response_headers_received"] <= marks["body_complete"]
    assert "secret-path" not in json.dumps(trace.spans)
    trace.requests.append(
        {"span_id": trace.spans[0]["id"], "usage_id": "local", "role": "dialogue", "stream": False}
    )
    summary = transport_summary([{"id": trace.id, "spans": trace.spans, "requests": trace.requests}])
    assert len(summary) == 1
    assert summary[0]["tcp_connect_including_dns_ms"] >= 0
    assert summary[0]["request_sent_to_headers_ms"] >= 0
    assert summary[0]["tls_ms"] is None
    assert summary[0]["headers_to_first_content_ms"] is None
    assert "secret-path" not in json.dumps(summary)
    assert summary[0]["request_boundary_verified"] is True


async def test_proxy_connect_cannot_supply_application_request_boundaries():
    trace = TimingTrace(sample_task())
    with trace.activate(), measure("model.http") as span:
        callback = transport_marks(span)
        await callback("http11.send_request_headers.started", {
            "request": SimpleNamespace(method=b"CONNECT", headers="private-key", url="secret-proxy"),
        })
        await callback("http11.send_request_body.complete", {})
        await callback("http11.receive_response_headers.complete", {"return_value": "private-headers"})
        marks = span.row["marks"]
        assert "request_body_sent" not in marks and "response_headers_received" not in marks
        assert "proxy_connect_request_body_sent" in marks
        await callback("proxy.start_tls.started", {})
        await callback("proxy.start_tls.complete", {})
        await callback("http2.send_request_headers.started", {"request": SimpleNamespace(method=b"POST")})
        await callback("http2.send_request_body.complete", {})
        await callback("http2.receive_response_headers.complete", {})
        assert marks["proxy_connect_response_headers_received"] <= marks["tls_start"]
        assert marks["tls_complete"] <= marks["request_body_sent"] <= marks["response_headers_received"]
        assert "secret-proxy" not in json.dumps(span.row) and "private" not in json.dumps(span.row)


async def test_missing_method_does_not_invent_application_timing():
    trace = TimingTrace(sample_task())
    with trace.activate(), measure("model.http") as span:
        callback = transport_marks(span)
        await callback("http11.send_request_body.complete", {})
        await callback("http11.receive_response_headers.complete", {})
        assert "request_body_sent" not in span.row["marks"]
        assert "response_headers_received" not in span.row["marks"]


def test_legacy_transport_summary_keeps_total_but_withholds_unverified_intervals():
    legacy = {"id": "old", "spans": [{"id": "span", "name": "model.http", "duration_ms": 1250,
              "marks": {"request_body_sent": 10, "response_headers_received": 20, "body_complete": 1200}}],
              "requests": [{"span_id": "span", "usage_id": "usage", "role": "dialogue", "stream": False}]}
    before = json.dumps(legacy)
    row = transport_summary([legacy])[0]
    assert row["http_total_ms"] == 1250
    assert row["request_boundary_verified"] is False
    assert row["request_sent_to_headers_ms"] is None and row["headers_to_body_complete_ms"] is None
    assert json.dumps(legacy) == before


async def test_worker_scene_trace_is_creator_scoped(db, config, runtime, fake, tmp_path):
    story, _, _, _ = world(db, config, runtime, fake)
    tid = schedule_scenes(db, runtime, config.workspace_owner)
    task = runtime.claim("timing", background=True)
    await Worker(db, config, fake).run_task(task)
    with TestClient(create_app(config, db, fake)) as client:
        listing = client.get(f"/api/role/stories/{story}/traces")
        assert listing.status_code == 200 and listing.json()[0]["id"] == tid
        trace = client.get(f"/api/role/stories/{story}/tasks/{tid}/trace").json()
        names = {span["name"] for span in trace["timings"][0]["spans"]}
        assert {"npc.turn", "npc.plan", "npc.perform", "context.assemble", "world.commit"} <= names
        assert trace["timings"][0]["task_status"] == "completed"
        assert trace["timings"][0]["initial_queue_ms"] >= 0
        worksets = trace["timings"][0]["worksets"]
        assert worksets and all(w["estimated_tokens"] > 0 for w in worksets)
        assert all(w["scope"] == "assembled_role_context_before_stage_prompt" for w in worksets)
        assert all("content" not in w and "profile" not in w for w in worksets)
        assert all(w["selection"]["recall_before"] >= w["selection"]["recall_after"] for w in worksets)
        assert trace["timings"][0]["requests"] == []  # FakeProvider never dispatches HTTP.
        (tmp_path / "timing-ui-fixture.json").write_text(
            json.dumps({"tasks": listing.json(), "trace": trace, "model_mode": "fake"}, ensure_ascii=False),
            encoding="utf-8",
        )
        other, _, _, _ = world(db, config, runtime, fake)
        assert client.get(f"/api/role/stories/{other}/traces").json() == []
        assert client.get(f"/api/role/stories/{other}/tasks/{tid}/trace").status_code == 404


async def test_request_origins_are_identity_scoped_and_bounded():
    from chatbot.observability.timing import record_request, record_workset
    from chatbot.providers import estimate_tokens

    trace = TimingTrace(sample_task())

    async def actor(name):
        messages = [
            {"role": "system", "content": "private-policy"},
            {"role": "user", "content": "private-state"},
        ]
        record_workset(
            actor_id=name,
            revision=1,
            privacy_version=1,
            snapshot_id=None,
            tokens=10,
            observation_ids=[name + "-event"],
            memory_ids=[],
            messages=messages,
        )
        await asyncio.sleep(0)
        body = {"messages": [*messages, dict(messages[1])], "max_tokens": 10, "stream": False}
        record_request(body, role=name, usage_id=name, estimate=estimate_tokens)

    with trace.activate():
        await asyncio.gather(actor("a"), actor("b"))
        first, second = trace.requests
        assert first["components"][1]["workset_id"] != second["components"][1]["workset_id"]
        assert all(r["components"][2]["workset_id"] is None for r in trace.requests)
        for _ in range(130):
            record_request(
                {"messages": [], "max_tokens": 1, "stream": False},
                role="test",
                usage_id="id",
                estimate=estimate_tokens,
            )
    assert len(trace.requests) == 128 and trace.dropped_requests == 4
    assert "private-state" not in json.dumps(trace.requests + trace.worksets)


async def test_final_request_includes_provider_schema(db, config, runtime, tmp_path):
    from pydantic import BaseModel

    class Answer(BaseModel):
        answer: str

    c = runtime.create_session()
    runtime.submit(c.id, "local", "schema", "schema")
    task = runtime.claim("schema")
    provider = Provider(db, config, runtime)
    await provider.http.aclose()
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"answer":"ok"}'}, "finish_reason": "stop"}]}
        )

    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    trace = TimingTrace(task)
    try:
        with trace.activate():
            assert await provider.chat(
                task.id, "dialogue", [{"role": "user", "content": "private-question"}], schema=Answer
            ) == {"answer": "ok"}
    finally:
        await provider.close()
    record = trace.requests[0]
    assert len(sent[0]["messages"]) == record["message_count"] == 2
    assert record["components"][-1]["section"] == "output_schema"
    assert record["components"][-1]["characters"] == len(sent[0]["messages"][-1]["content"])
    assert record["response_format"] == sent[0]["response_format"]["type"]
    assert "private-question" not in json.dumps(record)
    (tmp_path / "request-ui-fixture.json").write_text(json.dumps(trace.requests), encoding="utf-8")
