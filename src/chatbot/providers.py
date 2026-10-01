import asyncio
import json
import math
import time
from datetime import UTC

import httpx
from opentelemetry import trace
from sqlalchemy import func, select, text

from .config import Settings
from .observability.timing import annotate, measure, measured_slot, record_request, timed, transport_marks
from .runtime import Runtime, StaleTask
from .storage import Database, Operation, Task, Usage, now, uid

tracer = trace.get_tracer("continuum.providers")


class ProviderError(Exception):
    pass


class OutputTruncated(ProviderError):
    """The provider stopped at the requested output limit before completing a response."""


class StructuredOutputError(ProviderError):
    """Keep a malformed model result in memory for a bounded format-only repair."""

    def __init__(self, answer, errors):
        super().__init__("模型结构化结果不符合协议。")
        self.answer = answer
        self.errors = errors


class BudgetExceeded(ProviderError):
    pass


class DailyBudgetExceeded(BudgetExceeded):
    pass


class TransientProviderError(ProviderError):
    pass


def http_failure(status):
    cls = TransientProviderError if status in {408, 425, 429, 500, 502, 503, 504} else ProviderError
    return cls(f"模型服务返回 HTTP {status}；请检查凭据、模型和服务状态。")


def estimate_tokens(value) -> int:
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    nonascii = sum(ord(c) > 127 for c in content)
    return math.ceil((len(content) - nonascii) / 3 + nonascii * 2) + 32


def rates(model):
    # Conservative peak USD / million; stored as an explicit price snapshot.
    return (1.32, 0.044, 3.96) if "pro" in model else (0.44, 0.014, 1.32)


def responses_review_output(data):
    """Extract only completed assistant text; reasoning is never review output."""
    if not isinstance(data, dict):
        raise ProviderError("模型响应格式无效，审查结果不得发布。")
    status = data.get("status")
    if status == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason")
        if reason == "max_output_tokens":
            raise OutputTruncated("模型达到输出上限，结果未完成；请缩小问题。")
        raise ProviderError("模型输出未完成，审查结果不得发布。")
    if status != "completed":
        raise TransientProviderError("模型响应未完成，审查结果不得发布。")
    output = data.get("output")
    if not isinstance(output, list):
        raise ProviderError("模型响应缺少输出，审查结果不得发布。")
    return "".join(
        part.get("text", "")
        for item in output
        if isinstance(item, dict)
        if item.get("type") == "message" and item.get("role") == "assistant"
        for part in (item.get("content") if isinstance(item.get("content"), list) else [])
        if isinstance(part, dict)
        if part.get("type") == "output_text" and isinstance(part.get("text"), str)
    )


def responses_review_usage(value):
    """Normalize Responses accounting to the existing reservation ledger."""
    if not isinstance(value, dict):
        return None
    input_tokens, output_tokens = value.get("input_tokens"), value.get("output_tokens")
    cached_tokens = (value.get("input_tokens_details") or {}).get("cached_tokens", 0)
    if not all(isinstance(n, int) and n >= 0 for n in (input_tokens, output_tokens, cached_tokens)):
        return None
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "prompt_cache_hit_tokens": min(cached_tokens, input_tokens),
    }


def review_repairable(answer):
    """A repair may change presentation, never an absent or unclear verdict."""
    try:
        review = json.loads(answer)
    except (TypeError, ValueError):
        return None
    if not isinstance(review, dict) or not isinstance(review.get("accepted"), bool):
        return None
    checks, issues, delivery = review.get("checks"), review.get("issues"), review.get("delivery")
    if not isinstance(checks, list) or not isinstance(issues, list) or not isinstance(delivery, dict):
        return None
    if not all(
        isinstance(check, dict)
        and isinstance(check.get("text"), str)
        and isinstance(check.get("supported"), bool)
        for check in checks
    ) or not all(isinstance(issue, str) for issue in issues):
        return None
    return review


def review_repair_errors_supported(errors):
    """Only labels and long explanation text can be changed without re-review."""
    if not errors:
        return False
    for error in errors:
        path = error.get("path", "").split(".")
        if len(path) != 3 or path[0] != "checks" or not path[1].isdigit():
            return False
        if (path[2], error.get("type")) not in {
            ("reason_code", "literal_error"),
            ("basis", "string_too_long"),
        }:
            return False
    return True


def review_repair_preserves(original, repaired):
    if repaired.get("accepted") is not original["accepted"]:
        return False
    if repaired.get("issues") != original["issues"]:
        return False
    new_checks = repaired.get("checks")
    if not isinstance(new_checks, list) or len(new_checks) != len(original["checks"]):
        return False
    reason_codes = {
        "none",
        "unsupported_fact",
        "invented_history",
        "acquisition_mismatch",
        "statement_promotion",
        "agreement_promotion",
        "player_agency",
        "mixed_recipient",
        "other",
    }
    for before, after in zip(original["checks"], new_checks, strict=True):
        if before["text"] != after.get("text") or before["supported"] is not after.get("supported"):
            return False
        if before.get("reason_code") in reason_codes and before["reason_code"] != after.get("reason_code"):
            return False
    new_delivery = repaired.get("delivery")
    return isinstance(new_delivery, dict) and all(
        new_delivery.get(key) == value for key, value in original["delivery"].items()
    )


class Provider:
    def __init__(self, db: Database, config: Settings, runtime: Runtime):
        self.db, self.config, self.runtime = db, config, runtime
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(config.request_timeout, connect=15))
        self.slots = asyncio.Semaphore(max(2, config.worker_concurrency))
        self.background_slots = asyncio.Semaphore(max(1, config.worker_concurrency - 1))
        self.ack_slots = asyncio.Semaphore(1)

    @timed("budget.reserve")
    def reserve(self, task_id, role, model, messages, max_tokens, *, price=None, tts_quote=None):
        uncached, _, output = (
            (self.config.embedding_input_usd_per_million, 0, 0) if role == "embedding" else rates(model)
        )
        if price is not None:
            uncached, _, output = price
        reserve = (estimate_tokens(messages) * uncached + max_tokens * output) / 1e6
        if role == "tts":
            reserve = len(messages) * self.config.tts_usd_per_million_chars / 1e6
            if tts_quote is not None:
                units, unit, rate = tts_quote
                if (
                    unit not in {"characters", "utf8_bytes"}
                    or units < 0
                    or not math.isfinite(rate)
                    or rate <= 0
                ):
                    raise ProviderError("无效的语音预算报价")
                reserve = units * rate / 1e6
        if role == "rerank":
            reserve = self.config.rerank_request_usd
        if role == "asr":
            reserve = float(messages) / 60 * self.config.asr_usd_per_minute
        if role == "voice_enrollment":
            reserve = self.config.voice_enrollment_reserve_usd
        with self.db.session.begin() as s:
            s.execute(text("SELECT pg_advisory_xact_lock(731291)"))
            t = s.get(Task, task_id)
            if not t or (t.status != "running" and role != "tts"):
                raise StaleTask("任务不可执行")
            root_id = t.parent_id or t.id
            related_ids = select(Task.id).where((Task.id == root_id) | (Task.parent_id == root_id))
            day = now().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            total = s.scalar(
                select(func.coalesce(func.sum(Usage.cost + Usage.reserved), 0)).where(Usage.created_at >= day)
            )
            task_total = s.scalar(
                select(func.coalesce(func.sum(Usage.cost + Usage.reserved), 0)).where(
                    Usage.task_id.in_(related_ids)
                )
            )
            if role == "acknowledgement" and (
                total + reserve + min(0.03, self.config.turn_budget_usd / 2) > self.config.daily_budget_usd
                or task_total + reserve > self.config.turn_budget_usd / 2
                or t.calls >= max(0, self.config.max_calls_per_task - 3)
            ):
                raise BudgetExceeded("保留正文额度，跳过接话。")
            if total + reserve > self.config.daily_budget_usd:
                raise DailyBudgetExceeded("已到今日 API 预算上限；可在配置中调整。")
            if (
                task_total + reserve > self.config.turn_budget_usd
                or s.scalar(select(func.coalesce(func.sum(Task.calls), 0)).where(Task.id.in_(related_ids)))
                >= self.config.max_calls_per_task
            ):
                raise BudgetExceeded("本任务已到调用或费用上限。")
            t.calls += 1
            row = Usage(
                id=uid(), owner_id=t.owner_id, task_id=task_id, role=role, model=model, reserved=reserve
            )
            s.add(row)
            if price is not None:
                s.add(Operation(id=f"usage:{row.id}", task_id=task_id, payload={"price": list(price)}))
        return row.id

    @timed("budget.settle")
    def settle(self, usage_id, usage, duration, status, metadata=None):
        with self.db.session.begin() as s:
            row = s.get(Usage, usage_id)
            row.duration_ms = int(duration * 1000)
            row.status = status
            snapshot = s.get(Operation, f"usage:{usage_id}")
            if snapshot and metadata:
                snapshot.payload = {**snapshot.payload, **metadata}
            if usage is not None:
                row.input_tokens = usage.get("prompt_tokens", 0)
                row.output_tokens = usage.get("completion_tokens", 0)
                row.cache_tokens = usage.get(
                    "prompt_cache_hit_tokens", usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                )
                uncached, cache, output = (
                    (self.config.embedding_input_usd_per_million, 0, 0)
                    if row.role == "embedding"
                    else rates(row.model)
                )
                if snapshot:
                    uncached, cache, output = snapshot.payload["price"]
                row.cost = (
                    max(0, row.input_tokens - row.cache_tokens) * uncached
                    + row.cache_tokens * cache
                    + row.output_tokens * output
                ) / 1e6
                if snapshot:
                    actual = usage.get("cost")
                    if isinstance(actual, int | float) and math.isfinite(actual) and actual >= 0:
                        row.cost = actual
                    else:
                        row.cost = max(row.cost, row.reserved)
                        row.status = "estimated_" + status
            else:
                # Unknown/cancelled requests may have been billed: retain reservation as estimate.
                row.cost = row.reserved
                row.status = "estimated_" + status
            row.reserved = 0

    @timed("model.call")
    async def chat(self, task_id, role, messages, **kwargs):
        annotate(role=role, task_id=task_id)
        emitted = False
        callback = kwargs.get("on_delta")

        async def tracked_delta(value):
            nonlocal emitted
            emitted = True
            await callback(value)

        if callback:
            kwargs["on_delta"] = tracked_delta
        for attempt in range(2):
            try:
                return await self._chat_once(task_id, role, messages, **kwargs)
            except StructuredOutputError as exc:
                if role != "actor_review":
                    if role == "acknowledgement" or attempt or emitted:
                        raise
                    self.runtime.check(task_id)
                    with measure("model.retry_wait"):
                        await asyncio.sleep(0.5)
                    continue
                if attempt or emitted:
                    raise
                original = review_repairable(exc.answer)
                if original is None or not review_repair_errors_supported(exc.errors):
                    # No complete verdict can be preserved by a format-only
                    # repair. Re-review the unchanged candidate and evidence
                    # once; never publish or infer approval from malformed text.
                    self.runtime.check(task_id)
                    with measure("model.retry_wait"):
                        await asyncio.sleep(0.5)
                    continue
                self.runtime.check(task_id)
                repair_messages = [
                    {
                        "role": "system",
                        "content": (
                            "你只修复审查结果的JSON结构，不重新审核，不改变accepted、"
                            "checks的数量/原文/supported、issues或delivery原有字段。"
                            "可缩短超长basis或把无效reason_code改为other；"
                            "无法原样保留判断时不要猜测。只返回JSON对象。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "invalid_review": exc.answer,
                                "schema_errors": exc.errors,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
                with measure("model.format_repair"):
                    repaired = await self._chat_once(
                        task_id, role, repair_messages, **{**kwargs, "operation": None}
                    )
                    if not review_repair_preserves(original, repaired):
                        raise ProviderError("审查格式修复改变了原审查结论，拒绝发布。")
                if kwargs.get("operation"):
                    version = self.runtime.check(task_id).lease_version
                    self._save_operation(task_id, version, kwargs["operation"], repaired)
                return repaired
            except ProviderError as exc:
                retryable = any(
                    code in str(exc)
                    for code in [
                        "HTTP 429",
                        "HTTP 502",
                        "HTTP 503",
                        "HTTP 504",
                        "网络请求失败",
                        "结构化结果不符合协议",
                    ]
                )
                if role == "acknowledgement" or attempt or emitted or not retryable:
                    raise
                self.runtime.check(task_id)
                with measure("model.retry_wait"):
                    await asyncio.sleep(0.5)

    @timed("model.attempt")
    async def _chat_once(
        self,
        task_id,
        role,
        messages,
        *,
        schema=None,
        on_delta=None,
        max_tokens=1800,
        operation=None,
        thinking=False,
    ):
        annotate(role=role, task_id=task_id, stream=bool(on_delta))
        task = self.runtime.check(task_id)
        version = task.lease_version
        if operation:
            with self.db.session() as s:
                saved = s.get(Operation, f"{task_id}:{operation}")
                if saved and "value" in saved.payload:
                    annotate(cache_hit=True)
                    return saved.payload["value"]
        routed = role in self.config.openrouter_roles
        key = self.config.openrouter_key() if routed else self.config.generation_key()
        if not key:
            raise ProviderError("尚未配置生成模型 API 凭据，请设置 CHAT_API_KEY 或 CHAT_API_KEY_FILE。")
        model = self.config.expert_model if role == "expert" else self.config.dialogue_model
        responses_review = (
            role == "actor_review"
            and schema is not None
            and not routed
            and self.config.model_provider == "deepseek"
            and self.config.deepseek_review_format == "responses_json_schema"
        )
        if responses_review:
            if on_delta:
                raise ProviderError("审查 JSON Schema 实验路径不支持流式输出。")
            if model == "deepseek-v4-flash":
                model = "deepseek-flash"
            if model not in {"deepseek-flash", "deepseek-v4-pro"}:
                raise ProviderError("审查 JSON Schema 实验路径需使用 DeepSeek Responses 支持的模型。")
        price = None
        endpoint = self.config.api_base.rstrip("/") + (
            "/responses" if responses_review else "/chat/completions"
        )
        if routed:
            model = self.config.openrouter_roles[role]
            endpoint = "https://openrouter.ai/api/v1/chat/completions"
            pricing = self.config.openrouter_prices.get(model, {})
            if any(
                not isinstance(pricing.get(k), int | float) or not math.isfinite(pricing[k]) or pricing[k] < 0
                for k in ("input", "output")
            ):
                raise ProviderError("OpenRouter模型缺少有效价格快照，拒绝未计价调用。")
            if not self.config.openrouter_providers.get(model):
                raise ProviderError("OpenRouter评测必须指定供应商。")
            price = (pricing["input"], pricing["input"], pricing["output"])
        messages = list(messages)
        if schema:
            messages.append(
                {
                    "role": "system",
                    "content": "只返回符合下列 JSON Schema 的 JSON 对象，不要 Markdown："
                    + json.dumps(schema.model_json_schema(), ensure_ascii=False),
                }
            )
        if estimate_tokens(messages) + max_tokens > self.config.context_hard_tokens:
            raise ProviderError("当前工作材料超过上下文预算，请缩小资料范围或问题。")
        # Reserve one execution slot for foreground dialogue.
        background = role not in {"dialogue", "acknowledgement"}
        if background:
            with measure("model.background_wait"):
                await self.background_slots.acquire()
        try:
            async with measured_slot(self.ack_slots if role == "acknowledgement" else self.slots):
                self.runtime.check(task_id, version)
                usage_id = self.reserve(task_id, role, model, messages, max_tokens, price=price)
                annotate(model=model, usage_id=usage_id, cache_hit=False)
                start, usage, status = time.monotonic(), None, "failed"
                metadata = {"requested_model": model, "route": "openrouter"} if routed else None
                body = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "stream": bool(on_delta),
                }
                if routed:
                    body["provider"] = {
                        "order": self.config.openrouter_providers[model],
                        "allow_fallbacks": False,
                        "require_parameters": True,
                        "data_collection": "deny",
                        "zdr": self.config.openrouter_zdr,
                        "max_price": {"prompt": price[0], "completion": price[2], "request": 0},
                    }
                    effort = self.config.openrouter_reasoning.get(model)
                    if effort is not None or thinking:
                        body["reasoning"] = {"effort": effort or "low"}
                elif self.config.model_provider == "deepseek":
                    body["thinking"] = {"type": "enabled" if thinking else "disabled"}
                    if thinking:
                        body["reasoning_effort"] = "low"
                if schema:
                    body["response_format"] = (
                        {
                            "type": "json_schema",
                            "json_schema": {
                                "name": schema.__name__,
                                "strict": True,
                                "schema": schema.model_json_schema(),
                            },
                        }
                        if routed
                        else {"type": "json_object"}
                    )
                if responses_review:
                    body = {
                        "model": model,
                        "input": messages,
                        "max_output_tokens": max_tokens,
                        "stream": False,
                        "reasoning": {"effort": "low" if thinking else "none"},
                        "text": {
                            "format": {
                                "type": "json_schema",
                                "name": schema.__name__,
                                "schema": schema.model_json_schema(),
                            }
                        },
                    }
                if on_delta:
                    body["stream_options"] = {"include_usage": True}
                with tracer.start_as_current_span(f"model.{role}") as span:
                    span.set_attribute("task.id", task_id)
                    span.set_attribute("model", model)
                    try:
                        headers = {"Authorization": f"Bearer {key}"}
                        with measure("model.http") as request:
                            extensions = {"trace": transport_marks(request)}
                            record_request(
                                body,
                                role=role,
                                usage_id=usage_id,
                                estimate=estimate_tokens,
                                schema_added=bool(schema),
                            )
                            if on_delta:
                                parts = []
                                finished = False
                                async with self.http.stream(
                                    "POST",
                                    endpoint,
                                    json=body,
                                    headers=headers,
                                    extensions=extensions,
                                ) as r:
                                    request.mark("response_headers")
                                    if r.status_code != 200:
                                        raise http_failure(r.status_code)
                                    async for line in r.aiter_lines():
                                        self.runtime.check(task_id, version)
                                        if not line.startswith("data: "):
                                            continue
                                        if line[6:] == "[DONE]":
                                            break
                                        frame = json.loads(line[6:])
                                        if routed:
                                            metadata.update(
                                                {
                                                    k: frame[k]
                                                    for k in ("id", "model", "provider")
                                                    if k in frame
                                                }
                                            )
                                        if frame.get("usage"):
                                            usage = frame["usage"]
                                        for choice in frame.get("choices", []):
                                            delta = choice.get("delta", {}).get("content") or ""
                                            if delta:
                                                if routed and "first_content_ms" not in metadata:
                                                    metadata["first_content_ms"] = round(
                                                        (time.monotonic() - start) * 1000
                                                    )
                                                request.mark("first_content")
                                                parts.append(delta)
                                                await on_delta(delta)
                                            if choice.get("finish_reason") == "length":
                                                raise OutputTruncated(
                                                    "模型达到输出上限，结果未完成；请缩小问题。"
                                                )
                                            if choice.get("finish_reason") == "stop":
                                                finished = True
                                if not finished:
                                    raise TransientProviderError(
                                        "模型流意外中断，未提交不完整回答；可以重新发送。"
                                    )
                                request.mark("body_complete")
                                answer = "".join(parts)
                            else:
                                r = await self.http.post(
                                    endpoint,
                                    json=body,
                                    headers=headers,
                                    extensions=extensions,
                                )
                                if r.status_code != 200:
                                    raise http_failure(r.status_code)
                                request.mark("body_complete")
                                data = r.json()
                                if responses_review:
                                    usage = responses_review_usage(data.get("usage"))
                                    answer = responses_review_output(data)
                                else:
                                    if routed:
                                        metadata.update(
                                            {k: data[k] for k in ("id", "model", "provider") if k in data}
                                        )
                                    usage = data.get("usage")
                                    choice = data["choices"][0]
                                    if choice.get("finish_reason") == "length":
                                        raise OutputTruncated("模型达到输出上限，结果未完成；请缩小问题。")
                                    answer = choice["message"].get("content") or ""
                        self.runtime.check(task_id, version)
                        if not answer.strip():
                            raise ProviderError("模型未返回有效内容。")
                        if schema:
                            try:
                                value = schema.model_validate_json(answer).model_dump()
                            except Exception as exc:
                                errors = (
                                    [
                                        {"path": ".".join(map(str, error["loc"])), "type": error["type"]}
                                        for error in exc.errors()
                                    ]
                                    if hasattr(exc, "errors")
                                    else [{"path": "", "type": "invalid_json"}]
                                )
                                if errors:
                                    annotate(
                                        schema_error_type=errors[0]["type"],
                                        schema_error_path=errors[0]["path"],
                                    )
                                raise StructuredOutputError(answer, errors) from None
                        else:
                            value = answer
                        status = "completed"
                        if operation:
                            self._save_operation(task_id, version, operation, value)
                        return value
                    except httpx.HTTPError as exc:
                        raise TransientProviderError("模型网络请求失败或超时，请稍后重试。") from exc
                    finally:
                        self.settle(usage_id, usage, time.monotonic() - start, status, metadata=metadata)
        finally:
            if background:
                self.background_slots.release()

    def _save_operation(self, task_id, version, operation, value):
        with self.db.session.begin() as s:
            t = s.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if (
                t.status != "running"
                or t.lease_version != version
                or not t.lease_until
                or t.lease_until <= now()
            ):
                raise StaleTask("提交已过期")
            if not s.get(Operation, f"{task_id}:{operation}"):
                s.add(Operation(id=f"{task_id}:{operation}", task_id=task_id, payload={"value": value}))

    async def close(self):
        await self.http.aclose()


class Embeddings:
    def __init__(self, config, provider=None):
        self.config, self.provider = config, provider

    @timed("embedding.encode")
    async def encode(self, texts, task_id=None):
        if self.config.embedding_provider == "lexical":
            return None
        if not self.config.embedding_key():
            raise ProviderError("语义索引已启用，但未配置 embedding API key。")
        if self.provider is None or task_id is None:
            raise ProviderError("Embedding 调用需要绑定任务和预算。")
        for attempt in range(2):
            task = self.provider.runtime.check(task_id)
            usage_id = self.provider.reserve(task_id, "embedding", self.config.embedding_model, texts, 0)
            start = time.monotonic()
            usage, status = None, "failed"
            try:
                vectors, usage = await self._request(texts)
                self.provider.runtime.check(task_id, task.lease_version)
                status = "completed"
                return vectors
            except httpx.HTTPError as exc:
                retryable = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code in {
                    429,
                    502,
                    503,
                    504,
                }
                if attempt or not retryable:
                    detail = (
                        f"HTTP {exc.response.status_code}"
                        if isinstance(exc, httpx.HTTPStatusError)
                        else type(exc).__name__
                    )
                    raise ProviderError(f"Embedding 网络请求失败（{detail}），请稍后重试。") from exc
            finally:
                self.provider.settle(usage_id, usage, time.monotonic() - start, status)
            await asyncio.sleep(0.5)

    async def _request(self, texts):
        # Batch indexing returns many vectors; allow slow response transfer while bounding connection setup.
        timeout = httpx.Timeout(60, connect=10, write=20, pool=10)
        async with httpx.AsyncClient(timeout=timeout, trust_env=self.config.embedding_trust_env) as client:
            r = await client.post(
                self.config.embedding_api_base.rstrip("/") + "/embeddings",
                headers={"Authorization": "Bearer " + self.config.embedding_key()},
                json={
                    "model": self.config.embedding_model,
                    "input": texts,
                    "dimensions": self.config.embedding_dimensions,
                    "encoding_format": "float",
                },
            )
            r.raise_for_status()
            return self.parse_response(r.json(), len(texts))

    @staticmethod
    def parse_response(payload, expected):
        try:
            data = sorted(payload["data"], key=lambda x: x["index"])
            if [row["index"] for row in data] != list(range(expected)):
                raise ValueError("invalid indices")
            vectors = [row["embedding"] for row in data]
            if any(
                len(v) != 1024
                or not any(v)
                or any(type(n) not in (int, float) or not math.isfinite(n) for n in v)
                for v in vectors
            ):
                raise ValueError("invalid vector")
            usage = payload.get("usage")
            if usage is not None:
                tokens = usage.get("prompt_tokens", usage.get("input_tokens", usage.get("total_tokens")))
                usage = (
                    {"prompt_tokens": tokens, "completion_tokens": 0}
                    if isinstance(tokens, int) and tokens >= 0
                    else None
                )
            return vectors, usage
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("Embedding 返回协议无效：需要完整索引、1024 维有限非零向量。") from exc
