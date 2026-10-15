"""OpenAIEmbedder against an httpx2 mock transport; FakeEmbedder vectors."""

import json
import math

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from rozmova_core.models import EMBEDDING_DIMENSIONS
from rozmova_engine.rag.embeddings import FakeEmbedder, OpenAIEmbedder, fake_vector


class EmbeddingsApi:
    """Answers /v1/embeddings with vectors whose first value is the input's index."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        inputs = body["input"]
        # Reversed on purpose: the client must order by "index", not by position.
        data = [
            {"object": "embedding", "index": i, "embedding": [float(i)] * 3}
            for i in reversed(range(len(inputs)))
        ]
        usage = {"prompt_tokens": 10 * len(inputs), "total_tokens": 10 * len(inputs)}
        return httpx2.Response(
            200, json={"object": "list", "data": data, "model": body["model"], "usage": usage}
        )


def _embedder(api: EmbeddingsApi, batch_size: int) -> OpenAIEmbedder:
    client = AsyncOpenAI(
        api_key="sk-test",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(api)),
    )
    return OpenAIEmbedder(client=client, batch_size=batch_size)


async def test_openai_embedder_batches_and_keeps_order() -> None:
    api = EmbeddingsApi()
    embedder = _embedder(api, batch_size=2)

    vectors = await embedder.embed(["а", "б", "в", "г", "ґ"])

    assert [len(request["input"]) for request in api.requests] == [2, 2, 1]  # type: ignore[arg-type]
    assert [vector[0] for vector in vectors] == [0.0, 1.0, 0.0, 1.0, 0.0]
    assert all(request["model"] == "text-embedding-3-small" for request in api.requests)
    assert all(request["dimensions"] == EMBEDDING_DIMENSIONS for request in api.requests)
    assert embedder.usage_tokens == 50
    await embedder.aclose()


async def test_openai_embedder_with_no_texts_makes_no_request() -> None:
    api = EmbeddingsApi()

    assert await _embedder(api, batch_size=2).embed([]) == []
    assert api.requests == []


def test_create_reads_the_secret_key() -> None:
    embedder = OpenAIEmbedder.create(SecretStr("sk-secret"), "text-embedding-3-large")

    assert embedder.model == "text-embedding-3-large"
    assert embedder.client.api_key == "sk-secret"


async def test_fake_embedder_is_deterministic_and_records_calls() -> None:
    embedder = FakeEmbedder()

    first = await embedder.embed(["тариф «Безлім»", "роумінг"])
    again = await embedder.embed(["тариф «Безлім»"])

    assert first[0] == again[0] != first[1]
    assert embedder.embedded_texts == ["тариф «Безлім»", "роумінг", "тариф «Безлім»"]


@pytest.mark.parametrize("text", ["", "Код тарифу: `bezlim`"])
def test_fake_vector_is_a_unit_vector_of_the_model_size(text: str) -> None:
    vector = fake_vector(text)

    assert len(vector) == EMBEDDING_DIMENSIONS
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0)
