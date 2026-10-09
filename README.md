# VibeSearch

Local-first search over gallery **metadata**, with exact structured filters and a local web reader. The CLI collects a bounded sample through an official JSON API and indexes dense text embeddings locally. Search covers the local corpus only. The reader fetches individual page images on demand into a bounded temporary cache, not a permanent archive. Source metadata may contain unedited titles and tags; use `--display id-only` to omit them from list/search results.

See [the consolidated v2 contract](docs/TECHNICAL_SPEC_V2.md), [reader source verification](docs/READER_VERIFICATION.md), and [implementation verification results](docs/V2_VERIFICATION.md). [TECHNICAL_SPEC.md](TECHNICAL_SPEC.md) preserves the v1 baseline. Title/page translation milestones are deferred and disabled in v2.0.

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
vibesearch filter --filters '{"language":["english"],"tags_any":["quiet"]}' --display id-only
vibesearch suggest artist 'a'
vibesearch search 'quiet rainy night' --filters '{"language":["english"]}' --display id-only
vibesearch serve
```

Collection is paced below the documented endpoint limits and best-effort under shifting newest-first pagination. `index` does not call the API; `search` does not call the API. Results show cosine **distance** (lower is closer), not a probability. Changing model, revision, or text template requires a separate index. `status` summarizes local state without printing credentials.

## Validate

```bash
python -m pytest -q
python -m compileall -q src
ruff check src tests
```

Automated tests use synthetic neutral metadata and mocked HTTP, not live service requests. A small live smoke test may be run explicitly by an operator after setup. Do not run unattended bulk collection.

## Local web UI and reader

With the environment activated, run `vibesearch serve`, then open **http://127.0.0.1:8000**. Stop it with Ctrl+C before running collection/indexing or other CLI operations against the same data directory. The process lock rejects conflicting commands rather than allowing SQLite/Chroma races. Port can be changed with `vibesearch serve --port 8001`; external hosting is unsupported and requires explicit opt-in.

Browse/filter/autocomplete need no model or network. Semantic search uses a cached compatible model/index, defaults to CPU in the web interface, and uses a bounded single-inference queue. Build the index with `vibesearch index` before serving. If the first model download is needed, indexing is the explicit setup step. `VIBESEARCH_EMBEDDING_REVISION` can pin a model commit; unpinned models resolve their immutable revision from the cached model configuration.

Reader descriptors are projected from existing raw details during migration. Missing/invalid descriptors do not hide otherwise valid metadata from search. To refresh such records explicitly, stop the server and run `vibesearch refresh-reader-metadata --max-items 50`. The hard attempt limit is 100, including failures, and interrupted runs retain their original queue/cap.

Only verified `i1`/`i2` image hosts are used, with no metadata API credentials forwarded. Page images are validated/decoded, animation is rejected, downloads are capped at 15 MiB with a total deadline, and the cache expires after 24 hours with a 512 MiB default disk cap. No thumbnails or automatic whole-gallery download are required. The source may change or reject requests; the UI surfaces controlled per-page failures rather than bypassing restrictions.

Exact filters use AND across fields, OR within normal selections, and `tags_all` for all required tags. IDs may be JSON integers; names are case-folded exact matches within the field type. `exclude_tags`/`exclude_artists` exclude any selected value. Empty lists impose no constraint. Results omit inaccessible and metadata-invalid records. Autocomplete is local-only.

Existing databases migrate transactionally, preserving raw metadata and embedding manifests. Back up valuable data before an upgrade; the implementation verification on this machine saved a pre-v2 SQLite backup under `data/backups/`. Cache files and data remain outside version control. ID-only mode omits titles/tags from list/search JSON and DOM; it is not authentication or a guarantee that source content is private when using the reader.
