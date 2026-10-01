"""Budgeted model checks before starting/resuming player runs. No credentials in responses."""

import asyncio
import hashlib
import json
import time
from datetime import timedelta

from fastapi import HTTPException

from .providers import ProviderError
from .storage import Task, now, uid


class ModelReadiness:
    def __init__(self, config, db, provider):
        self.config, self.db, self.provider = config, db, provider
        self.signature, self.until = "", 0
        self.lock = asyncio.Lock()

    def status(self):
        try:
            signature = self.fingerprint()
        except HTTPException as exc:
            return {"configured": False, "verified": False, "message": exc.detail}
        verified = signature == self.signature and time.monotonic() < self.until
        return {
            "configured": True,
            "verified": verified,
            "message": "LLM 最近的连接检查通过。" if verified else "已配置 LLM，开始前将检查连接和模型权限。",
        }

    def fingerprint(self):
        c = self.config
        try:
            key, routed = c.generation_key(), c.openrouter_key()
        except OSError as exc:
            raise HTTPException(409, "无法读取 LLM 密钥文件，请在本机配置后重试。") from exc
        if not key or (c.openrouter_roles and not routed):
            raise HTTPException(409, "开始游戏前必须配置可用的 LLM API Key；当前生成或分流密钥缺失。")
        return hashlib.sha256(
            json.dumps(
                [
                    key,
                    routed,
                    c.api_base,
                    c.dialogue_model,
                    c.expert_model,
                    c.openrouter_roles,
                    c.openrouter_providers,
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()

    async def require(self):
        async with self.lock:
            signature = self.fingerprint()
            if signature == self.signature and time.monotonic() < self.until:
                return
            c = self.config
            roles = ["dialogue", "expert", *c.openrouter_roles]
            checked = set()
            task_id = uid()
            scope = getattr(getattr(self.provider, "runtime", None), "execution_scope", None)
            with self.db.session.begin() as s:
                s.add(
                    Task(
                        id=task_id,
                        owner_id=c.workspace_owner,
                        kind="connection_check",
                        status="running",
                        deadline=now() + timedelta(seconds=90),
                        lease_until=now() + timedelta(seconds=90),
                        payload={"release_id": scope.release_id} if scope else {},
                    )
                )
            success = False
            try:
                async with asyncio.timeout(60):
                    for role in roles:
                        default_model = c.expert_model if role == "expert" else c.dialogue_model
                        route = (role in c.openrouter_roles, c.openrouter_roles.get(role, default_model))
                        if route in checked:
                            continue
                        result = await self.provider.chat(
                            task_id, role, [{"role": "user", "content": "Reply with OK."}], max_tokens=8
                        )
                        if not result or not str(result).strip():
                            raise ProviderError("模型未返回有效内容")
                        checked.add(route)
                success = True
                self.signature, self.until = signature, time.monotonic() + 900
            except Exception as exc:
                self.signature, self.until = "", 0
                raise HTTPException(
                    409, "LLM 可用性检查失败：请检查密钥、模型权限、网络与预算，再重试。未开始游戏。"
                ) from exc
            finally:
                with self.db.session.begin() as s:
                    task = s.get(Task, task_id)
                    task.status = "completed" if success else "failed"
