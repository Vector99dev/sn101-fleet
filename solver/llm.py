"""LLM router with OpenRouter primary and a deterministic mock fallback.

If OPENROUTER_API_KEY is set, calls OpenRouter with a model fallback chain.
Otherwise, returns a deterministic mock derived from the tweet text so the
pipeline can be tested end-to-end without any external API.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Iterable

import httpx


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_CHAIN: tuple[str, ...] = (
    "openai/gpt-4o-mini",
    "google/gemini-flash-1.5",
    "anthropic/claude-haiku-4.5",
    "meta-llama/llama-3.3-70b-instruct",
)

_SYSTEM_PROMPT = (
    "Generate topic tags for a social post. Return ONLY a JSON array of "
    "lowercase strings, 1-2 words each. Output exactly {n} tags. No duplicates."
)
_USER_PROMPT = "Tweet:\n{post}"


class LLMRouter:
    def __init__(
        self,
        api_key: str | None = None,
        chain: Iterable[str] = DEFAULT_CHAIN,
        per_model_timeout: float = 3.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.chain = tuple(chain)
        self.timeout = per_model_timeout
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout + 1.0, connect=2.0),
                headers={
                    "Authorization": f"Bearer {self.api_key or ''}",
                    "HTTP-Referer": "https://github.com/tag101-fleet",
                    "X-Title": "tag101-solver",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def generate(self, tweet: str, n: int = 5) -> list[str]:
        """Return up to n candidate tags. Falls through models on failure."""
        if not self.api_key:
            return self._mock_generate(tweet, n)

        for model in self.chain:
            try:
                tags = await asyncio.wait_for(
                    self._call(model, tweet, n), timeout=self.timeout + 1.0
                )
                if tags:
                    return tags
            except Exception:
                continue
        # All providers failed; fall back to mock so the miner still answers.
        return self._mock_generate(tweet, n)

    async def _call(self, model: str, tweet: str, n: int) -> list[str]:
        client = await self._http()
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT.format(n=n)},
                {"role": "user", "content": _USER_PROMPT.format(post=tweet)},
            ],
            "temperature": 0.0,
        }
        r = await client.post(OPENROUTER_URL, json=body)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return self._parse(content)

    @staticmethod
    def _parse(content: str) -> list[str]:
        text = content.strip()
        if text.startswith("```"):
            text = "\n".join(
                line for line in text.splitlines() if not line.strip().startswith("```")
            ).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = re.split(r"[,\n]", text)
        if not isinstance(parsed, list):
            return []
        return [str(x).strip() for x in parsed if str(x).strip()]

    @staticmethod
    def _mock_generate(tweet: str, n: int) -> list[str]:
        """Deterministic mock: extract plausible tags from tweet content.

        Strategy:
        1. Lowercase the tweet.
        2. Identify words that appear in our static vocabulary.
        3. If we don't find enough, fall back to longest alphanumeric tokens.

        This is good enough to verify the pipeline without an LLM, and the
        results are deterministic per tweet — perfect for testing.
        """
        from .vocab import VOCABULARY  # local import to avoid cycle

        lowered = tweet.lower()
        found: list[str] = []
        for term in VOCABULARY:
            if term in lowered and term not in found:
                found.append(term)
            if len(found) >= n:
                break

        if len(found) < n:
            tokens = re.findall(r"[a-z][a-z0-9-]{3,}", lowered)
            seen = set(found)
            for tok in tokens:
                if tok not in seen:
                    found.append(tok)
                    seen.add(tok)
                if len(found) >= n:
                    break

        return found[:n]
