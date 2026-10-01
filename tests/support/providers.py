"""Shared providers test helpers."""

import asyncio


class FakeProvider:
    def __init__(self, replies=None, delay=0):
        self.replies = replies or {}
        self.delay = delay
        self.calls = []
        self.active = 0
        self.peak = 0

    async def chat(self, task_id, role, messages, **kwargs):
        if messages == [{"role": "user", "content": "Reply with OK."}]:
            return "OK"
        self.calls.append((role, messages, kwargs))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            schema = kwargs.get("schema")
            if role in self.replies:
                value = self.replies[role]
                if callable(value):
                    value = value(messages)
                if isinstance(value, Exception):
                    raise value
            elif schema and schema.__name__ == "MemoryExtraction":
                value = {"candidates": []}
            elif schema and schema.__name__ == "ContextSummary":
                value = {
                    "summary": "用户目标：完成项目；预算5000；尚待核对交期。",
                    "constraints": ["预算5000"],
                    "open_items": ["核对交期"],
                }
            elif schema and schema.__name__ == "SearchPlan":
                value = {"queries": [], "sufficient": True, "missing": []}
            elif schema and schema.__name__ == "WorkPlan":
                value = {
                    "tasks": [
                        {"id": "a", "question": "性能", "depends_on": []},
                        {"id": "b", "question": "成本", "depends_on": []},
                    ]
                }
            elif schema and schema.__name__ == "Route":
                value = {"route": "retrieve", "reason": "needs evidence"}
            elif schema and schema.__name__ == "ActorReview":
                value = {"accepted": True, "issues": []}
            elif schema and schema.__name__ == "ActorIntent":
                value = {
                    "act": "speak",
                    "purpose": "回应",
                    "addressee_id": None,
                    "source_ids": [],
                    "wait_for_player": True,
                    "deception": False,
                }
            elif schema and schema.__name__ == "RoleReflection":
                value = {"summary": "角色记得有来源的公开发言；传闻尚未确认。", "notes": []}
            elif schema and schema.__name__ == "ActorSpeech":
                value = {"text": "我暂时无法确认，可以先核对记录。", "claims": []}
            else:
                value = "已核对资料，项目预算为5000元。"
            if schema and schema.__name__ == "ActorSpeech" and isinstance(value, str):
                value = {"text": value, "claims": []}
            if kwargs.get("on_delta") and isinstance(value, str):
                await kwargs["on_delta"](value)
            return value
        finally:
            self.active -= 1

    async def close(self):
        pass
