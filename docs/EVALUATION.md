# Offline retrieval evaluation

`vibesearch.evaluation` validates human relevance judgments and measures a supplied local ranker. It makes no network requests, loads no model, and does not provide a CLI command. Curate judgments only after indexing a pilot corpus; this repository does not provide curated gallery IDs or measured scores.

## Judgment file

Copy this **unfilled template** to a local JSON file. It is intentionally not a valid evaluation dataset until a candidate pool and at least one query with a positive judgment are supplied. Do not treat the empty arrays as evidence.

```json
{
  "candidate_ids": [],
  "model_revision": "",
  "template_version": "",
  "sampling_method": "",
  "queries": []
}
```

Each query object has this shape (field types, **not** example gallery IDs):

| Field | Type | Meaning |
| --- | --- | --- |
| `query` | nonblank string | Search text. |
| `positives` | nonempty array of `{ "gallery_id": integer, "grade": integer }` | Distinct IDs from the candidate pool; grades 1–3, with 3 most relevant. |
| `hard_negatives` | optional array of integers | Distinct IDs from the pool known to be irrelevant; cannot overlap positives. |

The top-level `candidate_ids` is a nonempty array of unique positive integer gallery IDs defining the frozen retrieval pool. `model_revision`, `template_version`, and `sampling_method` must be nonblank strings. Record how the pool was selected in `sampling_method`; keep the judgment file and index contract together for reproducibility. All judged IDs must be in the pool; each query string must be unique. Unknown fields, nonpositive IDs, duplicate judgments, and incompatible grades are rejected. Unjudged candidates have grade zero for scoring, which is not a claim that they are objectively irrelevant.

For a useful comparison, manually judge approximately 15–30 neutral queries with 3–5 positive IDs and a few hard negatives each from the indexed pilot corpus. Freeze the candidate pool before comparing model revisions or text templates. Inspect false positives and corpus coverage alongside the scores; a small judged sample does not establish broad semantic performance.

## Python usage

```python
from vibesearch.evaluation import evaluate, load_dataset

judgments = load_dataset("local-judgments.json")

# Supply an existing local search adapter. It must return distinct ranked IDs
# from judgments.candidate_ids, with no IDs outside that frozen pool.
def rank_ids(query: str):
    return local_search(query, candidate_ids=judgments.candidate_ids)

report = evaluate(judgments, rank_ids)
for row in report.per_query:
    print(row.query, row.hit_rate_at_10, row.ndcg_at_10,
          row.hard_negatives_at_10)
print(report.query_count, report.hit_rate_at_10, report.ndcg_at_10)
```

`local_search` above denotes a caller-provided adapter, **not** a function included by this module. The evaluator receives query text and returns an ordered iterable of integer gallery IDs; empty rankings are allowed. Rankings with duplicates, noninteger or out-of-pool IDs are rejected, including those beyond rank 10.

A query scores hit-rate@10 = 1 when any judged positive is within the first ten results, otherwise 0. For nDCG@10, DCG is the sum of `(2**grade - 1) / log2(rank + 1)` over positions 1–10; ideal DCG sorts the known positive grades descending and uses at most ten. Hard negatives and unjudged results score zero. Aggregate metrics are arithmetic means of the per-query metrics; the report also retains the top-ten IDs and retrieved hard negatives per query. There are no fabricated evaluation results in this document.
