import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text

from .agents import Agents
from .characters import character_router, validate_profile
from .config import Settings, settings
from .experiment_archive import archive_router
from .experiment_comparison import comparison_router
from .experiment_initials import experiment_router
from .experiment_notes import notes_router
from .items.api import authoring_router
from .items.runtime_api import runtime_router as item_runtime_router
from .product_boundary import has_role_state
from .project_workspace import project_router
from .providers import Provider, estimate_tokens
from .role_api import role_router
from .runtime import Runtime
from .schemas import MemoryEdit, SceneFact, SessionProfile, SubmitMessage
from .storage import (
    Chunk,
    Conversation,
    Database,
    Document,
    Event,
    Evidence,
    Memory,
    Message,
    Story,
    Task,
    Tombstone,
    Usage,
    uid,
)
from .stories import story_router
from .story_packages import package_router
from .version import PUBLIC_VERSION


def create_app(
    config: Settings | None = None,
    db: Database | None = None,
    provider=None,
    *,
    web_dir: Path | None = None,
    serve_web: bool = True,
):
    config = config or settings()
    db = db or Database(config)
    runtime = Runtime(db, config)
    provider = provider or Provider(db, config, runtime)
    from .service_readiness import ModelReadiness

    runtime.model_readiness = ModelReadiness(config, db, provider)
    agents = Agents(db, config, runtime, provider)
    from .voice import VoiceService, voice_router

    voice = VoiceService(db, config, runtime)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await provider.close()
        await voice.close()

    app = FastAPI(title="sceneweaver", version=PUBLIC_VERSION, lifespan=lifespan)
    app.state.db, app.state.runtime, app.state.agents = db, runtime, agents
    app.state.voice = voice

    @app.middleware("http")
    async def origin_check(request, call_next):
        origin = request.headers.get("origin")
        allowed = {
            f"http://127.0.0.1:{config.port}",
            f"http://localhost:{config.port}",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            str(request.base_url).rstrip("/"),
        }
        if origin and origin not in allowed:
            return JSONResponse({"detail": "不允许的请求来源"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def owner(authorization: str | None = Header(default=None)):
        token = config.access_token.get_secret_value()
        if token and not secrets.compare_digest(authorization or "", "Bearer " + token):
            raise HTTPException(401, "需要本地访问令牌")
        return config.workspace_owner

    def role_owner(who=Depends(owner)):
        if config.product != "roleplay":
            raise HTTPException(404, "此功能不属于当前产品")
        return who

    def conversation(s, sid, who):
        c = s.scalar(select(Conversation).where(Conversation.id == sid, Conversation.owner_id == who))
        if not c:
            raise HTTPException(404, "会话不存在")
        return c

    app.state.owner_dependency = owner

    @app.get("/api/runtime/readiness")
    def readiness(who=Depends(owner)):
        return {"llm": runtime.model_readiness.status(), "tts_configured": voice.available()}

    @app.post("/api/runtime/check")
    async def check_readiness(who=Depends(owner)):
        await runtime.model_readiness.require()
        return runtime.model_readiness.status()

    app.include_router(character_router(db, config, runtime, owner))
    app.include_router(story_router(db, config, runtime, owner))
    app.include_router(role_router(db, config, runtime, owner))
    app.include_router(experiment_router(db, config, runtime, owner))
    app.include_router(authoring_router(owner))
    app.include_router(item_runtime_router(db, config, owner))
    from .presentation import presentation_router

    app.include_router(presentation_router(db, config, owner))
    app.include_router(package_router(db, config, owner))
    app.include_router(project_router(db, config, owner))
    app.include_router(archive_router(db, config, owner))
    app.include_router(notes_router(db, config, owner))
    app.include_router(comparison_router(db, config, owner))
    app.include_router(voice_router(voice, owner))
    from .voice_references import reference_router

    app.include_router(reference_router(voice, owner))
    from .speech_input import speech_input_router

    app.include_router(speech_input_router(db, config, runtime, provider, owner))

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"detail": "对象不存在或不可访问"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/health")
    def health():
        with db.session() as s:
            s.execute(select(1))
        return {"status": "ok", "version": PUBLIC_VERSION}

    @app.get("/api/config")
    def public_config(who=Depends(owner)):
        return {
            "product": config.product,
            "provider": config.model_provider,
            "dialogue_model": config.dialogue_model,
            "expert_model": config.expert_model,
            "generation_configured": bool(config.generation_key()),
            "embedding_mode": config.embedding_provider,
            "daily_budget_usd": config.daily_budget_usd,
            "turn_budget_usd": config.turn_budget_usd,
            "context_soft_tokens": config.context_soft_tokens,
        }

    @app.get("/api/sessions")
    def sessions(who=Depends(owner)):
        with db.session() as s:
            rows = s.scalars(
                select(Conversation)
                .where(Conversation.owner_id == who)
                .order_by(Conversation.created_at.desc())
            )
            return [
                {"id": c.id, "title": c.title, "epoch": c.epoch, "created_at": c.created_at} for c in rows
            ]

    @app.post("/api/sessions")
    def new_session(who=Depends(owner)):
        c = runtime.create_session(who)
        return {"id": c.id, "title": c.title, "epoch": c.epoch}

    @app.get("/api/sessions/{sid}")
    def get_session(sid: str, who=Depends(owner)):
        with db.session() as s:
            s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            c = conversation(s, sid, who)
            messages = list(s.scalars(select(Message).where(Message.session_id == sid).order_by(Message.seq)))
            from .player_release import pending_tasks, withhold

            withheld = pending_tasks(s, c)
            tasks = list(
                s.scalars(
                    select(Task)
                    .where(Task.session_id == sid, Task.kind == "respond")
                    .order_by(Task.created_at.desc())
                    .limit(10)
                )
            )
            active_ids = [
                t.id for t in tasks if t.status in {"queued", "running", "waiting"} and t.epoch == c.epoch
            ]
            partial = "".join(
                e.payload.get("text", "")
                for e in s.scalars(
                    select(Event)
                    .where(
                        Event.session_id == sid,
                        Event.task_id.in_(active_ids),
                        Event.kind == "answer_delta",
                        Event.seq <= c.seq,
                    )
                    .order_by(Event.seq)
                )
            )
            return {
                "id": c.id,
                "title": c.title,
                "story_id": c.story_id,
                "epoch": c.epoch,
                "cursor": c.seq,
                "partial": "" if withheld else partial,
                "messages": [
                    {
                        "id": m.id,
                        "role": m.role,
                        "content": "" if m.role == "assistant" and m.task_id in withheld else m.content,
                        "task_id": m.task_id,
                        "redacted": m.redacted,
                        "delivered": m.delivered,
                    }
                    for m in messages
                ],
                "tasks": [
                    {
                        "id": t.id,
                        "status": t.status,
                        "epoch": t.epoch,
                        "error": t.error,
                        "result": withhold(t.result or {}) if t.id in withheld else t.result,
                    }
                    for t in tasks
                ],
            }

    @app.post("/api/sessions/{sid}/messages", status_code=202)
    async def submit(sid: str, body: SubmitMessage, who=Depends(owner)):
        if config.product == "roleplay":
            await runtime.model_readiness.require()
            with db.session() as s:
                if not conversation(s, sid, who).current_state.get("profile"):
                    raise ValueError("请先创建角色并设置核心经历与关系，再开始剧情。")
        else:
            with db.session() as s:
                if has_role_state(conversation(s, sid, who)):
                    raise ValueError("会话与当前产品不匹配。")
        t = runtime.submit(
            sid,
            who,
            body.content.strip(),
            body.client_message_id,
            body.mode,
            channel=body.channel,
            addressees=body.addressees,
            occurred_at=body.occurred_at,
            acknowledgement=body.acknowledgement,
        )
        return {"task_id": t.id, "epoch": t.epoch, "status": t.status}

    @app.get("/api/sessions/{sid}/profile")
    def get_profile(sid: str, who=Depends(role_owner)):
        with db.session() as s:
            c = conversation(s, sid, who)
            return {
                "profile": c.current_state.get("profile"),
                "scene_facts": c.current_state.get("scene_facts", {}),
            }

    @app.put("/api/sessions/{sid}/profile")
    def set_profile(sid: str, body: SessionProfile, who=Depends(role_owner)):
        with db.session.begin() as s:
            c = s.scalar(
                select(Conversation)
                .where(Conversation.id == sid, Conversation.owner_id == who)
                .with_for_update()
            )
            if not c:
                raise KeyError("会话不存在")
            if any(
                c.current_state.get(k) for k in ("character_origin", "player", "scene_facts", "scene_claims")
            ):
                raise ValueError("已有角色版本或剧情状态不能重置，请新建剧情。")
            if s.scalar(select(Message.id).where(Message.session_id == sid).limit(1)):
                raise ValueError("请在新对话开始前设置角色，以免混入其他角色的历史。")
            validate_profile(s, who, body, config)
            if not c.story_id:
                story = Story(id=uid(), owner_id=who, title=body.name + " · 剧情", world=body.world)
                s.add(story)
                s.flush()
                c.story_id = story.id
            elif s.get(Story, c.story_id).world != body.world:
                raise ValueError("已绑定剧情的世界概况不能通过角色设定改写。")
            c.current_state = {"profile": body.model_dump(), "scene_facts": {}}
            c.title = body.name + " · 新剧情"
            c.revision += 1
        return {"ok": True}

    @app.put("/api/sessions/{sid}/scene-facts")
    def scene_fact(sid: str, body: SceneFact, who=Depends(role_owner)):
        # Only this authenticated control path changes canonical scene state.
        # Dialogue, summaries and memory extraction never call it.
        with db.session.begin() as s:
            c = s.scalar(
                select(Conversation)
                .where(Conversation.id == sid, Conversation.owner_id == who)
                .with_for_update()
            )
            if not c:
                raise KeyError("会话不存在")
            if not c.current_state.get("profile"):
                raise ValueError("此对话尚未设置角色。")
            if c.story_id and s.scalar(
                select(Conversation.id)
                .where(
                    Conversation.story_id == c.story_id,
                    Conversation.current_state["player"].astext == "true",
                )
                .limit(1)
            ):
                raise ValueError("玩家剧情请通过剧情事件投递状态，以同步取消过时的NPC回答。")
            previous_event = s.scalar(
                select(Event)
                .where(
                    Event.session_id == sid,
                    Event.kind == "scene_updated",
                    Event.payload["fact"]["event_id"].astext == body.event_id,
                )
                .limit(1)
            )
            if previous_event:
                fact = previous_event.payload["fact"]
                if fact["key"] != body.key or fact["value"] != body.value:
                    raise ValueError("同一事件标识不能对应不同内容。")
                return {"ok": True, "revision": fact["revision"]}
            facts = dict(c.current_state.get("scene_facts", {}))
            if body.key not in facts and len(facts) >= 30:
                raise ValueError("当前剧情状态最多保留30项，请更新已有项目。")
            prior = facts.get(body.key)
            if prior and prior["event_id"] == body.event_id:
                if prior["value"] != body.value:
                    raise ValueError("同一事件标识不能对应不同内容。")
                return {"ok": True, "revision": c.revision}
            c.epoch += 1
            c.revision += 1
            facts[body.key] = {**body.model_dump(), "revision": c.revision}
            if (
                estimate_tokens({"profile": c.current_state["profile"], "scene_facts": facts})
                > config.context_hard_tokens // 2
            ):
                raise ValueError("角色当前状态过长，请精简已有事项后再更新。")
            c.current_state = {**c.current_state, "scene_facts": facts}
            for t in s.scalars(
                select(Task).where(
                    Task.session_id == sid,
                    Task.kind == "respond",
                    Task.status.in_(["queued", "running", "waiting"]),
                )
            ):
                t.status = "superseded"
            runtime.emit(s, c, "scene_updated", {"fact": facts[body.key], "previous": prior})
        return {"ok": True, "revision": c.revision}

    @app.post("/api/tasks/{tid}/cancel")
    def cancel(tid: str, who=Depends(owner)):
        runtime.cancel(tid, who)
        return {"ok": True}

    @app.post("/api/outputs/{tid}/ack")
    def ack(tid: str, who=Depends(owner)):
        with db.session.begin() as s:
            t = s.get(Task, tid)
            if not t or t.owner_id != who:
                raise HTTPException(404)
            for m in s.scalars(select(Message).where(Message.task_id == tid, Message.role == "assistant")):
                m.delivered = True
        return {"ok": True}

    @app.get("/api/sessions/{sid}/events")
    async def events(
        sid: str,
        request: Request,
        cursor: int = 0,
        last_event_id: str | None = Header(default=None),
        who=Depends(owner),
    ):
        with db.session() as s:
            conversation(s, sid, who)
        try:
            after = max(0, int(last_event_id or cursor))
        except ValueError:
            raise HTTPException(400, "无效游标")

        def batch(after):
            with db.session() as s:
                from .player_release import pending_tasks, withhold

                withheld = pending_tasks(s, conversation(s, sid, who))
                return [
                    {
                        "seq": e.seq,
                        "kind": e.kind,
                        "epoch": e.epoch,
                        "task_id": e.task_id,
                        "payload": withhold(e.payload) if e.task_id in withheld else e.payload,
                    }
                    for e in s.scalars(
                        select(Event)
                        .where(Event.session_id == sid, Event.seq > after)
                        .order_by(Event.seq)
                        .limit(200)
                    )
                ]

        async def stream():
            nonlocal after
            idle = 0
            while not await request.is_disconnected():
                rows = await asyncio.to_thread(batch, after)
                for e in rows:
                    after = e["seq"]
                    yield f"id: {after}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                idle += 1
                if idle % 40 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/documents")
    def documents(who=Depends(owner)):
        with db.session() as s:
            rows = s.scalars(
                select(Document)
                .where(Document.owner_id == who, Document.deleted.is_(False))
                .order_by(Document.created_at.desc())
            )
            return [
                {
                    "id": d.id,
                    "name": d.name,
                    "version": d.version,
                    "status": d.status,
                    "error": d.error,
                    "created_at": d.created_at,
                }
                for d in rows
            ]

    @app.post("/api/documents", status_code=202)
    async def upload(file: UploadFile = File(...), replace_id: str | None = None, who=Depends(owner)):
        data = await file.read(15 * 1024 * 1024 + 1)
        d, t = await asyncio.to_thread(
            agents.knowledge.upload, who, file.filename or "file.txt", data, replace_id
        )
        return {"document_id": d.id, "task_id": t.id, "version": d.version}

    @app.delete("/api/documents/{did}")
    def delete_document(did: str, who=Depends(owner)):
        with db.session.begin() as s:
            d = s.scalar(
                select(Document).where(Document.id == did, Document.owner_id == who).with_for_update()
            )
            if not d:
                raise HTTPException(404)
            d.deleted, d.status = True, "deleted"
            s.add(Tombstone(owner_id=who, kind="document", target_id=did))
        return {"ok": True}

    @app.post("/api/documents/{did}/reindex", status_code=202)
    def reindex_document(did: str, who=Depends(owner)):
        with db.session() as s:
            d = s.scalar(
                select(Document).where(
                    Document.id == did, Document.owner_id == who, Document.deleted.is_(False)
                )
            )
            if not d:
                raise HTTPException(404)
        document, task = agents.knowledge.upload(who, d.name, Path(d.blob_path).read_bytes(), d.id)
        return {"document_id": document.id, "task_id": task.id, "version": document.version}

    @app.get("/api/evidence/{eid}")
    def get_evidence(eid: str, who=Depends(owner)):
        with db.session() as s:
            e = s.get(Evidence, eid)
            if not e or e.owner_id != who:
                raise HTTPException(404)
            c = s.get(Chunk, e.chunk_id)
            d = s.get(Document, c.document_id)
            if d.deleted or d.owner_id != who:
                raise HTTPException(404, "来源已删除")
            return {
                "id": e.id,
                "document": d.name,
                "section": c.section,
                "text": c.content,
                "excerpt": e.excerpt,
                "version": e.document_version,
                "current": d.version == e.document_version,
            }

    @app.get("/api/memories")
    def memories(who=Depends(owner)):
        with db.session() as s:
            rows = s.scalars(
                select(Memory)
                .where(Memory.owner_id == who, Memory.status != "deleted")
                .order_by(Memory.created_at.desc())
                .limit(300)
            )
            return [
                {
                    "id": m.id,
                    "predicate": m.predicate,
                    "value": m.value,
                    "scope": m.scope,
                    "status": m.status,
                    "valid_from": m.valid_from,
                    "valid_to": m.valid_to,
                    "supersedes": m.supersedes,
                }
                for m in rows
            ]

    @app.patch("/api/memories/{mid}")
    def edit_memory(mid: str, body: MemoryEdit, who=Depends(owner)):
        m = agents.memory.edit(who, mid, body.value, body.scope)
        return {"id": m.id}

    @app.delete("/api/memories/{mid}")
    def delete_memory(mid: str, who=Depends(owner)):
        agents.memory.forget(who, mid)
        return {"ok": True}

    @app.post("/api/sessions/{sid}/compact", status_code=202)
    def compact(sid: str, who=Depends(owner)):
        t = agents.context.schedule(sid, who, force=True)
        return {
            "task_id": t.id if t else None,
            "message": "已安排后台整理" if t else "当前没有需要整理的完整历史",
        }

    @app.get("/api/debug/overview")
    def debug(who=Depends(owner)):
        with db.session() as s:
            tasks = list(
                s.scalars(select(Task).where(Task.owner_id == who).order_by(Task.created_at.desc()).limit(80))
            )
            calls = list(
                s.scalars(
                    select(Usage).where(Usage.owner_id == who).order_by(Usage.created_at.desc()).limit(100)
                )
            )
            cost = s.scalar(
                select(func.coalesce(func.sum(Usage.cost + Usage.reserved), 0)).where(Usage.owner_id == who)
            )
            return {
                "total_cost_usd": cost,
                "tasks": [
                    {
                        "id": t.id,
                        "kind": t.kind,
                        "status": t.status,
                        "attempt": t.attempt,
                        "calls": t.calls,
                        "error": t.error,
                        "parent_id": t.parent_id,
                        "created_at": t.created_at,
                    }
                    for t in tasks
                ],
                "calls": [
                    {
                        "task_id": u.task_id,
                        "role": u.role,
                        "model": u.model,
                        "input_tokens": u.input_tokens,
                        "output_tokens": u.output_tokens,
                        "cost": u.cost,
                        "duration_ms": u.duration_ms,
                        "status": u.status,
                    }
                    for u in calls
                ],
            }

    from .native_paths import filesystem_path

    web = filesystem_path(
        web_dir if web_dir is not None else Path(__file__).resolve().parents[2] / "apps" / "web" / "dist"
    )
    if serve_web and web.exists():
        app.mount("/assets", StaticFiles(directory=web / "assets"), name="assets")

        @app.get("/{path:path}")
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(web / "index.html")

    return app
