"""Typed production query embedding factory (B6-L Finding 2).

The runtime never downloads or loads models itself: the production factory
owns the CUDA-preflight loader and fails closed on revision, dimension, and
CUDA availability mismatches. Tests inject fakes through the same typed
contract.

Query embedding mode policy (Wave 1):
- ``production_pinned``: ``ProductionBgeM3QueryEmbeddingFactory``; BGE-M3
  revision ``BGE_M3_REVISION`` and dimension ``BGE_M3_DIMENSION`` are pinned,
  CUDA is required, and there is no silent CPU fallback.
- ``mock_unit_test``: deterministic/fake embedders are allowed only in unit
  tests or explicit mock runs; every artifact that uses them must record
  ``embedding_mode=mock_unit_test`` and must never be mixed into production
  dense/hybrid scoring.
- ``degraded``: manager-approved degraded runs only (for example reranker
  unavailable); never a silent automatic downgrade.
"""

from __future__ import annotations

import math
from typing import Callable, Protocol

import numpy as np

from catalyst_data.config import BGE_M3_DIMENSION, BGE_M3_MODEL, BGE_M3_REVISION
from catalyst_data.retrieval.gpu_driver import load_real_cuda_embedder


class QueryEmbedder(Protocol):
    """Typed single-query embedder contract (no ``Any`` in the boundary)."""

    @property
    def dimension(self) -> int: ...

    @property
    def model_revision(self) -> str: ...

    def embed_query(self, query: str) -> np.ndarray: ...


class QueryEmbeddingFactory(Protocol):
    """Creates a typed query embedder for the pinned production model."""

    def create(self, *, model_name: str) -> QueryEmbedder: ...


class BgeM3QueryEmbedder:
    """L2-normalized BGE-M3 single-query embedder wrapper."""

    def __init__(
        self,
        *,
        embed_batch_fn: Callable[[list[str]], np.ndarray],
        dimension: int = BGE_M3_DIMENSION,
        model_revision: str = BGE_M3_REVISION,
    ) -> None:
        if dimension != BGE_M3_DIMENSION:
            raise ValueError(f"BGE-M3 query embedder dimension must be {BGE_M3_DIMENSION}")
        if model_revision != BGE_M3_REVISION:
            raise ValueError("BGE-M3 query embedder revision must be the pinned revision")
        self._embed_batch_fn = embed_batch_fn
        self._dimension = dimension
        self._model_revision = model_revision

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_revision(self) -> str:
        return self._model_revision

    def embed_query(self, query: str) -> np.ndarray:
        matrix = np.asarray(self._embed_batch_fn([query]), dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape != (1, self._dimension):
            raise ValueError("query embedder returned a non-contract matrix")
        vector = matrix[0]
        norm = float(np.linalg.norm(vector))
        if not math.isfinite(norm) or norm == 0.0:
            raise ValueError("query embedder returned a non-finite or zero vector")
        return (vector / norm).astype(np.float32, copy=False)


class ProductionBgeM3QueryEmbeddingFactory:
    """Fail-closed production factory with no silent CPU fallback."""

    def __init__(
        self,
        *,
        model_revision: str = BGE_M3_REVISION,
        dimension: int = BGE_M3_DIMENSION,
        cuda_check: Callable[[], bool] | None = None,
        embedder_loader: Callable[..., Callable[[list[str]], np.ndarray]] | None = None,
    ) -> None:
        if model_revision != BGE_M3_REVISION:
            raise ValueError("production query embedding revision must be the pinned BGE-M3 revision")
        if dimension != BGE_M3_DIMENSION:
            raise ValueError(f"production query embedding dimension must be {BGE_M3_DIMENSION}")
        self._model_revision = model_revision
        self._dimension = dimension
        self._cuda_check = cuda_check
        self._embedder_loader = embedder_loader or load_real_cuda_embedder

    def create(self, *, model_name: str) -> QueryEmbedder:
        if model_name != BGE_M3_MODEL:
            raise ValueError("query embedding model must be the pinned BGE-M3 model")
        if self._model_revision != BGE_M3_REVISION:
            raise ValueError("query embedding revision must be the pinned BGE-M3 revision")
        if self._dimension != BGE_M3_DIMENSION:
            raise ValueError(f"query embedding dimension must be {BGE_M3_DIMENSION}")
        if self._cuda_check is not None:
            cuda_available = bool(self._cuda_check())
        else:
            try:
                import torch
            except ImportError as exc:
                raise RuntimeError("CUDA is required; torch is not installed") from exc
            cuda_available = bool(torch.cuda.is_available())
        if not cuda_available:
            raise RuntimeError("CUDA is required for production query embeddings; CPU fallback is disabled")
        embed_batch_fn = self._embedder_loader(model_revision=self._model_revision)
        return BgeM3QueryEmbedder(
            embed_batch_fn=embed_batch_fn,
            dimension=self._dimension,
            model_revision=self._model_revision,
        )


__all__ = [
    "BgeM3QueryEmbedder", "ProductionBgeM3QueryEmbeddingFactory",
    "QueryEmbedder", "QueryEmbeddingFactory",
]
