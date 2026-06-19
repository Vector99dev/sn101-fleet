"""Local replicas of the validator's validity + diversity checks.

These mirror the math in
tag101.tasks.sn101_reference.core.scoring.{validity,diversity,preprocessing}
closely enough that anything passing OUR check will pass the validator's check.
"""

from __future__ import annotations

import re
import unicodedata

import numpy as np

_URL_PATTERN = re.compile(
    r"(https?://\S+|www\.\S+|\b[a-z0-9-]+\.(com|org|net|io|ai|co)\b)",
    re.IGNORECASE,
)


def normalize_tag(tag: str) -> str:
    """Match validator's preprocessing.normalize_tag."""
    return re.sub(r"\s+", " ", tag.strip().lower())


def _tokenize(text: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]+", text.lower())
    out = []
    for token in raw:
        if token.endswith("ies") and len(token) > 4:
            out.append(f"{token[:-3]}y")
        elif token.endswith("s") and len(token) > 3:
            out.append(token[:-1])
        else:
            out.append(token)
    return out


def format_ok(tag: str) -> bool:
    """Match validator's _format_score == 1.0."""
    s = tag.strip()
    if not s:
        return False
    if _URL_PATTERN.search(s):
        return False
    tokens = _tokenize(s)
    if len(tokens) < 1 or len(tokens) > 5:
        return False
    if re.fullmatch(r"[\d\s.,:/%+\-]+", s):
        return False
    if re.fullmatch(r"[^\w\s]+", s, flags=re.UNICODE):
        return False
    compact = "".join(ch for ch in s if not ch.isspace())
    if compact and not any(ch.isalnum() for ch in compact):
        if all(unicodedata.category(ch).startswith(("S", "P")) for ch in compact):
            return False
    compact_chars = [ch for ch in s if not ch.isspace()]
    if compact_chars:
        special = sum(
            1
            for ch in compact_chars
            if not ch.isalnum() and ch not in {"-", "_", "&", "/", "+"}
        )
        if special / len(compact_chars) > 0.3:
            return False
    return True


def lexical_overlap(tag: str, spans: list[str]) -> float:
    """Match validator's _lexical_overlap_score."""
    tag_tokens = _tokenize(tag)
    if not tag_tokens:
        return 0.0
    best = 0.0
    for span in spans:
        span_set = set(_tokenize(span))
        if not span_set:
            continue
        hits = sum(1 for t in tag_tokens if t in span_set)
        best = max(best, hits / len(tag_tokens))
    return best


def scaled_similarity(sim: float, low: float = 0.30, high: float = 0.75) -> float:
    if sim <= low:
        return 0.0
    if sim >= high:
        return 1.0
    return (sim - low) / (high - low)


def tier(raw: float) -> float:
    """Match validator's _tier_map_validity."""
    if raw < 0.15:
        return 0.0
    if raw < 0.35:
        return 0.3
    if raw < 0.65:
        return 0.6
    return 1.0


def build_spans(post: str) -> list[str]:
    """Simplified version of preprocessing.build_spans (no spaCy dependency).

    We include the whole post and each sentence. We skip the spaCy named-entity
    extraction step — it's a strict superset for the validator, but the spans
    we DO build are still a subset the validator definitely also has, so any
    tag that passes our lexical check passes theirs too. The semantic check
    is the same regardless.
    """
    spans: list[str] = []
    seen: set[str] = set()
    cleaned = post.strip()
    if cleaned:
        spans.append(cleaned)
        seen.add(cleaned.lower())
    for sent in re.split(r"[.!?\n]+", post):
        s = sent.strip().strip("\"'`.,;:!?()[]{}")
        if s and s.lower() not in seen:
            spans.append(s)
            seen.add(s.lower())
    return spans or [post]


def validity_for(
    tag: str,
    post_embedding: np.ndarray,
    span_embeddings: np.ndarray,
    tag_embedding: np.ndarray,
    spans: list[str],
) -> float:
    """Return the predicted validity tier (0, 0.3, 0.6, 1.0) for a tag."""
    if not format_ok(tag):
        return 0.0
    sim_post = scaled_similarity(float(np.dot(tag_embedding, post_embedding)))
    sim_span = scaled_similarity(
        float(np.max(span_embeddings @ tag_embedding)) if len(span_embeddings) else 0.0
    )
    lex = lexical_overlap(tag, spans)
    base = max(sim_post, sim_span, lex)
    return tier(base * 1.0)


DIVERSITY_LOW = 0.55
DIVERSITY_HIGH = 0.85


def diversity_for(tag_embeddings: np.ndarray) -> list[float]:
    """For each tag, score its diversity vs. the others in the miner's set."""
    n = len(tag_embeddings)
    if n <= 1:
        return [1.0] * n
    out: list[float] = []
    for i in range(n):
        sims = tag_embeddings @ tag_embeddings[i]
        sims[i] = -1.0
        nearest = float(np.clip(np.max(sims), 0.0, 1.0))
        if nearest <= DIVERSITY_LOW:
            out.append(1.0)
        elif nearest >= DIVERSITY_HIGH:
            out.append(0.0)
        else:
            out.append(
                1.0 - (nearest - DIVERSITY_LOW) / (DIVERSITY_HIGH - DIVERSITY_LOW)
            )
    return out


def enforce_diversity_picks(
    candidates: list[str],
    candidate_embeddings: np.ndarray,
    n: int = 3,
    threshold: float = DIVERSITY_LOW,
) -> list[int]:
    """Greedy pick of indices that keep pairwise similarity below threshold."""
    picked_idx: list[int] = []
    picked_emb: list[np.ndarray] = []
    for i, emb in enumerate(candidate_embeddings):
        if all(float(np.dot(emb, p)) < threshold for p in picked_emb):
            picked_idx.append(i)
            picked_emb.append(emb)
        if len(picked_idx) >= n:
            break
    return picked_idx
