"""Single text representation, deterministically projected to the existing review contract."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..schemas import ActorIntent, ActorSpeech, GroundedClaim
from .speech_contract import SPEECH_OUTPUT_CONTRACT


class SpeechPacket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["speech", "action"] = "speech"
    text: str = Field(min_length=1, max_length=2400)
    expression: Literal["neutral", "happy", "sad", "angry", "surprised", "concerned"] = "neutral"
    pose: Literal["neutral", "arms_crossed", "thinking", "nodding"] = "neutral"
    voice_direction: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def action_has_no_voice(self):
        if self.kind == "action" and (self.expression != "neutral" or self.pose != "neutral" or self.voice_direction):
            raise ValueError("动作片段不携带台词表现字段。")
        if self.kind == "action" and any(c in self.text for c in "（）()[]<>"):
            raise ValueError("动作正文不重复输出分隔括号。")
        return self


class PacketSpeech(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segments: list[SpeechPacket] = Field(min_length=1, max_length=12)
    claims: list[GroundedClaim] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def total_size(self):
        if sum(len(p.text) + (2 if p.kind == "action" else 0) for p in self.segments) > 2400:
            raise ValueError("完整正文超出台词上限。")
        return self


class PacketTurnDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: ActorIntent
    speech: PacketSpeech | None = None


def to_actor_speech(value):
    packet = PacketSpeech.model_validate(value)
    spoken = [p for p in packet.segments if p.kind == "speech"]
    return ActorSpeech(
        text="".join(p.text if p.kind == "speech" else "（" + p.text + "）" for p in packet.segments),
        spoken_parts=[p.text for p in spoken],
        claims=packet.claims,
        performance=[{k: v for k, v in p.model_dump().items() if k != "kind"} for p in spoken],
    ).model_dump()


def output_contract(segments=False):
    if not segments:
        return SPEECH_OUTPUT_CONTRACT
    # Keep all fact/knowledge rules literally. Only the representation instructions change.
    common = SPEECH_OUTPUT_CONTRACT.split("spoken_parts按顺序", 1)[0]
    common = common.replace("输出ActorSpeech JSON。text是全部台词和动作；",
                            "输出PacketSpeech JSON。完整text由运行时按segments顺序拼接；")
    return common + (
        "segments是有序片段。kind=speech为实际台词，kind=action为括号内动作正文（不写括号）；"
        "运行时为action加全角括号后与台词直接拼接，不插入空格。台词/动作正文只输出一次。"
        "每个speech片段携带expression、pose及简短voice_direction；动作片段这些字段保持中性和空语气。"
        "voice_direction仅写音量、语速、情绪等简短语气描述。不得借表现字段加入秘密或执行世界动作。"
        "台词正文不混入动作、旁白、姓名标题或语音控制标签；纯动作可没有speech片段。"
        "action只表示表演描述，不授权物品操作，也不证明世界动作已成功；仍遵守完整事实与世界动作审核。"
        "claims.text仍须逐字来自拼接后的完整正文。片段不是独立获准事件，全部输出后仍接受原完整审核。"
    )


def uses_packets(services):
    return getattr(services.config, "role_speech_representation", "legacy") == "segments"
