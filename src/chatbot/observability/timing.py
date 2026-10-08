"""Bounded, content-free timing records; durations use the process monotonic clock."""

import asyncio
import inspect
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from uuid import uuid4

from sqlalchemy import select, text

from ..storage import Operation, Task

log = logging.getLogger(__name__)
_collector = ContextVar("timing_collector", default=None)
_parent = ContextVar("timing_parent", default=None)
_fields = {
    "actor_id",
    "role",
    "model",
    "usage_id",
    "cache_hit",
    "stream",
    "step",
    "task_id",
    "item_result_expression",
    "result_event_id",
    "candidate_status",
    "audio_status",
    "audio_error_type",
    "audio_failure_phase",
    "audio_received_close_code",
    "audio_sent_close_code",
    "audio_received_close_first",
    "voice_requested",
    "voice_available",
    "preconnect_ready",
    "voice_candidate_enabled",
    "candidate_revision",
    "preparation_concurrency",
    "local_issue_count",
    "review_accepted",
    "review_provider_accepted",
    "assignment_reply_capacity_exhausted",
    "assignment_provider_accepted",
    "assignment_evidence_invalid_count",
    "assignment_evidence_missing_fields_count",
    "assignment_evidence_extent_invalid_count",
    "assignment_evidence_source_class_invalid_count",
    "assignment_evidence_expression_source_invalid_count",
    "assignment_evidence_quote_missing_count",
    "assignment_evidence_quote_not_found_count",
    "review_issue_count",
    "review_unsupported_count",
    "job_id",
    "narrowed_addressee_count",
    "tts_connection_ms",
    "tts_connection_reused",
    "projection_status",
    "tts_task_ready_ms",
    "tts_text_submitted_ms",
    "tts_first_pcm_ms",
    "tts_complete_ms",
    "tts_receive_wait_ms",
    "tts_max_receive_wait_ms",
    "tts_consumer_hold_ms",
    "tts_last_pcm_ms",
    "tts_pcm_packets",
    "audio_bytes",
    "published_producer_count",
    "pending_published_producer_count",
    "schema_error_type",
    "schema_error_path",
    "actor_generation",
    "generation_fallback",
    "combined_speech_reused",
    "profile_reference_repair_count",
    "beat_reuse_hit",
    "beat_reuse_miss",
    "routing_scope",
    "routing_accepted",
    "player_action_preflight_hit",
    "grounded_response_pruning_disabled",
    "response_review_binding",
    "response_binding_accepted",
    "response_task_status",
    "assignment_authorization",
    "assignment_purpose",
    "director_recovery_feedback",
    "assignment_source_event_id",
    "assignment_grounded_rejected",
    "query_cache_hit",
    "query_has_indexed_memory",
    "query_inflight_joined",
    "review_explanation_mode",
}


class Span:
    def __init__(self, collector, name):
        self.collector = collector
        self.row = {
            "id": uuid4().hex,
            "parent_id": _parent.get(),
            "name": name,
            "start_ms": collector.elapsed(),
            "duration_ms": None,
            "status": "running",
            "attributes": {},
            "marks": {},
        }

    def mark(self, name):
        # Names are supplied by application code, never provider frames or prompt contents.
        self.row["marks"].setdefault(name, self.collector.elapsed() - self.row["start_ms"])


class NullSpan:
    def mark(self, name):
        pass


_transport_marks = {
    "connection.connect_tcp.started": "tcp_connect_start",
    "connection.connect_tcp.complete": "tcp_connect_complete",
    "connection.start_tls.started": "tls_start",
    "connection.start_tls.complete": "tls_complete",
    "http11.send_request_body.complete": "request_body_sent",
    "http2.send_request_body.complete": "request_body_sent",
    "http11.receive_response_headers.complete": "response_headers_received",
    "http11.receive_response.complete": "response_headers_received",
    "http2.receive_response_headers.complete": "response_headers_received",
}


def transport_marks(span):
    """Capture only known transport boundaries, never trace event payloads.

    These optional HTTPX/httpcore events are absent for mock transports and may
    change with httpcore versions. Missing marks mean unknown, not zero time.
    """

    async def mark(event_name, _info):
        label = _transport_marks.get(event_name)
        if label:
            span.mark(label)

    return mark


def transport_summary(timings):
    """Summarize observed client boundaries without inventing inference time."""

    def difference(marks, start, end):
        a, b = marks.get(start), marks.get(end)
        return (
            round(b - a, 2) if isinstance(a, int | float) and isinstance(b, int | float) and b >= a else None
        )

    rows = []
    for timing in timings:
        spans = {
            span["id"]: span
            for span in timing.get("spans", [])
            if span.get("name") == "model.http" and span.get("id")
        }
        for request in timing.get("requests", []):
            span = spans.get(request.get("span_id"))
            if not span:
                continue
            marks = span.get("marks", {})
            rows.append(
                {
                    "trace_id": timing.get("id"),
                    "usage_id": request.get("usage_id"),
                    "role": request.get("role"),
                    "stream": request.get("stream"),
                    "http_total_ms": round(span["duration_ms"], 2)
                    if isinstance(span.get("duration_ms"), int | float)
                    else None,
                    "tcp_connect_including_dns_ms": difference(
                        marks, "tcp_connect_start", "tcp_connect_complete"
                    ),
                    "tls_ms": difference(marks, "tls_start", "tls_complete"),
                    "request_sent_to_headers_ms": difference(
                        marks, "request_body_sent", "response_headers_received"
                    ),
                    "headers_to_first_content_ms": difference(
                        marks, "response_headers_received", "first_content"
                    ),
                    "headers_to_body_complete_ms": difference(
                        marks, "response_headers_received", "body_complete"
                    ),
                }
            )
    return rows


class TimingTrace:
    def __init__(self, task):
        self.id = uuid4().hex
        self.task_id = task.id
        self.attempt = task.attempt
        self.lease_version = task.lease_version
        self.started_at = datetime.now(UTC)
        self.started_ns = time.perf_counter_ns()
        self.enqueue_to_worker_ms = max(0, (self.started_at - task.created_at).total_seconds() * 1000)
        self.initial_queue_ms = (
            max(0, (task.updated_at - task.created_at).total_seconds() * 1000) if task.attempt == 1 else None
        )
        self.spans = []
        self.worksets = []
        self.requests = []
        self.message_origins = []  # Transient object references; never serialized.
        self.stage_origins = []
        self.dropped_stage_origins = 0
        self.dropped_requests = 0
        self.dropped_worksets = 0
        self.dropped = 0
        self.duration_ms = None

    def elapsed(self):
        return (time.perf_counter_ns() - self.started_ns) / 1e6

    @contextmanager
    def activate(self):
        token = _collector.set(self)
        parent = _parent.set(None)
        try:
            yield self
        finally:
            self.duration_ms = self.elapsed()
            _parent.reset(parent)
            _collector.reset(token)

    def save(self, db):
        """Best-effort final flush, outside world transactions; never stores text or errors."""
        try:
            with db.session.begin() as s:
                s.execute(text("SET LOCAL statement_timeout = '500ms'"))
                s.execute(text("SET LOCAL lock_timeout = '250ms'"))
                task = s.get(Task, self.task_id)
                if not task or (task.result or {}).get("redacted"):
                    return
                for span in self.spans:
                    if span["name"] == "worker.execute":
                        span["status"] = task.status
                s.add(
                    Operation(
                        id=f"timing:{self.task_id}:{self.id}",
                        task_id=self.task_id,
                        payload={
                            "timing_v1": {
                                "id": self.id,
                                "task_id": self.task_id,
                                "attempt": self.attempt,
                                "lease_version": self.lease_version,
                                "task_status": task.status,
                                "started_at": self.started_at.isoformat(),
                                "duration_ms": self.duration_ms,
                                "enqueue_to_worker_ms": self.enqueue_to_worker_ms,
                                "initial_queue_ms": self.initial_queue_ms,
                                "spans": self.spans,
                                "worksets": self.worksets,
                                "requests": self.requests,
                                "dropped_requests": self.dropped_requests,
                                "dropped_worksets": self.dropped_worksets,
                                "dropped_stage_origins": self.dropped_stage_origins,
                                "dropped_spans": self.dropped,
                                "clock": "process_monotonic",
                                "complete": True,
                            }
                        },
                    )
                )
        except Exception:
            # Log neither exception text nor a database URL: telemetry must not leak configuration.
            log.warning("timing_flush_failed task=%s", self.task_id)


@contextmanager
def measure(name):
    collector = _collector.get()
    if collector is None or len(collector.spans) >= 512:
        if collector:
            collector.dropped += 1
        yield NullSpan()
        return
    span = Span(collector, name)
    collector.spans.append(span.row)
    token = _parent.set(span.row["id"])
    try:
        yield span
    except BaseException as exc:
        span.row["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
        raise
    else:
        span.row["status"] = "ok"
    finally:
        span.row["duration_ms"] = collector.elapsed() - span.row["start_ms"]
        _parent.reset(token)


def annotate(**values):
    collector = _collector.get()
    if collector:
        row = next((s for s in reversed(collector.spans) if s["id"] == _parent.get()), None)
        if row is not None:
            row["attributes"].update(
                {
                    k: v
                    for k, v in values.items()
                    if k in _fields and isinstance(v, str | int | float | bool | None)
                }
            )


def record_workset(
    *,
    actor_id,
    revision,
    privacy_version,
    snapshot_id,
    tokens,
    observation_ids,
    memory_ids,
    messages=(),
    selection=None,
):
    """Capture assembly provenance only, not a copy of private context or model reasoning."""
    collector = _collector.get()
    if collector is None or len(collector.worksets) >= 64:
        if collector is not None:
            collector.dropped_worksets += 1
        return
    workset_id = uuid4().hex
    for message, section in zip(messages, ("role_policy", "role_workset", "new_observations")):
        collector.message_origins.append((message, workset_id, section))
    collector.worksets.append(
        {
            "id": workset_id,
            "span_id": _parent.get(),
            "actor_id": actor_id,
            "revision": revision,
            "privacy_version": privacy_version,
            "snapshot_id": snapshot_id,
            "estimated_tokens": tokens,
            "observation_ids": observation_ids[:256],
            "memory_ids": memory_ids[:256],
            "omitted_id_count": max(0, len(observation_ids) - 256) + max(0, len(memory_ids) - 256),
            "scope": "assembled_role_context_before_stage_prompt",
            "selection": selection or {},
        }
    )


def audit_message(message, *, section, groups):
    """Attach identifier-only provenance to a prepared message without changing its payload."""
    collector = _collector.get()
    if collector is not None:
        if len(collector.stage_origins) >= 256:
            collector.dropped_stage_origins += 1
        else:
            allowed = {
                "documents",
                "capacity_omitted",
                "excerpt_truncated",
                "references",
                "recent_utterances",
                "confirmed_events",
                "item_action_results",
                "profile",
                "subjective_goals",
                "player_statements",
            }
            metadata = {}
            for key, values in groups.items():
                if key not in allowed:
                    continue
                ids = list(dict.fromkeys(str(value) for value in values if value is not None))
                metadata[key] = {
                    "ids": [value[:128] for value in ids[:64]],
                    "total": len(ids),
                    "omitted": max(0, len(ids) - 64),
                }
            collector.stage_origins.append((message, section, metadata))
    return message


def record_request(body, *, role, usage_id, estimate, schema_added=False):
    """Record the actual transport input shape, never text, credentials or content hashes.

    Dictionary identity links unchanged assembly containers; copied/rebuilt review inputs
    remain unattributed instead of guessing their provenance from matching text.
    """
    collector = _collector.get()
    if collector is None:
        return
    if len(collector.requests) >= 128:
        collector.dropped_requests += 1
        return
    messages = body.get("messages", body.get("input", []))
    components = []
    for index, message in enumerate(messages[:128]):
        origin = next((item for item in reversed(collector.message_origins) if item[0] is message), None)
        stage = next((item for item in reversed(collector.stage_origins) if item[0] is message), None)
        components.append(
            {
                "index": index,
                "role": message.get("role")
                if message.get("role") in {"system", "user", "assistant", "tool"}
                else "other",
                "characters": len(message.get("content", ""))
                if isinstance(message.get("content", ""), str)
                else None,
                "estimated_tokens": estimate([message]),
                "workset_id": origin[1] if origin else None,
                "source_groups": stage[2] if stage else {},
                "section": stage[1]
                if stage
                else origin[2]
                if origin
                else "output_schema"
                if schema_added and index == len(messages) - 1
                else "stage_message",
            }
        )
    collector.requests.append(
        {
            "span_id": _parent.get(),
            "usage_id": usage_id,
            "role": role,
            "message_count": len(messages),
            "estimated_tokens": estimate(messages),
            "max_output_tokens": body.get("max_tokens", body.get("max_output_tokens")),
            "stream": body["stream"],
            "response_format": body.get("response_format", {}).get("type")
            or (body.get("text", {}).get("format") or {}).get("type"),
            "components": components,
            "omitted_components": max(0, len(messages) - 128),
            "boundary": "transport_dispatch_attempt",
        }
    )


def timed(name):
    def decorate(fn):
        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def asynchronous(*args, **kwargs):
                with measure(name):
                    return await fn(*args, **kwargs)

            return asynchronous

        @wraps(fn)
        def synchronous(*args, **kwargs):
            with measure(name):
                return fn(*args, **kwargs)

        return synchronous

    return decorate


@asynccontextmanager
async def measured_slot(semaphore):
    with measure("model.slot_wait"):
        await semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()


def read_timings(session, task_id):
    records = session.scalars(
        select(Operation)
        .where(Operation.task_id == task_id, Operation.id.startswith("timing:"))
        .order_by(Operation.created_at)
        .limit(12)
    )
    return [r.payload["timing_v1"] for r in records if "timing_v1" in r.payload]
