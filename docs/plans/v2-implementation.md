# VibeSearch v2.0 Implementation Plan

> **For Hermes:** Use subagent-driven-development discipline with disjoint ownership and parent integration.

**Goal:** Implement and test phases A-C of docs/TECHNICAL_SPEC_V2.md in the existing project. D/E remain explicitly deferred.

**Architecture:** Preserve SQLite/Chroma and existing CLI. Add schema projections/exact filters, eligible-set ranking, single-worker FastAPI UI and verified CDN reader/cache. Parent owns core integration; independent inherited GPT-6 workers own web and reader modules.

**Tech Stack:** Python, SQLite, Chroma, sentence-transformers, FastAPI, HTTPX, Pillow, pytest.

## Tasks and ownership

1. Parent: write failing migration/filter tests in tests/test_filters.py and implement transactional schema projections in catalog.py and FilterService in filter.py. Preserve raw data, queued/inaccessible states and manifests. Run `.venv/bin/python -m pytest tests/test_filters.py -q`.
2. Reader worker: write failing reader/cache tests then implement reader.py, image_cache.py, refresh.py if needed. Approved hosts i1/i2 only, real decoding, per-hop URL validation, stream caps/deadline, TTL/reference-safe cache. Own tests/test_reader.py and tests/test_image_cache.py only. Do not touch catalog/config/CLI/web. Run scoped tests.
3. Web worker: write failing ASGI/security tests then implement web/ and tests/test_web.py. Integrate documented parent/reader interfaces; one bounded inference queue, exact Host/origin/CSRF, safe UI and ID-only JSON/DOM. Do not touch core/reader/config/manifest. Run scoped tests when dependencies are ready.
4. Parent: modify search.py and tests/test_search.py for full eligible-set ranking/counts and no encoding for empty intersection. Implement common locking.py, CLI serve/filter/suggest/refresh helpers, settings, dependency/test isolation and revision compatibility. Test each changed behavior before integration.
5. Parent: install declared dependencies in existing .venv, run whole tests, compile checks and diff checks. Independently review worker code, exercise real local index and ASGI/server routes. Browser verification uses isolated/headless tools, never the user's live Linux display.
6. Parent: fix defects and rerun checks. Update README/.env.example with launch commands and D/E status. Preserve operator .env and existing data; back up local SQLite before production migration. No git commits, root actions, unbounded collection or translation downloads.

## Cross-module interfaces

- FilterService(catalog): eligible_ids(filters: dict|None) -> set[int]; list(filters=None, limit=50, offset=0, display='full') -> dict with items,total; suggest(field,prefix,limit=20) -> list of {id,type,name,count}.
- SearchService.search(query,top_k=20,display='full',eligible_ids=None): retains original fields and adds eligible_total/eligible_indexed/eligible_not_indexed. Worker may wrap dataclasses.asdict.
- Reader module: ReaderError with safe message/status_code; reader_bundle(raw, display='full') -> dict; PageCache(root,max_bytes=512*1024*1024,ttl_seconds=86400) exposes async get_page(gallery_id,raw,page_number) -> object with data:bytes,content_type:str and async aclose(). Constructor test injection permitted. No catalog ownership in cache.
- Settings additions supplied by parent: web_host,web_port,allow_non_loopback,embed_queue_size,embed_queue_timeout_s,image_cache_max_mb,image_cache_ttl_hours; translators fixed off by default. Web settings _env_file injection supported.
- Common data_lock(data_dir) for all CLI operations and serve lifetime; worker create_app(settings=None,provider=None,page_cache=None) injectable for tests, app lifespan gets lock and runs migration. Parent may supply lock module before integration.

## Verification gates

Required evidence: scoped failing/passing tests; full regression suite without command-local environment hacks; declared package install; real offline query/filter and server HTTP smoke; reader bounded live fetch/cache hit; security tests; documented limitations. Existing source probe already passed, not equivalent to reader acceptance. Translation is not part of v2.0 acceptance.
