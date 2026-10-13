import asyncio
import logging
import time
from contextlib import AsyncExitStack
from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph

from .agents import Agents
from .config import Settings
from .observability.timing import TimingTrace, timed
from .providers import BudgetExceeded, Provider, ProviderError
from .runtime import Runtime, StaleTask
from .storage import Database, Task, uid

log = logging.getLogger("continuum.worker")


class GraphState(TypedDict):
    task_id: str
    phase: str


class Worker:
    def __init__(self, db: Database, config: Settings, provider=None, *, execution_scope=None):
        self.db, self.config = db, config
        self.runtime = Runtime(db, config, execution_scope=execution_scope)
        self.provider = provider or Provider(db, config, self.runtime)
        self.agents = Agents(db, config, self.runtime, self.provider)
        self.id = uid()
        self.stop = asyncio.Event()
        self.graph = None
        self.next_index_at = 0
        self.next_scene_at = 0
        self.next_backstage_at = 0

    async def setup_graph(self, stack):
        saver = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(self.config.pg_dsn))
        await saver.setup()

        async def execute(state, config: RunnableConfig):
            # The invocation owns a specific lease; a checkpoint must not lend
            # a delayed old execution the lease held by its replacement worker.
            task = self.runtime.check(state["task_id"], config["configurable"]["lease_version"])
            result = await self.agents.execute(task)
            self.runtime.finish(task.id, task.lease_version, result)
            return {"task_id": task.id, "phase": "done"}

        builder = StateGraph(GraphState)
        builder.add_node("execute", execute)
        builder.add_edge(START, "execute")
        builder.add_edge("execute", END)
        self.graph = builder.compile(checkpointer=saver)

    async def run_task(self, task):
        if self.runtime.execution_scope:
            self.runtime.check(task.id, task.lease_version)
        timing = TimingTrace(task)
        try:
            with timing.activate():
                await self._run_task(task)
        finally:
            await asyncio.to_thread(timing.save, self.db)

    @timed("worker.execute")
    async def _run_task(self, task):
        async def execute():
            if self.graph:
                # Checkpoint contains identifiers only; canonical business effects are idempotent.
                cfg = {"configurable": {"thread_id": task.id, "lease_version": task.lease_version}}
                state = await self.graph.aget_state(cfg)
                await self.graph.ainvoke(None if state.next else {"task_id": task.id, "phase": "start"}, cfg)
            else:
                result = await self.agents.execute(task)
                self.runtime.finish(task.id, task.lease_version, result)

        work = asyncio.create_task(execute())
        ticks = 0
        try:
            while not work.done():
                await asyncio.wait({work}, timeout=0.4)
                if work.done():
                    break
                with self.db.session() as s:
                    current = s.get(Task, task.id)
                    if current.status == "completed":
                        break  # Allow identifier-only checkpoint commit to finish.
                ticks += 1
                self.runtime.check(task.id, task.lease_version)
                if ticks % max(1, self.config.lease_seconds // 2) == 0:
                    if not self.runtime.heartbeat(task.id, task.lease_version):
                        raise StaleTask("租约失效")
            await work
            if task.kind == "respond":
                from sqlalchemy import func, select

                from .observations import members
                from .role_memory import RoleMemoryService
                from .storage import Conversation, Observation, RoleSnapshot, SceneRecord

                with self.db.session() as s:
                    current = s.get(Conversation, task.session_id)
                    player = current.current_state.get("player")
                    actors = members(s, current.story_id) if player else []
                    backlog = {}
                    for actor in actors:
                        cut = (
                            s.scalar(
                                select(RoleSnapshot.covered_seq)
                                .where(
                                    RoleSnapshot.actor_id == actor.id,
                                    RoleSnapshot.privacy_version == actor.privacy_version,
                                )
                                .order_by(RoleSnapshot.covered_seq.desc())
                                .limit(1)
                            )
                            or 0
                        )
                        backlog[actor.id] = s.scalar(
                            select(func.count())
                            .select_from(SceneRecord)
                            .join(Observation, Observation.event_id == SceneRecord.id)
                            .where(
                                Observation.actor_id == actor.id,
                                SceneRecord.seq > cut,
                                SceneRecord.redacted.is_(False),
                            )
                        )
                if player:
                    for actor in sorted(actors, key=lambda c: backlog[c.id], reverse=True):
                        if not actor.current_state.get("player"):
                            pending = RoleMemoryService(
                                self.db, self.config, self.runtime, self.provider
                            ).schedule(actor.id, task.owner_id, parent_id=task.id)
                            if pending:
                                break
                else:
                    self.agents.context.schedule(task.session_id, task.owner_id)
                    if current.story_id and current.current_state.get("profile"):
                        RoleMemoryService(self.db, self.config, self.runtime, self.provider).schedule(
                            current.id, task.owner_id, parent_id=task.id
                        )
            if task.kind == "scene_run":
                from .role_memory import RoleMemoryService

                service = RoleMemoryService(self.db, self.config, self.runtime, self.provider)
                for actor_id in task.payload["audience"]:
                    if service.schedule(actor_id, task.owner_id, parent_id=task.id):
                        break
        except (StaleTask, asyncio.CancelledError) as exc:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            from .action_attempts import pause_task

            pause_task(self.db, task, "interrupted")
            if task.kind in {"scene_run", "npc_step"}:
                self.runtime.finish(task.id, task.lease_version, error="场景活动或实验许可已停止或变化。")
            if isinstance(exc, asyncio.CancelledError):
                # Preserve recoverable task state, but honour caller/shutdown cancellation.
                raise
        except Exception as exc:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            from .action_attempts import pause_task

            pause_task(
                self.db,
                task,
                "budget"
                if isinstance(exc, BudgetExceeded)
                else "provider_error"
                if isinstance(exc, ProviderError)
                else "validation_error"
                if isinstance(exc, ValueError)
                else "technical_error",
            )
            safe = (
                str(exc)
                if isinstance(exc, ProviderError | ValueError)
                else "任务执行失败，请查看本地诊断或重试。"
            )
            log.error("task_failed task=%s kind=%s error_type=%s", task.id, task.kind, type(exc).__name__)
            from .scene_recovery import park_scene

            if park_scene(self.db, task, exc):
                return
            self.runtime.finish(task.id, task.lease_version, error=safe)

    async def lane(self, index, background):
        while not self.stop.is_set():
            try:
                task = self.runtime.claim(f"{self.id}:{index}", background=background)
                if task:
                    await self.run_task(task)
                else:
                    if (
                        background
                        and index == self.config.worker_concurrency - 1
                        and time.monotonic() >= self.next_index_at
                    ):
                        from .role_index import schedule_index

                        self.next_index_at = time.monotonic() + 30
                        schedule_index(self.db, self.config, self.runtime)
                    await asyncio.sleep(0.25)
                if index == 0:
                    self.runtime.expire()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("worker_loop error_type=%s", type(exc).__name__)
                await asyncio.sleep(1)

    async def notification_loop(self):
        from .agreement_delivery import deliver_notices

        while not self.stop.is_set():
            try:
                await asyncio.to_thread(deliver_notices, self.db, self.runtime, 16)
                if time.monotonic() >= self.next_scene_at:
                    from .scene_tasks import schedule_scenes

                    self.next_scene_at = time.monotonic() + 5
                    await asyncio.to_thread(
                        schedule_scenes, self.db, self.runtime, self.config.workspace_owner
                    )
                if self.runtime.execution_scope and time.monotonic() >= self.next_backstage_at:
                    from .game_backstage import schedule_backstage

                    self.next_backstage_at = time.monotonic() + 1
                    await asyncio.to_thread(schedule_backstage, self.db, self.runtime)
            except Exception as exc:
                log.error("agreement_delivery error_type=%s", type(exc).__name__)
            await asyncio.sleep(0.5)

    async def run(self):
        async with AsyncExitStack() as stack:
            await self.setup_graph(stack)
            lanes = [
                asyncio.create_task(self.lane(i, i >= max(1, self.config.worker_concurrency // 2)))
                for i in range(self.config.worker_concurrency)
            ]
            lanes.append(asyncio.create_task(self.notification_loop()))
            try:
                await asyncio.gather(*lanes)
            finally:
                for lane in lanes:
                    lane.cancel()
                await asyncio.gather(*lanes, return_exceptions=True)
                await self.provider.close()
