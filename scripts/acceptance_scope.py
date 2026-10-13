"""Bound acceptance tools to a project or its verified independent-game save."""

import math
from datetime import UTC, datetime

from sqlalchemy import func, select, text

from chatbot.config import Settings
from chatbot.experiment_initials import compatible_recipe_digests, digest
from chatbot.observability.usage import cache_reporting
from chatbot.storage import Database, ExperimentInitial, GameSave, Task, Usage, now, uid


def reserve_acceptance_budget(db, config, cap):
    """Isolated test databases must share the project's daily envelope."""
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError("Acceptance budget must be positive and finite")
    with db.session.begin() as s:
        # Same lock as existing real-model architecture probes.
        s.execute(text("SELECT pg_advisory_xact_lock(731291)"))
        day = now().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        spent = s.scalar(
            select(func.coalesce(func.sum(Usage.cost + Usage.reserved), 0)).where(Usage.created_at >= day)
        )
        if spent + cap > config.daily_budget_usd:
            raise RuntimeError("Insufficient daily budget for the bounded acceptance")
        row = Usage(
            id=uid(), owner_id=config.workspace_owner, task_id=uid(),
            role="acceptance_batch", model=config.dialogue_model, reserved=cap,
        )
        s.add(row)
        return row.id


def settle_acceptance_budget(db, usage_id, conservative_cost):
    """Settle once; uncertain in-flight reservations count as spent, never free."""
    if not math.isfinite(conservative_cost) or conservative_cost < 0:
        raise ValueError("Acceptance cost must be nonnegative and finite")
    with db.session.begin() as s:
        row = s.get(Usage, usage_id, with_for_update=True)
        if row is None or row.role != "acceptance_batch":
            raise ValueError("Missing acceptance budget reservation")
        if row.status == "completed":
            if not math.isclose(row.cost, conservative_cost, abs_tol=1e-9):
                raise ValueError("A completed acceptance charge cannot be overwritten")
            return
        row.cost, row.reserved, row.status = conservative_cost, 0, "completed"


def reopened_content_preserved(before, after):
    """Compare captured state; allow only a monotonically rewritten bookmark revision."""
    if not isinstance(before, dict) or not isinstance(after, dict) or before.keys() != after.keys():
        return False
    left, right = before.get("reading"), after.get("reading")
    if not isinstance(left, dict) or not isinstance(right, dict) or left.keys() != right.keys():
        return False
    old, new = left.get("revision"), right.get("revision")
    if type(old) is not int or type(new) is not int or old < 0 or new < old:
        return False
    return (
        {k: v for k, v in before.items() if k != "reading"}
        == {k: v for k, v in after.items() if k != "reading"}
        and {k: v for k, v in left.items() if k != "revision"}
        == {k: v for k, v in right.items() if k != "revision"}
    )


def belongs_to_release(s, player, release, owner):
    """Bind a paid probe to the verified package, its immutable seed and saved recipe."""
    if not player or player.owner_id != owner or not player.current_state.get("player"):
        return False
    origin = s.get(ExperimentInitial, player.story_id)
    save = s.get(GameSave, player.id)
    source_id = origin.receipt.get("source_story_id") if origin else None
    source = s.get(ExperimentInitial, source_id) if source_id else None
    return bool(
        save
        and source
        and origin
        and save.owner_id == origin.owner_id == source.owner_id == owner
        and save.story_id == player.story_id
        and source.receipt.get("release_seed") is True
        and source.request_hash == source.receipt.get("package_hash") == release["package_sha256"]
        and save.release_id == digest(source.recipe)
        and digest(origin.recipe) in compatible_recipe_digests(source.recipe)
    )


def belongs_to_project(s, player, project_id):
    if not player or not project_id or not player.current_state.get("player"):
        return False
    origin = s.get(ExperimentInitial, player.story_id)
    if not origin or origin.owner_id != player.owner_id:
        return False
    if origin.receipt.get("blueprint_id") == project_id:
        return True
    save = s.get(GameSave, player.id)
    source_id = origin.receipt.get("source_story_id")
    source = s.get(ExperimentInitial, source_id) if source_id else None
    return bool(
        save
        and source
        and save.owner_id == player.owner_id == source.owner_id
        and save.story_id == player.story_id
        and source.receipt.get("blueprint_id") == project_id
        and save.release_id == digest(source.recipe)
        and digest(origin.recipe) in compatible_recipe_digests(source.recipe)
    )


EXECUTION_FIELDS = (
    "role_actor_generation",
    "role_preparation_concurrency",
    "role_director_beat_reuse",
    "role_direct_response",
    "role_speech_representation",
    "role_player_action_preflight",
    "role_response_review_binding",
    "role_assignment_authorization",
)


def execution_identity(config: Settings, *, require_defaults: bool = False) -> dict:
    """Record only public execution settings; never serialize credentials.

    Field defaults, unlike another Settings instance, cannot be overridden by
    this machine's environment. Controlled retrieval and budgets are separate
    from the execution-architecture claim.
    """
    modes = {name: getattr(config, name) for name in EXECUTION_FIELDS}
    defaults = {name: Settings.model_fields[name].default for name in EXECUTION_FIELDS}
    differences = [name for name in EXECUTION_FIELDS if modes[name] != defaults[name]]
    if require_defaults and differences:
        raise ValueError("Execution modes differ from code defaults: " + ", ".join(differences))
    return {
        "execution_modes": modes,
        "code_execution_defaults": defaults,
        "execution_matches_code_defaults": not differences,
        "controlled_environment": {
            "product": config.product,
            "model_provider": config.model_provider,
            "dialogue_model": config.dialogue_model,
            "embedding_provider": config.embedding_provider,
            "openrouter_role_overrides_present": bool(config.openrouter_roles),
            "tts_enabled": config.tts_enabled,
            "database_budget_usd": config.daily_budget_usd,
            "turn_budget_usd": config.turn_budget_usd,
            "max_calls_per_task": config.max_calls_per_task,
        },
        "scope_exclusions": [
            "native Windows executable",
            "external embedding retrieval when lexical is selected",
            "human listening quality",
        ],
    }



def ledger(db: Database, cap: float):
    with db.session() as s:
        rows = list(s.scalars(select(Usage).order_by(Usage.created_at)))
        tasks = list(s.scalars(select(Task).order_by(Task.created_at)))
        cache_reports = cache_reporting(s, rows)
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "limit_usd": cap,
        "cost_usd": round(sum(row.cost for row in rows), 8),
        "reserved_usd": round(sum(row.reserved for row in rows), 8),
        "usage": [
            {
                "task_id": row.task_id,
                "role": row.role,
                "model": row.model,
                "status": row.status,
                "cost_usd": round(row.cost, 8),
                "reserved_usd": round(row.reserved, 8),
                "duration_ms": row.duration_ms,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "cache_tokens": row.cache_tokens,
                "cache_tokens_reported": cache_reports[row.id],
            }
            for row in rows
        ],
        "tasks": [
            {"id": task.id, "kind": task.kind, "status": task.status, "calls": task.calls} for task in tasks
        ],
    }

