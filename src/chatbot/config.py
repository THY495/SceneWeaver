import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHAT_", env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://sceneweaver:sceneweaver_local@127.0.0.1:55432/sceneweaver"
    product: Literal["roleplay"] = "roleplay"
    model_provider: str = "deepseek"
    deepseek_review_format: Literal["json_object", "responses_json_schema"] = "json_object"
    api_base: str = "https://api.deepseek.com"
    api_key: SecretStr = SecretStr("")
    api_key_file: str = ""
    dialogue_model: str = "deepseek-v4-flash"
    expert_model: str = "deepseek-v4-pro"
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_api_key_file: str = ""
    openrouter_roles: dict[str, str] = Field(default_factory=dict)
    openrouter_providers: dict[str, list[str]] = Field(default_factory=dict)
    openrouter_prices: dict[str, dict[str, float]] = Field(default_factory=dict)
    openrouter_reasoning: dict[str, Literal["none", "minimal", "low", "medium", "high"]] = Field(
        default_factory=dict
    )
    openrouter_zdr: bool = True
    auto_complex: bool = False
    embedding_provider: Literal["lexical", "api"] = "lexical"
    embedding_api_base: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    embedding_api_key: SecretStr = SecretStr("")
    embedding_api_key_file: str = ""
    embedding_trust_env: bool = True
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = Field(1024, ge=1024, le=1024)
    embedding_batch_size: int = Field(3, ge=1, le=10)
    embedding_input_usd_per_million: float = Field(0.2, ge=0)
    rerank_api_url: str = ""
    rerank_api_key: SecretStr = SecretStr("")
    rerank_model: str = "qwen3-rerank"
    rerank_request_usd: float = Field(0.002, ge=0)
    data_dir: Path = Path("data")
    windows_game_runtime_dir: Path | None = None
    bind_host: str = "127.0.0.1"
    port: int = 8766
    access_token: SecretStr = SecretStr("")
    daily_budget_usd: float = Field(2, gt=0)
    turn_budget_usd: float = Field(0.2, gt=0)
    max_calls_per_task: int = Field(48, ge=1, le=100)
    context_soft_tokens: int = Field(16000, ge=200)
    context_hard_tokens: int = Field(24000, ge=400)
    context_recent_messages: int = Field(12, ge=2)
    worker_concurrency: int = Field(4, ge=2, le=16)
    lease_seconds: int = Field(60, ge=5)
    request_timeout: int = Field(90, ge=1)
    task_timeout: int = Field(240, ge=5)
    allow_live_tests: bool = False
    tts_enabled: bool = False
    tts_provider: Literal["qwen_legacy", "qwen_audio", "fish"] = "qwen_legacy"
    tts_adapter_model: str = ""
    tts_workspace: str = ""
    tts_endpoint: Literal["workspace", "shared"] = "workspace"
    voice_enrollment_reserve_usd: float = Field(default=0.05, gt=0, le=1)
    tts_region: Literal["cn-beijing", "ap-southeast-1"] = "cn-beijing"
    tts_voices: dict[str, str] = Field(default_factory=dict)
    tts_adapter_api_key: SecretStr = SecretStr("")
    tts_usd_per_million_bytes: float = Field(15, gt=0)
    acknowledgement_enabled: bool = True
    acknowledgement_timeout: float = Field(3, ge=0.2, le=10)
    tts_api_key: SecretStr = SecretStr("")
    tts_api_key_file: str = ""
    tts_api_url: str = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    tts_model: str = "qwen3-tts-flash"
    tts_transport: Literal["http", "realtime"] = "http"
    tts_realtime_model: str = "qwen3-tts-flash-realtime"
    tts_usd_per_million_chars: float = Field(20, gt=0)
    asr_enabled: bool = False
    asr_model: str = "qwen3-asr-flash"
    asr_usd_per_minute: float = Field(0.02, gt=0)

    def tts_key(self) -> str:
        if self.tts_api_key.get_secret_value():
            return self.tts_api_key.get_secret_value()
        if self.tts_api_key_file:
            value = Path(self.tts_api_key_file).read_text("utf-8-sig").strip()
            return value if re.fullmatch(r"sk-[A-Za-z0-9_-]{16,}", value) else ""
        return ""

    def generation_key(self) -> str:
        if self.api_key.get_secret_value():
            return self.api_key.get_secret_value()
        if self.api_key_file:
            values = re.findall(r"sk-[A-Za-z0-9_-]{16,}", Path(self.api_key_file).read_text("utf-8-sig"))
            if values:
                return values[0]
        return ""

    def openrouter_key(self) -> str:
        if self.openrouter_api_key.get_secret_value():
            return self.openrouter_api_key.get_secret_value()
        if self.openrouter_api_key_file:
            value = Path(self.openrouter_api_key_file).read_text("utf-8-sig").strip()
            if re.fullmatch(r"sk-or-[A-Za-z0-9_-]{16,}", value):
                return value
        return ""

    def embedding_key(self) -> str:
        if self.embedding_api_key.get_secret_value():
            return self.embedding_api_key.get_secret_value()
        if self.embedding_api_key_file:
            try:
                value = Path(self.embedding_api_key_file).read_text("utf-8-sig").strip()
            except OSError:
                return ""
            if re.fullmatch(r"sk-[A-Za-z0-9_-]{16,}", value):
                return value
        return ""

    @property
    def workspace_owner(self) -> str:
        return "local-roleplay"

    @property
    def pg_dsn(self) -> str:
        return self.database_url.replace("postgresql+psycopg://", "postgresql://")


@lru_cache
def settings() -> Settings:
    return Settings()
