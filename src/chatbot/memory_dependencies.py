"""Evidence anchors are not a complete account of a model-derived note's inputs."""

from sqlalchemy import text

from .storage import RoleMemory


def memory_visible(memory, visible):
    dependencies = memory.context_source_ids
    return (dependencies is not None and set(memory.source_ids).issubset(visible)
            and set(dependencies).issubset(visible))


def visible_memory_clause(visible):
    ids = list(visible)
    return (RoleMemory.source_ids.contained_by(ids)
            & RoleMemory.context_source_ids.is_not(None)
            & RoleMemory.context_source_ids.contained_by(ids))


def memory_dependencies(memory):
    """Use only after memory_visible; NULL must never mean no dependencies."""
    if memory.context_source_ids is None:
        raise ValueError("旧推导记忆缺少上下文依赖，需要从有效原记录重新整理。")
    return set(memory.source_ids) | set(memory.context_source_ids)


def migrate_dependencies(connection):
    """Idempotent native-host upgrade. Only deterministic legacy rows are inferred.

    Unknown model-derived notes retain SQL NULL and are excluded from readers;
    their content/status is preserved, rather than fabricating derivation data.
    """
    connection.execute(text("ALTER TABLE role_memories ADD COLUMN IF NOT EXISTS context_source_ids JSONB"))
    connection.execute(text("""
        UPDATE role_memories AS m SET context_source_ids = m.source_ids
        WHERE m.context_source_ids IS NULL AND jsonb_array_length(m.source_ids) > 0
          AND (m.kind = 'episode' OR (
            jsonb_array_length(m.source_ids) = 1 AND EXISTS (
              SELECT 1 FROM scene_records AS r
              WHERE r.id = m.source_ids->>0 AND r.story_id = m.story_id
                AND NOT r.redacted AND r.text = m.content
                AND ((r.kind = 'authoring' AND r.payload->>'kind' = m.kind)
                     OR (r.kind = 'relationship' AND m.kind = 'relationship'))
            )
          ))
    """))
