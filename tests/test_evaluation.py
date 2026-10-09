"""Offline retrieval evaluation with synthetic, non-gallery identifiers."""

import json
import math

import pytest
from pydantic import ValidationError

from vibesearch.evaluation import EvaluationDataset, evaluate, load_dataset


@pytest.fixture
def payload():
    return {
        "candidate_ids": [101, 102, 103, 104],
        "model_revision": "local-test-revision",
        "template_version": "test-template",
        "sampling_method": "synthetic unit-test fixture",
        "queries": [
            {
                "query": "neutral query one",
                "positives": [
                    {"gallery_id": 101, "grade": 3},
                    {"gallery_id": 102, "grade": 1},
                ],
                "hard_negatives": [103],
            },
            {
                "query": "neutral query two",
                "positives": [{"gallery_id": 104, "grade": 2}],
            },
        ],
    }


def test_grades_positions_and_aggregates(payload):
    dataset = EvaluationDataset.model_validate(payload)
    results = {"neutral query one": [103, 102, 101], "neutral query two": [101, 102]}
    report = evaluate(dataset, lambda query: results[query])
    first, second = report.per_query
    ideal = 7 + 1 / math.log2(3)
    actual = 1 / math.log2(3) + 7 / math.log2(4)
    assert first.hit_rate_at_10 == 1.0
    assert first.ndcg_at_10 == pytest.approx(actual / ideal)
    assert first.hard_negatives_at_10 == (103,)
    assert second.hit_rate_at_10 == 0.0
    assert second.ndcg_at_10 == 0.0
    assert report.query_count == 2
    assert report.hit_rate_at_10 == 0.5
    assert report.ndcg_at_10 == pytest.approx(first.ndcg_at_10 / 2)


def test_cutoff_and_empty_results(payload):
    payload["candidate_ids"] += list(range(105, 117))
    dataset = EvaluationDataset.model_validate(payload)
    results = {"neutral query one": list(range(105, 115)) + [101], "neutral query two": []}
    report = evaluate(dataset, lambda query: results[query])
    assert all(row.hit_rate_at_10 == row.ndcg_at_10 == 0 for row in report.per_query)
    assert report.per_query[0].retrieved_ids_at_10 == tuple(range(105, 115))

@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(candidate_ids=[101, 101]),
        lambda p: p.update(candidate_ids=[101, 102, 103]),
        lambda p: p["queries"][0].update(positives=[]),
        lambda p: p["queries"][0].update(positives=[{"gallery_id": 101, "grade": 0}]),
        lambda p: p["queries"][0]["positives"].append({"gallery_id": 101, "grade": 2}),
        lambda p: p["queries"][0].update(hard_negatives=[101]),
        lambda p: p["queries"][0].update(hard_negatives=[103, 103]),
        lambda p: p["queries"][0].update(query="  "),
        lambda p: p["queries"].append(dict(p["queries"][0])),
        lambda p: p.update(model_revision=""),
        lambda p: p.update(unexpected="field"),
        lambda p: p["queries"][0].update(positives=[{"gallery_id": True, "grade": 3}]),
    ],
)
def test_invalid_dataset(payload, mutate):
    mutate(payload)
    with pytest.raises(ValidationError):
        EvaluationDataset.model_validate(payload)


def test_load_json(tmp_path, payload):
    path = tmp_path / "judgments.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_dataset(path).queries[0].query == "neutral query one"
    path.write_text("{invalid", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_dataset(path)


@pytest.mark.parametrize("ranking", [[101, 101], [999], [True], ["101"]])
def test_invalid_ranking(payload, ranking):
    dataset = EvaluationDataset.model_validate(payload)
    with pytest.raises(ValueError):
        evaluate(dataset, lambda query: ranking)
