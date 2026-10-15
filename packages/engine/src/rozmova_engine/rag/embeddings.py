"""Text embeddings for the knowledge base and, later, for search queries (ADR 0004).

``OpenAIEmbedder`` calls ``text-embedding-3-small`` in batches; ``FakeEmbedder`` gives
deterministic unit vectors from a hash, for tests and for runs without an API key.
"""

import asyncio
import hashlib
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpx2
from openai import AsyncOpenAI
from pydantic import SecretStr

from rozmova_core.models import EMBEDDING_DIMENSIONS

DEFAULT_MODEL = "text-embedding-3-small"
# The API accepts up to 2048 inputs per request; 128 chunks of <=800 tokens stay far
# below the per-request token limit and keep one failed request cheap to retry.
DEFAULT_BATCH_SIZE = 128
DEFAULT_TIMEOUT_S = 60.0


class Embedder(Protocol):
    model: str

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector of ``EMBEDDING_DIMENSIONS`` floats per text, in order."""
        ...


@dataclass(slots=True)
class OpenAIEmbedder:
    """``usage_tokens`` sums the tokens OpenAI billed across calls."""

    client: AsyncOpenAI
    model: str = DEFAULT_MODEL
    batch_size: int = DEFAULT_BATCH_SIZE
    timeout_s: float = DEFAULT_TIMEOUT_S
    usage_tokens: int = 0

    @classmethod
    def create(
        cls,
        api_key: SecretStr,
        model: str = DEFAULT_MODEL,
        *,
        http_client: httpx2.AsyncClient | None = None,
    ) -> "OpenAIEmbedder":
        # The SDK retries 429 and 5xx with backoff; the overall wait is bounded in embed().
        client = AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            max_retries=3,
            timeout=DEFAULT_TIMEOUT_S,
            http_client=http_client,
        )
        return cls(client=client, model=model)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            async with asyncio.timeout(self.timeout_s * 2):
                response = await self.client.embeddings.create(
                    model=self.model,
                    input=batch,
                    dimensions=EMBEDDING_DIMENSIONS,
                )
            self.usage_tokens += response.usage.total_tokens
            ordered = sorted(response.data, key=lambda item: item.index)
            vectors.extend(item.embedding for item in ordered)
        if len(vectors) != len(texts):
            raise RuntimeError(f"expected {len(texts)} embeddings, got {len(vectors)}")
        return vectors

    async def aclose(self) -> None:
        await self.client.close()


@dataclass(slots=True)
class FakeEmbedder:
    """Deterministic embeddings with no network; ``calls`` records each batch."""

    model: str = "fake-embedding"
    calls: list[list[str]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [fake_vector(text) for text in texts]

    @property
    def embedded_texts(self) -> list[str]:
        return [text for call in self.calls for text in call]


def fake_vector(text: str) -> list[float]:
    """A unit vector derived from ``text``: equal texts give equal vectors."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    raw: list[float] = []
    counter = 0
    while len(raw) < EMBEDDING_DIMENSIONS:
        block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        raw.extend(value / 2**31 for value in struct.unpack(">8i", block))
        counter += 1
    vector = raw[:EMBEDDING_DIMENSIONS]
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]
