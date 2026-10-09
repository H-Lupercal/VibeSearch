# VibeSearch v2.0 implementation verification

## Delivered scope

Phases A-C are implemented: local normalized filtering/autocomplete, eligible-set semantic ranking, a localhost web interface, validated reader descriptors, and a bounded temporary image cache. Existing collection, indexing, status, evaluation and search commands remain available. Phases D/E (translation) remain deferred and disabled, as specified.

## Automated checks

Executed in the project virtual environment:

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest -q` | 172 passed, 88 existing Chroma deprecation warnings |
| `.venv/bin/ruff check src tests` | Passed |
| `.venv/bin/python -m compileall -q src` | Passed |
| `node --check src/vibesearch/web/static/app.js` | Passed |
| `git diff --check` | Passed |
| `uv build --wheel` | Built v0.2.0 wheel; templates/static assets and reviewed fixes verified in archive |

Tests use synthetic metadata, mocked HTTP, temporary SQLite/Chroma stores and fake embedding providers. Operator settings are isolated. Test results do not imply universal upstream compatibility.

## Review regressions

The independent review findings were reproduced by the parent before patching: all nine initially added regression cases failed. The final `tests/test_review_regressions.py` includes ten passing cases, covering:

- Repeated request cancellation and concurrent cancelled cache shutdown retain physical decoder ownership and fetch slots until the thread finishes.
- Aborted publication does not restore lookup references to already-deleted blobs. Committed reference eviction governs filesystem sweeping; reclamation occurs before new blob publication.
- Shared-tag backfill follows detail-observation order, including a newer rename in a lower-ID gallery. Later indexing does not count as another detail observation.
- Projection and reader use the same descriptor validator. Safe supplied paths are preserved, not reconstructed from expected filenames.
- Oversized tag IDs are unknown filter identities; oversized route IDs/pages return controlled 422 errors.
- Out-of-range numeric metadata is isolated during historical backfill, preserving raw metadata, access state and indexed hashes.
- Repeated cancelled inference shutdown retains the physical worker until it completes.

## Real catalog and server checks

- Migrated the existing catalog to internal schema version 3. Compared all 100 rows' IDs, raw JSON, states, content hashes and indexed hashes before/after: unchanged. The pre-v2 SQLite backup is under `data/backups/`.
- Confirmed 100 eligible records and 12 English-language eligible indexed records.
- Actual offline semantic search with an English constraint returned two hits from those 12 records.
- Real localhost HTTP tests passed for home, CSS/JS, browse/filter, semantic search and reader. Wrong Host/port and unapproved mutation Origin were rejected; limits and oversized integer routes produced controlled errors.
- A real on-demand page fetch decoded as WebP, 1063 x 1500, 511,988 bytes. A repeat request had the same SHA-256 and was served from cache. Browser reader navigation loaded the next page. No gallery-wide download was performed.
- Isolated headless gstack browser tests exercised browse, English filtering, semantic search and page navigation. ID-only search rendered no source-title nodes. Cosine distances now appear with lower-is-closer labels. At 375px viewport width the page had no horizontal overflow; console reported no errors.
- `refresh-reader-metadata --max-items 1` completed with zero attempts because the real catalog already had readable descriptors. Mocked regression tests cover actual refresh attempts, failures and resume caps.

The final server was started with `HF_HUB_OFFLINE=1 .venv/bin/vibesearch serve` and verified at http://127.0.0.1:8000. Stop the server before other CLI operations against the same data directory.

## Limits and maintenance

- Historical schemas did not record a separate detail-observation timestamp. Migration uses their existing `updated_at` as the best available fallback with gallery ID as a deterministic tie-breaker. It cannot reconstruct observation history that was never stored. New detail saves record `detail_observed_at` independently of indexing.
- DNS pinning requires private HTTPX/httpcore network-backend hooks. The implementation constrains HTTPX to 0.28.x and pins httpcore 1.0.9; dependency upgrades require rerunning connection-pinning tests and a bounded live fetch.
- The live sample establishes tested compatibility, not a guarantee for every gallery/page or future CDN behavior. Source failures remain controlled, and only the verified i1/i2 hosts are allowed.
- ID-only is display minimization, not authentication. Autocomplete explicitly exposes requested local tag names, and opening the reader fetches source image content.
- No translation backend, public-hosting support or permanent image archive is delivered.
