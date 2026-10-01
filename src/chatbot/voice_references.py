"""Project-local WAV references and explicit, account-scoped Aliyun enrollment."""

import asyncio
import base64
import hashlib
import io
import json
import time
import wave
from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl

from .project_workspace import project_root
from .storage import Task, now, uid
from .tts_adapters import QwenAudioTTS, binding_scope, selected_model


class Reference(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    character: str = Field(min_length=1, max_length=100)
    transcript: str = Field(min_length=1, max_length=2000)
    wav_base64: str = Field(max_length=4_000_000)
    rights_confirmed: bool = False
    redistribution_allowed: bool = False


class Enrollment(BaseModel):
    audio_url: HttpUrl
    consent_aliyun: bool = False
    same_audio_confirmed: bool = False


def validate_wav(encoded):
    try:
        raw = base64.b64decode(encoded, validate=True)
        with wave.open(io.BytesIO(raw), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or not 16000 <= audio.getframerate() <= 48000
            ):
                raise ValueError()
            if not 3 <= duration <= 30:
                raise ValueError()
            if len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2:
                raise ValueError()
        return raw, duration
    except Exception:
        raise HTTPException(422, "请提供 3–30 秒、16–48kHz、单声道 PCM16 WAV") from None


def write_json(path, value):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def restore_catalog(config):
    """Rebuild only this account/model's registered catalog after service restart."""
    scope = binding_scope(config)
    for path in project_root(config, config.workspace_owner).glob("*/voice-references/*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            registration = value.get("enrollment", {})
            if registration.get("scope") == scope and registration.get("status") == "completed":
                voice = registration["voice_id"]
                if isinstance(voice, str) and voice.startswith(selected_model(config) + "-"):
                    config.tts_voices[voice] = str(value["name"])[:100]
        except (OSError, ValueError, KeyError, TypeError):
            continue


def reference_router(service, owner):
    config, db = service.config, service.db
    router = APIRouter(prefix="/api/role/projects")
    lock = asyncio.Lock()

    def folder(pid, identity):
        if config.product != "roleplay":
            raise HTTPException(404)
        try:
            root = project_root(config, identity) / str(UUID(pid))
        except ValueError:
            raise HTTPException(422, "工程标识无效") from None
        if not (root / "sceneweaver.project.json").is_file():
            raise HTTPException(404, "请先保存为本机工程")
        directory = root / "voice-references"
        directory.mkdir(exist_ok=True)
        return directory

    @router.get("/{pid}/voice-references")
    def listing(pid: str, identity=Depends(owner)):
        result = []
        for path in folder(pid, identity).glob("*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            state = value.get("enrollment", {})
            result.append(
                {
                    **{k: v for k, v in value.items() if k != "enrollment"},
                    "enrollment_status": state.get("status", "not_registered"),
                    "voice_id": state.get("voice_id")
                    if state.get("scope") == binding_scope(config)
                    else None,
                }
            )
        return result

    @router.post("/{pid}/voice-references", status_code=201)
    def upload(pid: str, body: Reference, identity=Depends(owner)):
        if not body.rights_confirmed:
            raise HTTPException(422, "请确认有权使用该录音制作角色音色")
        directory = folder(pid, identity)
        if len(list(directory.glob("*.json"))) >= 64:
            raise HTTPException(409, "单个工程最多 64 个参考音频")
        raw, duration = validate_wav(body.wav_base64)
        rid = str(uuid4())
        value = body.model_dump(exclude={"wav_base64"})
        value.update(id=rid, sha256=hashlib.sha256(raw).hexdigest(), duration_seconds=duration)
        (directory / f"{rid}.wav").write_bytes(raw)
        write_json(directory / f"{rid}.json", value)
        return value

    @router.post("/{pid}/voice-references/{rid}/enroll")
    async def enroll(pid: str, rid: str, body: Enrollment, identity=Depends(owner)):
        if not body.consent_aliyun or not body.same_audio_confirmed:
            raise HTTPException(422, "须确认 URL 对应同一份授权录音，并同意发送给阿里云注册")
        if body.audio_url.scheme != "https" or body.audio_url.username or body.audio_url.password:
            raise HTTPException(422, "录音地址须为无账号密码的 HTTPS URL")
        if config.tts_provider != "qwen_audio" or not service.available():
            raise HTTPException(409, "请先配置阿里云 Qwen-Audio-TTS")
        try:
            key = str(UUID(rid))
        except ValueError:
            raise HTTPException(422, "参考音频标识无效") from None
        path = folder(pid, identity) / f"{key}.json"
        if not path.is_file():
            raise HTTPException(404)
        async with lock:
            value = json.loads(path.read_text(encoding="utf-8"))
            scope = binding_scope(config)
            previous = value.get("enrollment", {})
            if previous.get("scope") == scope:
                if previous.get("status") == "completed":
                    return {"voice_id": previous["voice_id"], "reused": True}
                if previous.get("status") in {"pending", "unknown"}:
                    raise HTTPException(409, "前次注册结果待核实，请在供应商控制台确认，避免重复收费")
            task_id, usage, started = uid(), None, time.monotonic()
            with db.session.begin() as s:
                s.add(
                    Task(
                        id=task_id,
                        owner_id=identity,
                        kind="voice_enrollment",
                        status="running",
                        deadline=now() + timedelta(seconds=60),
                    )
                )
            status = "failed"
            try:
                usage = service.billing.reserve(task_id, "voice_enrollment", selected_model(config), "", 0)
                value["enrollment"] = {"scope": scope, "status": "pending"}
                write_json(path, value)
                adapter = QwenAudioTTS(
                    config.tts_adapter_api_key.get_secret_value(),
                    selected_model(config),
                    config.tts_workspace,
                    config.tts_region,
                    endpoint=config.tts_endpoint,
                )
                host = adapter.url.split("/")[2]
                response = await service.http.post(
                    f"https://{host}/api/v1/services/audio/tts/customization",
                    headers={"Authorization": "Bearer " + config.tts_adapter_api_key.get_secret_value()},
                    json={
                        "model": "voice-enrollment",
                        "input": {
                            "action": "create_voice",
                            "target_model": selected_model(config),
                            "prefix": "sw" + key.replace("-", "")[:8],
                            "url": str(body.audio_url),
                            "language_hints": ["zh"],
                        },
                    },
                )
                response.raise_for_status()
                voice_id = response.json()["output"]["voice_id"]
                if not isinstance(voice_id, str) or not voice_id.startswith(selected_model(config) + "-"):
                    raise ValueError("Invalid voice response")
                value["enrollment"] = {"scope": scope, "status": "completed", "voice_id": voice_id}
                write_json(path, value)
                status = "completed"
                config.tts_voices = {**config.tts_voices, voice_id: value["name"]}
                return {"voice_id": voice_id, "reused": False}
            except Exception:
                if usage:
                    value["enrollment"] = {"scope": scope, "status": "unknown"}
                    write_json(path, value)
                raise HTTPException(
                    409, "注册未确认成功；请检查服务和预算，核实供应商音色列表后再处理"
                ) from None
            finally:
                if usage:
                    service.billing.settle(usage, None, time.monotonic() - started, status)
                with db.session.begin() as s:
                    s.get(Task, task_id).status = status

    return router
