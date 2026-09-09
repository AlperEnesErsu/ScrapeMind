"""Embedding service for ScrapeMind (Faz 5.4).

Provides vector embedding generation for papers, search queries, and RAG
chat retrieval. Talks to OpenAI-compatible endpoints (OpenRouter, OpenAI,
or local Ollama).

Like `ai_service`, this module is provider-agnostic, fail-open (returns None
on any error or missing key so callers can fall back to lexical matching),
and supports per-user API keys when configured.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

import structlog
from flask import current_app

from app.extensions import db
from app.modules.scrape.models import Paper

logger = structlog.get_logger()

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIM = 1536
BATCH_SIZE = 32


def _resolve_embedding_config(user=None) -> tuple[str, str, str, str, int] | None:
    """Resolve (provider, base_url, api_key, model, dimension) for embedding.

    Returns None if embedding is disabled or no usable key/provider is found.
    """
    provider = (
        (
            current_app.config.get("EMBEDDING_PROVIDER")
            or current_app.config.get("LLM_PROVIDER")
            or "openrouter"
        )
        .strip()
        .lower()
    )

    if provider == "none":
        return None

    dim = int(current_app.config.get("EMBEDDING_DIM") or DEFAULT_EMBEDDING_DIM)
    model = (current_app.config.get("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL).strip()

    if provider == "ollama":
        base_url = (
            current_app.config.get("EMBEDDING_BASE_URL")
            or current_app.config.get("OLLAMA_BASE_URL")
            or "http://localhost:11434/v1"
        )
        return "ollama", base_url, "ollama", model, dim

    # OpenRouter or OpenAI
    from app.modules.scrape.ai_service import get_user_llm_key

    user_key, _ = get_user_llm_key(user)
    api_key = (
        (current_app.config.get("EMBEDDING_API_KEY") or "").strip()
        or user_key
        or (current_app.config.get("OPENROUTER_API_KEY") or "").strip()
        or None
    )

    if not api_key:
        return None

    base_url = (
        current_app.config.get("EMBEDDING_BASE_URL")
        or current_app.config.get("OPENROUTER_BASE_URL")
        or "https://openrouter.ai/api/v1"
    )
    return provider, base_url, api_key, model, dim


def is_embedding_enabled(user=None) -> bool:
    """True iff an embedding provider + key is configured (or testing mock is enabled)."""
    provider = (
        (
            current_app.config.get("EMBEDDING_PROVIDER")
            or current_app.config.get("LLM_PROVIDER")
            or "openrouter"
        )
        .strip()
        .lower()
    )
    if provider == "none":
        return False

    if current_app.config.get("TESTING") and current_app.config.get("MOCK_EMBEDDINGS", True):
        return True

    return _resolve_embedding_config(user) is not None


def paper_text_for_embedding(paper: Paper) -> str:
    """Construct the canonical text representation of a paper for embedding."""
    parts: list[str] = []
    title = (paper.title or "").strip()
    if title:
        parts.append(title)
    abstract = (paper.abstract or "").strip()
    if abstract:
        parts.append(abstract)
    return "\n\n".join(parts)


def deterministic_mock_embedding(text: str, dim: int = DEFAULT_EMBEDDING_DIM) -> list[float]:
    """Generate a deterministic normalized unit vector from a text string.

    Useful for test environments and offline development to verify cosine
    distance and vector math without live external API calls.
    """
    if not text:
        return [0.0] * dim

    seed = hashlib.sha256(text.encode("utf-8")).digest()
    vec = []
    for i in range(dim):
        byte_val = seed[i % len(seed)]
        offset = (i // len(seed)) + 1
        val = math.sin((byte_val + 1) * offset)
        vec.append(val)

    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


def get_embedding(text: str, user=None) -> list[float] | None:
    """Compute vector embedding for a single text.

    Returns None if embedding is disabled or the provider call fails.
    """
    results = get_embeddings_batch([text], user=user)
    if results and results[0]:
        return results[0]
    return None


def get_embeddings_batch(texts: list[str], user=None) -> list[list[float] | None]:
    """Compute embeddings for a batch of texts in a single provider call.

    Returns a list of vectors (or None for failed items) matching the input texts.
    """
    clean_texts = [t.strip() for t in texts]
    if not clean_texts:
        return []

    # If test mode has MOCK_EMBEDDINGS set, return deterministic mock vectors
    if current_app.config.get("TESTING") and current_app.config.get("MOCK_EMBEDDINGS", True):
        dim = int(current_app.config.get("EMBEDDING_DIM") or DEFAULT_EMBEDDING_DIM)
        return [deterministic_mock_embedding(t, dim=dim) if t else None for t in clean_texts]

    config = _resolve_embedding_config(user)
    if config is None:
        return [None] * len(clean_texts)

    _provider, base_url, api_key, model, expected_dim = config

    try:
        from openai import OpenAI

        client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            default_headers={
                "HTTP-Referer": "https://github.com/scrapemind",
                "X-Title": "ScrapeMind",
            },
        )

        valid_indices = [i for i, t in enumerate(clean_texts) if t]
        if not valid_indices:
            return [None] * len(clean_texts)

        payload_texts = [clean_texts[i] for i in valid_indices]

        kwargs: dict[str, Any] = {
            "model": model,
            "input": payload_texts,
        }
        if "text-embedding-3" in model:
            kwargs["dimensions"] = expected_dim

        resp = client.embeddings.create(**kwargs)

        output: list[list[float] | None] = [None] * len(clean_texts)
        for idx, item in enumerate(resp.data):
            orig_idx = valid_indices[idx]
            vec = item.embedding
            if len(vec) == expected_dim:
                output[orig_idx] = vec
            else:
                logger.warning(
                    "embedding_dimension_mismatch",
                    expected=expected_dim,
                    got=len(vec),
                    model=model,
                )
                output[orig_idx] = None
        return output

    except Exception as exc:
        logger.warning("embedding_batch_failed", error=str(exc), count=len(texts))
        return [None] * len(clean_texts)


def embed_paper(paper: Paper, user=None, commit: bool = True) -> bool:
    """Generate and store the embedding vector for a Paper row.

    Returns True if embedding was generated and stored, False otherwise.
    """
    text = paper_text_for_embedding(paper)
    if not text:
        return False

    vec = get_embedding(text, user=user)
    if vec is None:
        return False

    paper.embedding = vec
    if commit:
        db.session.commit()
    return True


def embed_papers_batch(papers: list[Paper], user=None, commit: bool = True) -> int:
    """Embed multiple papers in chunks. Returns count of successfully embedded papers."""
    to_embed = [p for p in papers if p.embedding is None and paper_text_for_embedding(p)]
    if not to_embed:
        return 0

    success_count = 0
    for i in range(0, len(to_embed), BATCH_SIZE):
        batch = to_embed[i : i + BATCH_SIZE]
        texts = [paper_text_for_embedding(p) for p in batch]
        vectors = get_embeddings_batch(texts, user=user)

        for paper, vec in zip(batch, vectors):
            if vec is not None:
                paper.embedding = vec
                success_count += 1

    if commit and success_count > 0:
        db.session.commit()
    return success_count
