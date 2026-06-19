"""Solver service: the brain shared by all your miners.

Pipeline per call:
  1. Hash the tweet content -> per-key lock (so concurrent miners share work).
  2. Look in the exact tweet cache. Hit -> return cached tags.
  3. Try vocabulary-cache match (embed tweet, score vs pre-embedded vocab).
     If we find 3 strong canonical tags, validate locally and return.
  4. LLM fallback -> generate ~5 candidates from OpenRouter (or mock).
  5. Canonicalize variants -> normalize -> dedupe.
  6. Filter through local validity check (mirror of the validator's logic).
  7. Enforce intra-set diversity (pairwise cosine < 0.55).
  8. If short of 3, top up from vocab.
  9. Cache and return.

All 10 miners hitting this service for the same tweet share a single LLM call.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field

import numpy as np
from sentence_transformers import SentenceTransformer

from .cache import AsyncTTLCache, KeyedLocks
from .llm import LLMRouter
from .scoring import (
    build_spans,
    diversity_for,
    enforce_diversity_picks,
    format_ok,
    normalize_tag,
    tier as validity_tier,
    lexical_overlap,
    scaled_similarity,
)
from .vocab import VOCABULARY, canonicalize


logger = logging.getLogger("sn101.solver")


SAFE_DEFAULT_TAGS = ["ai", "tech", "release"]


@dataclass
class SolveStats:
    total: int = 0
    exact_hits: int = 0
    vocab_hits: int = 0
    llm_calls: int = 0
    failures: int = 0
    last_path: str = ""

    def snapshot(self) -> dict:
        return {
            "total": self.total,
            "exact_hits": self.exact_hits,
            "vocab_hits": self.vocab_hits,
            "llm_calls": self.llm_calls,
            "failures": self.failures,
            "last_path": self.last_path,
        }


@dataclass
class SolveResult:
    tags: list[str]
    path: str  # "exact_cache" | "vocab" | "llm" | "fallback"
    latency_ms: float = 0.0
    info: dict = field(default_factory=dict)


class Solver:
    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        n_tags: int = 3,
        cache_ttl_seconds: float = 24 * 3600,
        cache_max: int = 50_000,
        llm: LLMRouter | None = None,
        vocab: list[str] | None = None,
    ) -> None:
        self.n_tags = int(n_tags)
        self.model = SentenceTransformer(model_name)
        self.vocab = list(vocab or VOCABULARY)
        self._vocab_embs = self._embed(self.vocab)
        self.cache = AsyncTTLCache(maxsize=cache_max, ttl=cache_ttl_seconds)
        self.locks = KeyedLocks()
        self.llm = llm or LLMRouter()
        self.stats = SolveStats()

    # ------------------------------------------------------------------ utils

    def _embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 384), dtype=np.float32)
        return self.model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        ).astype(np.float32)

    @staticmethod
    def _key(tweet: str) -> str:
        return hashlib.sha256(tweet.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ flow

    async def solve(self, tweet: str) -> SolveResult:
        import time

        started = time.perf_counter()
        self.stats.total += 1
        key = self._key(tweet)

        # Per-key lock so a flood of concurrent miners only fires one LLM call.
        lock = await self.locks.acquire(key)
        try:
            async with lock:
                # 1. Exact cache
                cached = await self.cache.get(key)
                if cached is not None:
                    self.stats.exact_hits += 1
                    self.stats.last_path = "exact_cache"
                    return SolveResult(
                        tags=list(cached),  # type: ignore[arg-type]
                        path="exact_cache",
                        latency_ms=(time.perf_counter() - started) * 1000,
                    )

                # 2. Vocab match — cheap, deterministic
                vocab_result = self._vocab_match(tweet)
                if vocab_result is not None:
                    await self.cache.set(key, vocab_result)
                    self.stats.vocab_hits += 1
                    self.stats.last_path = "vocab"
                    return SolveResult(
                        tags=vocab_result,
                        path="vocab",
                        latency_ms=(time.perf_counter() - started) * 1000,
                    )

                # 3. LLM fallback
                try:
                    candidates = await self.llm.generate(tweet, n=8)
                    self.stats.llm_calls += 1
                except Exception as exc:
                    logger.warning("LLM generate failed: %s", exc)
                    candidates = []

                tags = self._postprocess(tweet, candidates)
                if len(tags) < self.n_tags:
                    # Top up from vocab tags that pass validity
                    topup = self._topup_from_vocab(tweet, exclude=tags)
                    for t in topup:
                        if t not in tags:
                            tags.append(t)
                        if len(tags) >= self.n_tags:
                            break

                if len(tags) < self.n_tags:
                    # Last resort: pad with safe defaults
                    for t in SAFE_DEFAULT_TAGS:
                        if t not in tags:
                            tags.append(t)
                        if len(tags) >= self.n_tags:
                            break
                    self.stats.failures += 1
                    self.stats.last_path = "fallback"
                    path = "fallback"
                else:
                    self.stats.last_path = "llm"
                    path = "llm"

                tags = tags[: self.n_tags]
                await self.cache.set(key, tags)
                return SolveResult(
                    tags=tags,
                    path=path,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    info={"raw_candidates": candidates},
                )
        finally:
            await self.locks.release(key)

    # --------------------------------------------------------------- vocab fast path

    def _vocab_match(self, tweet: str) -> list[str] | None:
        """Try to assemble n_tags from the static vocabulary alone."""
        tweet_emb = self._embed([tweet])[0]
        sims = self._vocab_embs @ tweet_emb  # cosine sim because unit vectors

        lowered = tweet.lower()
        # Strong boost for tags that literally appear in the tweet text
        # (the validator's lexical_overlap shortcut == 1.0 -> validity tier 1.0)
        for i, tag in enumerate(self.vocab):
            if tag in lowered:
                sims[i] += 0.5

        order = np.argsort(-sims)
        picked: list[str] = []
        picked_embs: list[np.ndarray] = []
        spans = build_spans(tweet)
        span_embs = self._embed(spans)

        for idx in order:
            score = float(sims[idx])
            if score < 0.35:
                break
            cand = self.vocab[idx]
            cand_emb = self._vocab_embs[idx]
            # Diversity check vs already picked
            if any(float(np.dot(cand_emb, p)) >= 0.55 for p in picked_embs):
                continue
            # Validity check
            v = self._predicted_validity(cand, tweet_emb, span_embs, cand_emb, spans)
            if v < 0.6:
                continue
            picked.append(cand)
            picked_embs.append(cand_emb)
            if len(picked) >= self.n_tags:
                break

        if len(picked) >= self.n_tags:
            return picked
        return None

    def _topup_from_vocab(self, tweet: str, exclude: list[str]) -> list[str]:
        tweet_emb = self._embed([tweet])[0]
        sims = self._vocab_embs @ tweet_emb
        order = np.argsort(-sims)
        exclude_set = set(exclude)
        spans = build_spans(tweet)
        span_embs = self._embed(spans)
        out: list[str] = []
        for idx in order:
            tag = self.vocab[idx]
            if tag in exclude_set:
                continue
            cand_emb = self._vocab_embs[idx]
            v = self._predicted_validity(tag, tweet_emb, span_embs, cand_emb, spans)
            if v < 0.6:
                continue
            out.append(tag)
            if len(out) >= self.n_tags:
                break
        return out

    # --------------------------------------------------------------- LLM postprocess

    def _postprocess(self, tweet: str, candidates: list[str]) -> list[str]:
        """Clean + canonicalize + validity-filter + diversity-pick."""
        # Step 1: clean and canonicalize
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in candidates:
            if not isinstance(raw, str):
                continue
            n = normalize_tag(raw)
            n = canonicalize(n)
            if not n or n in seen:
                continue
            if not format_ok(n):
                continue
            seen.add(n)
            cleaned.append(n)
        if not cleaned:
            return []

        # Step 2: validity filter using local replica of the scorer
        tweet_emb = self._embed([tweet])[0]
        spans = build_spans(tweet)
        span_embs = self._embed(spans)
        cand_embs = self._embed(cleaned)
        survivors: list[str] = []
        survivor_embs: list[np.ndarray] = []
        for tag, emb in zip(cleaned, cand_embs):
            v = self._predicted_validity(tag, tweet_emb, span_embs, emb, spans)
            if v >= 0.6:  # we want tier 0.6 or better
                survivors.append(tag)
                survivor_embs.append(emb)

        if not survivors:
            return []

        # Step 3: diversity-aware pick
        idxs = enforce_diversity_picks(
            survivors, np.stack(survivor_embs), n=self.n_tags
        )
        return [survivors[i] for i in idxs]

    # --------------------------------------------------------------- validity predictor

    @staticmethod
    def _predicted_validity(
        tag: str,
        tweet_emb: np.ndarray,
        span_embs: np.ndarray,
        tag_emb: np.ndarray,
        spans: list[str],
    ) -> float:
        if not format_ok(tag):
            return 0.0
        sim_post = scaled_similarity(float(np.dot(tag_emb, tweet_emb)))
        if len(span_embs) > 0:
            sim_span = scaled_similarity(float(np.max(span_embs @ tag_emb)))
        else:
            sim_span = 0.0
        lex = lexical_overlap(tag, spans)
        return validity_tier(max(sim_post, sim_span, lex))

    # --------------------------------------------------------------- shutdown

    async def aclose(self) -> None:
        await self.llm.close()
