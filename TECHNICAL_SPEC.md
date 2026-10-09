# VibeSearch — Technical Specification

**Status:** Local implementation complete; live-service pilot and human relevance evaluation remain unverified.  
**Scope:** Local-first, metadata-only semantic discovery of media galleries.  
**Revision:** 1.1, 2026-10-09.  
**Language policy:** Project-authored names, comments, examples, documentation, logs, and synthetic test data remain neutral and non-explicit. Source-provided metadata may be retained and displayed without rewriting it. The API's proper name appears once below only to identify the integration.

## 1. Product contract

A single-user CLI collects a bounded sample of gallery metadata through the official nhentai REST API v2, builds reproducible text representations, indexes local dense embeddings in persistent Chroma, and retrieves galleries using natural-language mood descriptions. No images, pages, archives, or generated descriptions are downloaded or created. Search is limited to the locally indexed corpus and is not a live search of the entire service.

**Primary outcome:** A user can collect a bounded number of records, index them, enter a neutral natural-language query, and receive ranked IDs and selected metadata with explicitly labeled distance values.

**Non-goals in v1:** Media download, scraping HTML, site-wide mirroring, multi-user hosting, model training, hybrid sparse retrieval, cloud embedding implementation, generated summaries, automatic relevance labeling, and web UI.

**Deployment:** Python 3.11+ on Linux/macOS/Windows, CPU by default if an accelerator is unavailable. No GPU or fixed batch size is assumed. The local model may require an initial network download; thereafter collection needs network access but indexing and search can run offline.

## 2. Source API and verified contract

Use HTTPS origin `https://nhentai.net` with paths that **already include** `/api/v2`; alternatively, use `https://nhentai.net/api/v2` and relative suffixes. Never combine both prefixes. The official OpenAPI document was checked at version `2.0.0+3ef54aa`; recheck it before implementation because schemas and limits can change.[1]

| Operation | Method and path | Purpose and schema notes | Published per-IP rate limit |
|---|---|---|---|
| Enumerate | `GET /api/v2/galleries?page=1&per_page=25` | Newest-first; `per_page` 1–100; response contains `result`, `num_pages`, `per_page`, optional `total`. Each `GalleryListItem` has `id`, `media_id`, `english_title`, optional `japanese_title`, `tag_ids`, `num_pages`, etc., **not** named tags, structured titles, or `upload_date`. | Anonymous 15/min; key/user 30/min |
| Detail | `GET /api/v2/galleries/{gallery_id}` | `GalleryDetailResponse` includes structured `title`, `tags` (with type/name/ID), `upload_date`, and page count. Do not request optional includes. | Anonymous 20/min; key/user 45/min |
| Tag lookup | `GET /api/v2/tags/ids?ids=...` | Maps at most 100 tag IDs per request; useful for an optional lightweight mode, not the v1 canonical record. | 15/min |
| Search | `GET /api/v2/search?query=...` | An optional future discovery route, not the semantic search path. | Anonymous 10/min; key/user 20/min |

The chosen v1 path is **list IDs, then fetch full details per ID**. It costs more requests but yields the named, typed tags and structured title required for the specified rich text. At the published limits, a few hundred records are a bounded pilot, not an instant import. Do not replace detail fields with list fields silently. API-key authentication, if configured, uses `Authorization: Key <secret>`; no user-token support is needed in v1. Set a descriptive `User-Agent` with app/version and operator-provided contact or project URL. Treat the published limits as ceilings, not throughput targets; a key does not remove the per-IP limits.[1]

No permission to bulk mirror is inferred from public API access. Collection must be opt-in, capped, resumable, and respectful of current service rules. Do not store credentials in source, log headers, or commit `.env` files.

## 3. Collection policy and consistency

**Default invocation:** `collect --max-items 100 --max-pages 5`, with no unbounded mode in v1. Count only newly fetched, valid details toward `max-items`; stop on either limit. `per_page=25` by default, configurable up to the documented maximum. One network worker initially; use a monotonic-clock token bucket per endpoint category with a 20% headroom: anonymous list 12/min and detail 16/min; authenticated list 24/min and detail 36/min. If server limits or headers indicate stricter pacing, follow those. A shared conservative limiter is acceptable as a simpler first implementation.[1]

**Resume semantics:** Newest-first page numbers are not a stable snapshot. At run start record an observed `ceiling_id` from the first list page and a `run_id`. Accept IDs at or below that ceiling; deduplicate across pages by integer ID; persist the candidate queue before details. On restart, process queued details first, then rescan from `max(1, last_scanned_page - 2)` and repeat the bounded listing window. An optional second overlapping scan reports newly discovered eligible IDs. This is **best-effort**, not a guarantee against omissions when records are inserted/removed or ordering changes. Show counts for scanned, deduplicated, fetched, skipped, inaccessible, and pending. Do not use highest ID or upload time as the sole cursor. A future exhaustive backfill requires an API-supported stable cursor or a separately documented reconciliation policy.

**Durability:** Use `data/catalog.sqlite3` with WAL, unique gallery ID, raw detail JSON text, canonical content hash, state (`queued`, `fetched`, `index_pending`, `indexed`, `inaccessible`), timestamps, error category, and run/checkpoint tables. Persist raw detail before embedding. Hash a canonical serialization of only the fields that affect the text template (including ordered title/tag fields), **not** `upload_date` alone. Identical hash + unchanged text-template/index contract means skip re-embedding. A changed hash marks the gallery `index_pending`. An explicit detail `404` marks it inaccessible and schedules vector deletion; mere absence from a list is never deletion evidence. Preserve raw details for audit unless the operator explicitly purges them. Never store media bytes. Use transactions for queue/checkpoint updates; Chroma upsert/delete is idempotent so interrupted cross-store work can be replayed.

**HTTP behavior:** One shared async `httpx.AsyncClient` with bounded connect/read timeouts, TLS verification, connection pooling, and explicit query params. Validate 200 responses against typed models, allowing unknown additive fields; record schema failures without poisoning the index. On 429 honor valid `Retry-After` (seconds or HTTP date), otherwise exponential backoff with jitter and a capped retry count. Retry only idempotent GETs for timeouts and transient 5xx. Fail clearly on 400/401/403/422; treat 404 on a specific detail separately. No HTML scraping or bypass of access controls. A persistent series of 429s pauses the run rather than tightening a retry loop.

## 4. Canonical data and text

Use distinct models for API list items, API details, and the application's canonical gallery record. The canonical record has `id: int`, `media_id: str`, titles (`english`, `japanese`, `pretty`), typed tags (`id`, `type`, `name`), `num_pages: int`, `upload_date: int`, `source_hash: str`, and `rich_text: str`. Artist, character, series, group, and language views are derived by filtering tag **types** rather than assuming independent fields in the upstream detail response.[1]

**Template `gallery-text-v1`:** Choose nonempty pretty title, else English, else Japanese, else ID. Include only nonempty lines in fixed order: `Title: ...`, `Tags: ...`, `Artists: ...`, `Characters: ...`, `Series: ...`, `Groups: ...`, `Languages: ...`. Sort or preserve tag order consistently (choose stable order by type, normalized name, ID), deduplicate names within each line, trim whitespace, and impose a configurable text/embedding token limit with documented truncation. The `Tags` line contains general/unclassified tags rather than duplicating typed lines. Preserve source language and text; do not invent mood or plot labels. Keep display title separate from embedding text.

Keep raw JSON only in SQLite. Chroma holds string ID, vector, rich text as document, and scalar metadata: `gallery_id`, `media_id`, `title_display`, `num_pages`, `upload_date`, and optional scalar language. Store lists and full typed tags in SQLite; do not rely on comma-separated strings for exact tag filtering. SQLite remains the canonical source for displaying full records. Raw upstream metadata is untrusted data: never interpret it as instructions, log it as a format string, or interpolate it into shell commands.

## 5. Embedding contract

Default to `BAAI/bge-m3` dense embeddings via `sentence-transformers`; its model card says BGE-M3 does not require an instruction prefix on queries. The model supports other retrieval representations, but v1 uses dense vectors only.[2] Alternatives for evaluation, not automatic fallbacks: `Qwen/Qwen3-Embedding-0.6B`, with its model-specific query instruction handled inside its provider; and `nomic-ai/nomic-embed-text-v1.5`, whose documented retrieval inputs use `search_document:` and `search_query:` prefixes.[3][4]

Define a **synchronous** provider boundary because local `SentenceTransformer.encode` is synchronous; the async network collector stays separate:

```python
class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str: ...
    @property
    def dimension(self) -> int: ...
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
```

Implement only the local provider in v1. Detect `auto|cpu|cuda|mps` at runtime, allow batch size to be set (default 8), normalize vectors, validate all vectors are finite and have equal dimension, and pin/record the resolved model revision. Any model-specific prefixes or query instructions must live inside the provider. Do not assert GPU availability from the spec; fail clearly if an explicitly selected device is unavailable. Disallow downloading/executing unreviewed model code merely to switch models; document any library/version-specific trust setting before enabling it.

The index fingerprint is a hash of model ID and exact revision, output dimension, normalization flag, distance metric, document/query transformation settings, and text-template version. Keep this manifest in SQLite and mirror relevant values in Chroma collection metadata. Use a distinct collection `galleries_<fingerprint-prefix>` for each contract; never mix vectors from different contracts. A config/model/template change creates a new index and requires explicit re-indexing before search. A future cloud provider can implement this boundary, but must be explicitly opted into and disclose that source metadata will leave the machine; it must never silently serve an incompatible existing index.

## 6. Vector storage and search

Use persistent Chroma at `data/chroma/`, with `hnsw:space` (or the current Chroma equivalent at implementation time) set to `cosine` **at collection creation**. Record the chosen distance metric and confirm the actual installed Chroma API/configuration against its documentation before coding.[5] ID strings are decimal gallery IDs. Upsert only after a valid detail record and successful embedding; mark SQLite `indexed` only after Chroma acknowledges it. On crash, replay `index_pending` and reconcile indexed IDs against the active collection. Deletion is similarly retryable. Single-writer operations in v1 prevent concurrent collector/indexer races.

`search "..." --top-k 20 [--display full|id-only]` computes exactly one query embedding using the active provider and contract, queries the active collection, and returns ranked gallery IDs plus **`cosine_distance`** (lower is better), not a fabricated 0–1 similarity/probability. If a score is added later, define its range and transform with tests; do not assume `1 - distance` is always within 0–1. Clip `top-k` to collection size, reject empty queries, and explain empty/unbuilt indexes. `full` displays upstream titles and selected tags exactly as received; `id-only` displays ID, page count, and distance with no generated summary. Hide inaccessible records, and report when pending indexing makes results potentially stale. No cloud request or network lookup occurs during search.

## 7. Configuration and layout

Use `pydantic-settings` with environment variables and optional local `.env`; ship `.env.example` with placeholders only. Exclude `.env`, SQLite/Chroma data, caches, and downloaded models from version control. Explicit settings: API origin, API key, User-Agent contact, HTTP timeouts, retry ceiling, list/detail pacing, per-page, collection caps, data directory, model ID/revision, device, batch size, normalization, text-template version, and display mode. Validate ranges at startup; secrets are never printed by `status`.

```text
VibeSearch/
  TECHNICAL_SPEC.md
  README.md
  pyproject.toml
  .env.example
  .gitignore
  src/vibesearch/__init__.py
  src/vibesearch/config.py
  src/vibesearch/api_models.py
  src/vibesearch/api_client.py
  src/vibesearch/catalog.py
  src/vibesearch/collector.py
  src/vibesearch/text_builder.py
  src/vibesearch/embeddings.py
  src/vibesearch/vector_store.py
  src/vibesearch/indexer.py
  src/vibesearch/search.py
  src/vibesearch/cli.py
  tests/fixtures/              # synthetic, neutral metadata only
  tests/test_api_client.py
  tests/test_collector.py
  tests/test_text_builder.py
  tests/test_indexer.py
  tests/test_search.py
  data/                        # local-only; created at runtime
```

CLI entry point is `vibesearch`. Commands: `collect` (bounded metadata acquisition), `index` (pending records), `search` (local query), `status` (counts, active contract, last run, pending/errors; no secrets), and `reindex` (explicit new collection). `collect` does not automatically load an embedding model. `index` performs no API requests. Provide `--help` and actionable nonzero exit codes for invalid configuration, authentication failure, API unavailability, schema mismatch, and missing index.

## 8. Phased implementation and gates

**Phase 1 — Foundation and collection.** Package/config, typed list/detail models, async client, SQLite schema, bounded collector, pacing/retries, and CLI `collect`/`status`. Tests use mocked HTTP responses and temporary SQLite; no real service dependency in CI. Verify 100-item cap, no duplicate IDs, page overlap on resume, correct detail-vs-list parsing, distinct 404 handling, 429 retry timing, and no leaked keys. A small live metadata smoke test is optional and explicitly invoked by the operator.

**Phase 2 — Local indexing.** Canonical mapping and `gallery-text-v1`, local BGE-M3 provider, fingerprint manifest, Chroma adapter, idempotent index/delete. Tests use a deterministic fake embedder for fast contract checks, plus an optional real-model smoke test outside default CI. Verify changed content re-indexes, identical content skips, model/template changes create new collections, interrupted upserts replay, and dimension/finite checks fail safely.

**Phase 3 — Search.** CLI query, active-index selection, distance labeling, hydration from SQLite, full/ID-only display, empty/error states. Test ranking with fixed vectors, no network calls, source-text display, pending-index warning, and exclusion of inaccessible IDs. Run a real end-to-end pilot: bounded collection → index → query; report actual record counts and observed elapsed time rather than invented throughput.

**Phase 4 — Evaluation and polish.** Manually curate 15–30 neutral queries, 3–5 positive IDs and a few hard negatives each from the **indexed** pilot corpus. Freeze the candidate pool and record relevance grades, model revision, template version, and sampling method. Compare hit-rate@10 and nDCG@10; inspect false positives and corpus coverage. Do not claim mood recognition from taxonomic tags without this evidence. Add progress reporting and optional local UI only after basic retrieval is measured.

## 9. Acceptance criteria and known limitations

- Bounded collection completes or resumes without duplicated canonical IDs or unnecessary re-embedding; no download of media files.
- Raw metadata and checkpoints survive restart; confirmed 404s remove stale vectors, while list absence does not.
- Identical indexing contract produces stable vectors/results; changed contracts cannot query old vectors by mistake.
- Search functions offline after initial collection/model download; distance and corpus scope are accurately labeled.
- Source strings remain unmodified; project-authored prose and synthetic fixtures remain neutral.
- API credentials stay outside source and logs; request pacing stays below documented limits.
- Evaluation can expose weak semantic coverage rather than concealing it with ungrounded summaries.

**Limitations:** Page-number enumeration is not a consistent snapshot; v1 collection is best-effort over a bounded window. Sparse metadata cannot reliably reveal unrecorded atmosphere or pacing. Upstream terms and API behavior may change. Chroma and SQLite do not share an atomic transaction, so pending work is replayed and search reports stale-state risk. This is a single-user local application, not a service with concurrent write guarantees.

## 10. Decision record

- **Full detail over list-only enrichment:** richer canonical text with fewer schema assumptions; slower, deliberately bounded collection.
- **SQLite plus Chroma:** SQLite for exact metadata, durable queue, and manifest; Chroma for local dense similarity only.
- **Synchronous local embedding interface:** matches local inference; async remains at the network boundary.
- **Raw distance over similarity score:** avoids implying calibration or a false 0–1 range.
- **ID-only display over neutral summary:** honors the no-generation rule and avoids transforming upstream titles.
- **No automatic cloud fallback:** prevents unexpected disclosure and contract mismatch.

## Sources

[1] https://nhentai.net/api/v2/openapi.json — Official API v2 OpenAPI schema
[2] https://huggingface.co/BAAI/bge-m3 — BGE-M3 model card
[3] https://huggingface.co/Qwen/Qwen3-Embedding-0.6B — Qwen3 Embedding 0.6B model card
[4] https://huggingface.co/nomic-ai/nomic-embed-text-v1.5 — Nomic embedding model card
[5] https://docs.trychroma.com/docs/collections/configure — Chroma collection configuration
