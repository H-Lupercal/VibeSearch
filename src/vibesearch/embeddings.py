"""Synchronous local dense embedding boundary."""

from __future__ import annotations

import math
import re
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def validate_vectors(vectors: list[list[float]], dimension: int) -> None:
    if dimension <= 0:
        raise ValueError("Embedding dimension must be positive")
    for vector in vectors:
        if len(vector) != dimension or any(
            not math.isfinite(float(value)) for value in vector
        ):
            raise ValueError(
                "Embedding vector has an invalid dimension or non-finite value"
            )


class LocalSentenceTransformer:
    """Lazy model load. The default BGE-M3 contract uses no retrieval prefix.

    Explicit revisions should be immutable commit hashes. An unpinned revision is
    resolved from the loaded model's configuration and recorded before indexing.
    No remote-code trust is enabled. No alternative model is an automatic fallback.
    """

    _MODEL_PREFIXES = {
        "Qwen/Qwen3-Embedding-0.6B": (
            "",
            "Instruct: Given a natural-language description, retrieve relevant gallery metadata\nQuery: ",
        ),
        "nomic-ai/nomic-embed-text-v1.5": ("search_document: ", "search_query: "),
    }

    def __init__(
        self,
        model_id: str = "BAAI/bge-m3",
        *,
        revision: str | None = None,
        device: str = "auto",
        batch_size: int = 8,
        normalize: bool = True,
        document_prefix: str | None = None,
        query_prefix: str | None = None,
        offline: bool = False,
    ) -> None:
        if device not in ("auto", "cpu", "cuda", "mps"):
            raise ValueError("device must be auto, cpu, cuda, or mps")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.model_id, self.requested_revision = model_id, revision
        self.device, self.batch_size, self.normalize = device, batch_size, normalize
        defaults = self._MODEL_PREFIXES.get(model_id, ("", ""))
        self.document_prefix = (
            defaults[0] if document_prefix is None else document_prefix
        )
        self.query_prefix = defaults[1] if query_prefix is None else query_prefix
        self.offline = offline
        self._model = None
        self._revision: str | None = None
        self._dimension: int | None = None

    def _load(self):
        if self._model is None:
            import torch
            from sentence_transformers import SentenceTransformer

            available = {
                "cpu": True,
                "cuda": torch.cuda.is_available(),
                "mps": bool(
                    hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
                ),
            }
            if self.device != "auto" and not available[self.device]:
                raise RuntimeError(
                    f"Requested embedding device {self.device} is unavailable"
                )
            selected = (
                self.device
                if self.device != "auto"
                else next((item for item in ("cuda", "mps", "cpu") if available[item]))
            )
            model = SentenceTransformer(
                self.model_id,
                revision=self.requested_revision,
                device=selected,
                trust_remote_code=False,
                local_files_only=self.offline,
            )
            dimension_method = getattr(model, "get_embedding_dimension", None)
            dimension = (
                dimension_method()
                if dimension_method
                else model.get_sentence_embedding_dimension()
            )
            if not isinstance(dimension, int) or dimension <= 0:
                raise ValueError("Model has no valid dense embedding dimension")
            revision = None
            try:
                revision = model[0].auto_model.config._commit_hash
            except (AttributeError, IndexError, KeyError, TypeError):
                pass
            if not revision:
                from huggingface_hub import try_to_load_from_cache

                cached_config = try_to_load_from_cache(
                    self.model_id,
                    "config.json",
                    revision=self.requested_revision or "main",
                )
                match = re.search(
                    r"[/\\]snapshots[/\\]([0-9a-f]{40})(?:[/\\]|$)", str(cached_config)
                )
                revision = match.group(1) if match else None
            if not revision:
                match = re.search(
                    r"[/\\]snapshots[/\\]([0-9a-f]{40})(?:[/\\]|$)",
                    str(getattr(model, "model_card_data", "")),
                )
                revision = match.group(1) if match else None
            if (
                not revision
                and self.requested_revision
                and re.fullmatch(r"[0-9a-f]{40}", self.requested_revision)
            ):
                revision = self.requested_revision
            if not revision:
                raise RuntimeError(
                    "Could not resolve an immutable model revision; pin a commit hash"
                )
            self._model, self._revision, self._dimension = model, revision, dimension
        return self._model

    @property
    def revision(self) -> str:
        self._load()
        assert self._revision is not None
        return self._revision

    @property
    def dimension(self) -> int:
        self._load()
        assert self._dimension is not None
        return self._dimension

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        encoded = model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=self.normalize,
            convert_to_numpy=True,
            show_progress_bar=False,
            prompt="",
        )
        vectors = [list(map(float, row)) for row in encoded]
        validate_vectors(vectors, self.dimension)
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode([self.document_prefix + text for text in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._encode([self.query_prefix + text])[0]
