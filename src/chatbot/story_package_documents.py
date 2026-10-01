"""Portable text snapshots, never foreign database references or vector indexes."""

import hashlib
import json

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from .knowledge import tokenize
from .storage import Chunk, Document, uid


class TextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: str = Field(max_length=250)
    content: str = Field(min_length=1, max_length=20000)

    @field_validator("section", "content")
    @classmethod
    def database_text(cls, value):
        if "\x00" in value:
            raise ValueError("文字不能包含空字节")
        return value


class TextDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=250)
    source_version: int = Field(ge=1)
    parts: list[TextPart] = Field(min_length=1, max_length=10000)

    @field_validator("name")
    @classmethod
    def database_text(cls, value):
        if "\x00" in value:
            raise ValueError("名称不能包含空字节")
        return value


def document_references(recipe):
    return {key for p in recipe.people for key in p.profile.document_ids}


def export_documents(s, owner, recipe):
    documents, mapping = {}, {}
    for index, key in enumerate(sorted(document_references(recipe))):
        document = s.scalar(
            select(Document).where(
                Document.id == key,
                Document.owner_id == owner,
                Document.deleted.is_(False),
                Document.status == "ready",
            )
        )
        if not document:
            raise HTTPException(422, "引用资料不存在、未就绪或无权访问，请检查角色资料绑定。")
        parts = list(
            s.scalars(
                select(Chunk)
                .where(
                    Chunk.document_id == key,
                    Chunk.version == document.version,
                )
                .order_by(Chunk.ordinal)
            )
        )
        if not parts:
            raise HTTPException(422, "引用资料没有可导出的文字，请重新建立索引。")
        local_key = f"document-{index + 1}"
        documents[local_key] = TextDocument(
            name=document.name,
            source_version=document.version,
            parts=[TextPart(section=p.section, content=p.content) for p in parts],
        ).model_dump()
        mapping[key] = local_key
    for person in recipe.people:
        person.profile.document_ids = [mapping[key] for key in person.profile.document_ids]
    return documents


def import_documents(s, config, owner, recipe, documents, *, preserve_name=False):
    mapping = {}
    for key, snapshot in documents.items():
        document_id = uid()
        # A readable derived Markdown source supports the existing explicit reindex workflow.
        content = "\n\n".join(f"# {p.section}\n{p.content}" for p in snapshot.parts)
        raw = content.encode("utf-8")
        folder = config.data_dir / "package-documents" / hashlib.sha256(owner.encode()).hexdigest()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (document_id + ".md")
        path.write_bytes(raw)
        name = snapshot.name if preserve_name else snapshot.name[:210] + " · 导入文字快照.md"
        s.add(
            Document(
                id=document_id,
                owner_id=owner,
                name=name,
                version=1,
                content_hash=hashlib.sha256(raw).hexdigest(),
                blob_path=str(path),
                status="ready",
            )
        )
        s.flush()
        for ordinal, part in enumerate(snapshot.parts):
            terms = " ".join(tokenize(part.content))
            s.add(
                Chunk(
                    document_id=document_id,
                    version=1,
                    ordinal=ordinal,
                    section=part.section,
                    content=part.content,
                    terms=terms,
                    search_vector=func.to_tsvector("simple", terms),
                )
            )
        mapping[key] = document_id
        # Source version is an import provenance note, not the local revision number.
        (folder / (document_id + ".json")).write_text(
            json.dumps({"name": snapshot.name, "source_version": snapshot.source_version}), encoding="utf-8"
        )
    for person in recipe.people:
        person.profile.document_ids = [mapping[key] for key in person.profile.document_ids]
