"""Offline evaluation of ranked gallery IDs against curated relevance judgments.

This module does not query a service or an index. The caller supplies both the
judgments and a synchronous evaluator that returns ranked IDs for each query.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator

CUTOFF = 10


class Judgment(BaseModel):
    """A positive relevance judgment; unjudged and negative IDs have grade zero."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gallery_id: int = Field(strict=True, gt=0)
    grade: int = Field(strict=True, ge=1, le=3)


class EvaluationQuery(BaseModel):
    """One query and its judged positives and optional hard negatives."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str = Field(strict=True, min_length=1)
    positives: tuple[Judgment, ...] = Field(min_length=1)
    hard_negatives: tuple[int, ...] = ()

    @model_validator(mode="after")
    def validate_judgments(self) -> EvaluationQuery:
        if not self.query.strip():
            raise ValueError("query must not be blank")
        positive_ids = [item.gallery_id for item in self.positives]
        if len(set(positive_ids)) != len(positive_ids):
            raise ValueError("positive gallery IDs must be unique")
        negatives = self.hard_negatives
        if any(type(gallery_id) is not int or gallery_id <= 0 for gallery_id in negatives):
            raise ValueError("hard negative gallery IDs must be positive integers")
        if len(set(negatives)) != len(negatives):
            raise ValueError("hard negative gallery IDs must be unique")
        if set(positive_ids).intersection(negatives):
            raise ValueError("positive and hard negative gallery IDs must not overlap")
        return self


class EvaluationDataset(BaseModel):
    """Frozen candidate pool, contract provenance, and human judgments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_ids: tuple[int, ...] = Field(min_length=1)
    model_revision: str = Field(strict=True, min_length=1)
    template_version: str = Field(strict=True, min_length=1)
    sampling_method: str = Field(strict=True, min_length=1)
    queries: tuple[EvaluationQuery, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dataset(self) -> EvaluationDataset:
        pool = self.candidate_ids
        if any(type(gallery_id) is not int or gallery_id <= 0 for gallery_id in pool):
            raise ValueError("candidate gallery IDs must be positive integers")
        if len(set(pool)) != len(pool):
            raise ValueError("candidate gallery IDs must be unique")
        if any(not value.strip() for value in (
            self.model_revision, self.template_version, self.sampling_method
        )):
            raise ValueError("provenance fields must not be blank")
        if len({item.query for item in self.queries}) != len(self.queries):
            raise ValueError("queries must be unique")
        candidates = set(pool)
        for item in self.queries:
            judged = {positive.gallery_id for positive in item.positives}
            judged.update(item.hard_negatives)
            if not judged.issubset(candidates):
                raise ValueError("judged gallery IDs must belong to the candidate pool")
        return self


def load_dataset(path: str | Path) -> EvaluationDataset:
    """Read and validate a UTF-8 JSON judgment dataset."""
    return EvaluationDataset.model_validate_json(Path(path).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class QueryResult:
    query: str
    retrieved_ids_at_10: tuple[int, ...]
    hard_negatives_at_10: tuple[int, ...]
    hit_rate_at_10: float
    ndcg_at_10: float


@dataclass(frozen=True)
class EvaluationReport:
    query_count: int
    per_query: tuple[QueryResult, ...]
    hit_rate_at_10: float
    ndcg_at_10: float


def evaluate(
    dataset: EvaluationDataset, evaluator: Callable[[str], Iterable[int]],
) -> EvaluationReport:
    """Evaluate rankings at 10; scores are macro means across queries.

    DCG uses (2**grade - 1) / log2(rank + 1), with ranks starting at one.
    Unjudged IDs and hard negatives receive zero. The ideal ranking uses all
    known positive grades, truncated at ten. Returned IDs must be distinct
    members of the frozen candidate pool, including beyond the cutoff.
    """
    from math import log2

    candidates = set(dataset.candidate_ids)
    rows: list[QueryResult] = []
    for item in dataset.queries:
        ranking = tuple(evaluator(item.query))
        if any(type(gallery_id) is not int or gallery_id not in candidates for gallery_id in ranking):
            raise ValueError(f"ranking for {item.query!r} contains an invalid or out-of-pool ID")
        if len(set(ranking)) != len(ranking):
            raise ValueError(f"ranking for {item.query!r} contains duplicate IDs")
        top = ranking[:CUTOFF]
        grades = {positive.gallery_id: positive.grade for positive in item.positives}
        dcg = sum((2 ** grades.get(gallery_id, 0) - 1) / log2(rank + 1)
                  for rank, gallery_id in enumerate(top, start=1))
        ideal_grades = sorted(grades.values(), reverse=True)[:CUTOFF]
        ideal_dcg = sum((2 ** grade - 1) / log2(rank + 1)
                        for rank, grade in enumerate(ideal_grades, start=1))
        rows.append(QueryResult(
            query=item.query,
            retrieved_ids_at_10=top,
            hard_negatives_at_10=tuple(gallery_id for gallery_id in top
                                       if gallery_id in item.hard_negatives),
            hit_rate_at_10=float(any(gallery_id in grades for gallery_id in top)),
            ndcg_at_10=dcg / ideal_dcg,
        ))
    return EvaluationReport(
        query_count=len(rows),
        per_query=tuple(rows),
        hit_rate_at_10=sum(row.hit_rate_at_10 for row in rows) / len(rows),
        ndcg_at_10=sum(row.ndcg_at_10 for row in rows) / len(rows),
    )
