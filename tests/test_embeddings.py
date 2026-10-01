from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from chatbot.config import Settings
from chatbot.providers import Embeddings, ProviderError


def test_embedding_key_file_does_not_require_copying_secret_into_env(tmp_path):
    path = tmp_path / "embedding.txt"
    path.write_text("sk-" + "a" * 32 + "\n", encoding="utf-8-sig")
    config = Settings(_env_file=None, embedding_api_key_file=str(path))
    assert config.embedding_key() == "sk-" + "a" * 32
    path.unlink()
    assert config.embedding_key() == ""


def test_embedding_contract_reorders_indices_and_normalizes_usage():
    vectors, usage = Embeddings.parse_response(
        {
            "data": [{"index": 1, "embedding": [2.0] * 1024}, {"index": 0, "embedding": [1.0] * 1024}],
            "usage": {"total_tokens": 19},
        },
        2,
    )
    assert vectors[0][0] == 1.0 and vectors[1][0] == 2.0
    assert usage == {"prompt_tokens": 19, "completion_tokens": 0}


@pytest.mark.parametrize(
    "data",
    [
        [{"index": 0, "embedding": [1.0] * 1024}, {"index": 0, "embedding": [1.0] * 1024}],
        [{"index": 0, "embedding": [float("nan")] * 1024}],
        [{"index": 0, "embedding": [0.0] * 1024}],
        [{"index": 0, "embedding": [1.0] * 256}],
    ],
)
def test_embedding_contract_rejects_unusable_vectors(data):
    with pytest.raises(ProviderError):
        Embeddings.parse_response({"data": data}, len(data))


@pytest.mark.parametrize("status,attempts", [(429, 2), (401, 1), (503, 2)])
async def test_embedding_retry_is_bounded_and_each_attempt_accounted(status, attempts):
    config = Settings(_env_file=None, embedding_provider="api", embedding_api_key="test")
    provider = SimpleNamespace(runtime=Mock(), reserve=Mock(side_effect=["u1", "u2"]), settle=Mock())
    provider.runtime.check.return_value = SimpleNamespace(lease_version=1)
    embedding = Embeddings(config, provider)
    request = httpx.Request("POST", "https://example.invalid/embeddings")
    error = httpx.HTTPStatusError("test", request=request, response=httpx.Response(status, request=request))
    embedding._request = AsyncMock(side_effect=[error, ([[1.0] * 1024], {"prompt_tokens": 1})])
    if attempts == 1:
        with pytest.raises(ProviderError):
            await embedding.encode(["test"], "task")
    else:
        assert await embedding.encode(["test"], "task") == [[1.0] * 1024]
    assert provider.reserve.call_count == provider.settle.call_count == attempts
