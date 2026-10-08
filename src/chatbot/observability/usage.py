"""Cache-report provenance without retaining provider bodies or changing billing."""

from sqlalchemy import select

from ..storage import Operation


def cache_tokens_reported(usage):
    if not isinstance(usage, dict):
        return False
    if "prompt_cache_hit_tokens" in usage:
        value = usage["prompt_cache_hit_tokens"]
    else:
        details = usage.get("prompt_tokens_details")
        value = details.get("cached_tokens") if isinstance(details, dict) else None
    return type(value) is int and value >= 0


def cache_reporting(session, rows):
    """None means legacy/unknown; False means a new response omitted valid cache usage."""
    expected = {f"usage-cache:{row.id}": row for row in rows}
    result = {row.id: None for row in rows}
    if not expected:
        return result
    for operation in session.scalars(select(Operation).where(Operation.id.in_(expected))):
        row = expected[operation.id]
        value = operation.payload.get("cache_tokens_reported")
        if operation.task_id == row.task_id and type(value) is bool:
            result[row.id] = value
    return result
