"""Single RRF implementation shared by dense and lexical retrieval."""

from __future__ import annotations

from typing import Iterable

from .result import RetrievalResult, RetrievalResultSet


def compute_rrf(*, lexical_rank: int | None, dense_rank: int | None, k: int = 60) -> float:
    if k <= 0:
        raise ValueError("k must be positive")
    score = 0.0
    if lexical_rank is not None:
        score += 1.0 / (k + lexical_rank)
    if dense_rank is not None:
        score += 1.0 / (k + dense_rank)
    return score


def _compute_rrf_ranks(ranks: Iterable[int], *, k: int) -> float:
    return reciprocal_rank_score(ranks, k=k)


def reciprocal_rank_score(ranks: Iterable[int], *, k: int = 60) -> float:
    if k <= 0:
        raise ValueError("k must be positive")
    return sum(1.0 / (k + rank) for rank in ranks)


def fuse(
    *result_lists: Iterable[RetrievalResult],
    k: int = 60,
    output_k: int = 20,
) -> list[RetrievalResult]:
    if output_k < 1 or k <= 0:
        raise ValueError("k and output_k must be positive")
    if not result_lists:
        return []
    records: dict[str, dict[str, object]] = {}
    arm_names = ["lexical", "dense"] + [f"arm_{index}" for index in range(3, len(result_lists) + 1)]
    baseline_filters = None
    baseline_corpus_manifest_id = None
    baseline_index_manifest_id = None
    baseline_index_set = False
    for arm_name, values in zip(arm_names, result_lists):
        for position, item in enumerate(values, start=1):
            if baseline_filters is None:
                baseline_filters = item.filters_applied
                baseline_corpus_manifest_id = item.corpus_manifest_id
            elif item.filters_applied != baseline_filters or item.corpus_manifest_id != baseline_corpus_manifest_id:
                raise ValueError("RRF arms must use identical corpus and retrieval filters")
            if not baseline_index_set:
                baseline_index_manifest_id = item.index_manifest_id
                baseline_index_set = True
            elif item.index_manifest_id != baseline_index_manifest_id:
                raise ValueError("RRF arms must use one index manifest identity")
            named_rank = getattr(item, f"{arm_name}_rank", None)
            rank = named_rank if isinstance(named_rank, int) and named_rank >= 1 else position
            chunk_id = item.chunk_id
            record = records.setdefault(
                chunk_id,
                {
                    "item": item, "ranks": [], "lexical_rank": None,
                    "dense_rank": None, "lexical_raw_score": None, "dense_score": None,
                    "arm_ranks": {}, "arm_scores": {}, "index_manifest_id": None,
                },
            )
            record["ranks"].append(rank)  # type: ignore[union-attr]
            arm_ranks = record["arm_ranks"]  # type: ignore[assignment]
            arm_scores = record["arm_scores"]  # type: ignore[assignment]
            arm_ranks[arm_name] = min(arm_ranks.get(arm_name, rank), rank)
            if arm_name == "lexical":
                arm_scores[arm_name] = item.lexical_raw_score
            elif arm_name == "dense":
                arm_scores[arm_name] = item.dense_score
            else:
                arm_scores[arm_name] = item.fusion_score
            record["index_manifest_id"] = baseline_index_manifest_id
            if arm_name in {"lexical", "dense"}:
                existing = record[f"{arm_name}_rank"]
                record[f"{arm_name}_rank"] = rank if existing is None else min(existing, rank)
                score_field = "lexical_raw_score" if arm_name == "lexical" else "dense_score"
                score = getattr(item, score_field)
                if score is not None:
                    record[score_field] = score
    ordered = sorted(
        records.values(),
        key=lambda record: (
            -_compute_rrf_ranks(record["ranks"], k=k),
            min(record["ranks"]),
            record["item"].chunk_id,
        ),
    )
    output: list[RetrievalResult] = []
    for fusion_rank, record in enumerate(ordered[:output_k], start=1):
        score = _compute_rrf_ranks(record["ranks"], k=k)
        output.append(
            record["item"].model_copy(  # type: ignore[union-attr]
                update={
                    "lexical_rank": record["lexical_rank"],
                    "dense_rank": record["dense_rank"],
                    "lexical_raw_score": record["lexical_raw_score"],
                    "dense_score": record["dense_score"],
                    "fusion_score": score,
                    "fusion_rank": fusion_rank,
                    "arm_ranks": tuple(sorted(record["arm_ranks"].items())),  # type: ignore[union-attr]
                    "arm_scores": tuple(sorted(record["arm_scores"].items())),  # type: ignore[union-attr]
                    "index_manifest_id": record["index_manifest_id"],
                    "mode_requested": "hybrid",
                    "mode_served": "hybrid",
                    "is_degraded": False,
                    "fallback_reason": None,
                }
            )
        )
    return output


def fuse_result_sets(lexical: RetrievalResultSet, dense: RetrievalResultSet, *, k: int = 60, output_k: int = 20) -> RetrievalResultSet:
    values = fuse(lexical.results, dense.results, k=k, output_k=output_k)
    return RetrievalResultSet(
        candidates=tuple(values), results=tuple(values), candidate_count=len(values),
        mode_requested="hybrid", mode_served="hybrid", is_degraded=False,
        fallback_reason=None,
    )


__all__ = ["compute_rrf", "fuse", "fuse_result_sets", "reciprocal_rank_score"]
