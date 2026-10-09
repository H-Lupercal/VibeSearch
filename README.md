# VibeSearch

Local-first search over gallery **metadata**. The CLI collects a small, bounded sample through an official JSON API, indexes dense text embeddings locally, and searches only the records already indexed. It does not download media files, mirror a site, or infer descriptions absent from the metadata. Source metadata may contain unedited titles and tags; use `--display id-only` to hide them in results.

See [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) for the data model, limits, safety boundaries, and acceptance tests.

## Requirements

- Python 3.11+; a GPU is optional.
- Network for collecting metadata and for the first model download. Indexing/search can run offline after the model is cached.
- Enough storage/memory for a local embedding model. `sentence-transformers` may install a large accelerator-enabled PyTorch package by default; on CPU-only machines, consider installing the CPU-only PyTorch wheel from the official PyTorch package index first.

## Install

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
```

Create a local `.env` from `.env.example` and set `VIBESEARCH_USER_AGENT_CONTACT` to a real operator contact address or project URL before collection. Leave `VIBESEARCH_API_KEY` unset for anonymous access, or set an API key from your own account. Never commit `.env`.

## Commands

```bash
vibesearch --help
vibesearch status
vibesearch collect --max-items 100 --max-pages 5
vibesearch index
vibesearch search 'lonely rainy night with soft quiet atmosphere' --top-k 20
vibesearch search 'warm cozy indoor setting' --display id-only
vibesearch reindex
```

Collection is paced below the documented endpoint limits and best-effort under shifting newest-first pagination. `index` does not call the API; `search` does not call the API. Results show cosine **distance** (lower is closer), not a probability. Changing model, revision, or text template requires a separate index. `status` summarizes local state without printing credentials.

## Validate

```bash
python -m pytest -q
```

Automated tests use synthetic neutral metadata and mocked HTTP, not live service requests. A small live smoke test may be run explicitly by an operator after setup. Do not run unattended bulk collection.
