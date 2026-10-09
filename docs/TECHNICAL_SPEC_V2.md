# VibeSearch v2: Consolidated implementation contract

Status: Design contract for implementation, not a declaration of implemented or verified behavior.

This document consolidates Technical Specification 2.0 and Amendments A, B, and C supplied by the operator. It resolves their conflicting clauses. For v2 features, this document takes precedence over those drafts. The existing root TECHNICAL_SPEC.md remains the v1 baseline; its collection, embedding-fingerprint, source-preservation, and replay guarantees remain in force except where explicitly extended here.

## 1. Scope and delivery gates

v2.0 delivers local structured filtering/autocomplete, a single-user local web UI, combined semantic/filter search, a reader, and bounded temporary image caching. The CLI remains supported. No full-site mirror, permanent archive, custom OCR pipeline, generated summaries, public deployment, or automatic cloud fallback is introduced.

Phases:
- A: schema migration, normalized tags, filter/suggest, regression tests.
- B: local web browse/search, combined ranking, security and inference limits.
- C: verified reader descriptors, bounded refresh, safe image fetch/cache.
- D: optional title translation with one selected and pinned backend.
- E: optional page translation with exactly one selected and pinned backend and durable jobs.

A and B may begin without reader/CDN verification. The bounded Phase C source-compatibility gate has passed, as recorded in [READER_VERIFICATION.md](READER_VERIFICATION.md); C may begin implementation using section 8's verified adapter policy. D and E require their respective backend pin documents and operator opt-in. Requirements specified here still need implementation and tests; specification readiness is not release readiness.

Project-authored names, logs, comments, and fixtures are neutral. Source text is stored unchanged and rendered as untrusted text.

## 2. Data validity and projection schema

Keep raw source details in galleries.raw_json. Preserve existing IDs, checkpoints, index manifests, and source-access states. Add nullable upload_date and num_pages, readable INTEGER NOT NULL DEFAULT 0, and validation_error TEXT NULL. Add validated pages_json when the reader contract is verified. A queued row may have no upload date or details.

Normalized tag tables:

```sql
CREATE TABLE tags (
    tag_id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL
);
CREATE INDEX tags_lookup ON tags(type, name_norm);
CREATE TABLE gallery_tags (
    gallery_id INTEGER NOT NULL REFERENCES galleries(id),
    tag_id INTEGER NOT NULL REFERENCES tags(tag_id),
    PRIMARY KEY(gallery_id, tag_id)
);
CREATE INDEX gallery_tags_by_tag ON gallery_tags(tag_id, gallery_id);
```

Upstream tag ID is identity. Duplicate normalized names with different IDs remain distinct rows. Normalize lookup names with Unicode NFC followed by casefold, without rewriting display names. Tag types are validated against the supported field mapping. Unknown types may be preserved but do not become general tags implicitly. Renames update the row by ID; the latest successfully committed detail observation wins. Changing normalization requires a schema-versioned rebuild.

Browse/filter/autocomplete eligibility is: raw detail exists, metadata projection validates, upload_date is non-null, and state is neither queued nor inaccessible. readable controls reader eligibility only. Semantic search uses the same metadata eligibility and reports missing compatible vectors.

Only a confirmed detail 404 sets inaccessible and schedules vector deletion. Local malformed metadata preserves raw data and prior source-access state, sets a controlled validation_error, and excludes the row from browse/filter/autocomplete and v2 semantic results. Invalid/missing page descriptors alone set readable=false with a separate reader error; valid metadata remains searchable. Successful metadata repair clears validation_error. Reader repair does not clear unrelated metadata errors.

Persist raw details, normalized relationships, scalar projections, descriptor validity, and hash/state changes in one catalog transaction. Page-only changes do not invalidate embeddings. Title/tag changes retain v1 hash and index_pending behavior. Search must never imply that translated display titles were embedded.

## 3. Migration and cross-process coordination

Use a schema version table. Migrations acquire an exclusive data-directory lock and run transactionally. Advance the version only after successful completion. A failed migration rolls back; restarting repeats safely. Invalid historical rows are recorded individually without aborting valid-row backfill. Preserve their raw JSON and existing access states. A migration never infers a source 404 or silently purges vectors/data.

SQLite foreign keys and WAL remain enabled. Normalized browse/filter queries use projections, not ad-hoc raw_json parsing. Backfill is the only migration-time JSON fallback. Queued rows retain null scalar projections.

All collect/index/reindex/reader-refresh/migration commands use one common data-directory locking protocol. v2.0 serve holds an exclusive process lock for its lifetime; conflicting CLI operations fail promptly with a controlled message to stop the server. Read-only CLI operations follow the same conservative exclusion rule initially. This avoids relying on operator discipline for cross-store consistency. Within the server, serialize catalog mutations and use connections owned by the executing thread. No SQLite connection is moved into to_thread without explicit thread ownership management.

## 4. Exact structured search

Supported fields: general tag, artist, character, parody, group, language, category. General tags map only to upstream type tag; field mappings are fixed and tested.

AND across fields, OR within a normal multi-value field. tags_all requires every selection; tags_any requires at least one selection. Both may be supplied and both must hold. Empty lists impose no constraint, including empty tags_any. Name selection resolves to all IDs matching normalized name and requested type; each tags_all name selection means at least one of that name's resolved IDs, not every duplicate-name ID.

An unknown include ID contributes a false alternative. It does not invalidate another known OR alternative. An unknown required tags_all selection makes the result empty. Unknown excluded IDs have no effect. Wrong-type IDs are treated as unknown for that field. Exclusions mean NOT(has any excluded ID), combined across exclusion fields with AND. An overlapping OR include/exclude can still match another allowed value. Never simplify such overlap into a globally empty result.

Sort upload_date DESC, gallery_id DESC. Use limit/offset initially, limit default 50 and maximum 100, offset nonnegative. Ordering is deterministic for an unchanged catalog, not a snapshot across concurrent updates. Validate limits and field names; use bound SQL parameters. Index stored sort fields as appropriate.

Autocomplete prefix uses the same name normalization and escaped LIKE with an explicit escape character for %, _, and backslash. Return one record per upstream tag ID: id, type, original name, and count of distinct eligible galleries. Sort by normalized name then ID; default limit 20, maximum 50. Counts and suggestions are local-only. No orphan/inaccessible/invalid-row values leak into suggestions.

## 5. Combined semantic ranking

Resolve the complete eligible ID set before applying browse pagination. No eligible IDs means an empty result without loading/encoding. With eligible records but no compatible index, return a controlled index-unavailable response with instructions, not a fabricated empty success.

Intersect eligible IDs with IDs present in the active embedding-fingerprint collection. Rank only that intersection, or retrieve the complete local candidate ranking before filtering. Never filter only a global top_k. Return distance order, with gallery ID as the deterministic tie-breaker.

Report eligible_total, eligible_indexed, eligible_not_indexed, and stale/pending warnings. These counts describe the full filter set, not a paginated list. No indexed eligible records means an empty result with an incomplete-coverage warning and no query encoding. Preserve v1 missing/stale/deletion checks.

One process-wide provider, with one bounded worker queue serializing lazy model load, manifest resolution, and encode. asyncio.to_thread moves synchronous work off the event loop but supplies no synchronization. Default waiting capacity is 8 requests; full queue or 30-second admission wait returns 503. Cancelled queued requests are removed; an already-running thread retains the inference slot until it exits. Do not release serialization merely because the HTTP request was cancelled.

Web query text: nonempty after whitespace validation, maximum 500 Unicode characters. top_k: 1..50, default 20. Search loads only cached model files and makes no external requests, including telemetry. Preserve exact pinned/resolved revision and fingerprint compatibility. Missing model or manifest yields an actionable controlled error. Do not silently download or change models from a web query.

Default web embeddings use CPU. GPU use requires explicit selection and a tested deployment configuration. Separate processes do not isolate VRAM; any shared-GPU use needs a defined scheduling policy and measured budget.

## 6. Web interface and security

FastAPI, one Uvicorn worker, lightweight templates/JS. Provide browse, filter/autocomplete, vibe search, combined search, and page reader. Translation controls remain absent or disabled in v2.0.

Minimum routes:
- GET / and GET /gallery/{id}
- GET /api/galleries with validated pagination
- GET /api/search/filter and GET /api/suggest
- GET /api/search/vibe with optional structured constraints
- GET /api/gallery/{id}/pages/{page_number} for bounded reader image serving

Document request/response schemas and controlled errors before implementing each route. Invalid inputs: 422; unknown local gallery/page: 404; existing gallery without valid descriptors: 409; compatible model/index unavailable: 503. Upstream image failures have controlled structured errors without raw provider response bodies.

Default bind is 127.0.0.1:8000. Non-loopback requires explicit opt-in and a startup warning; it remains unsupported for public hosting. Allowed Host values are explicit names/addresses, not wildcard bind addresses. Configure localhost/127.0.0.1 and the selected concrete bind name with the expected port. Do not trust forwarded headers by default.

State-changing requests require an exact approved origin and a server-validated CSRF token. Reject missing/unapproved origins for browser mutation routes. Use a session token with HttpOnly and SameSite=Strict session cookie; never place secrets in client code. No permissive CORS. Disable externally loaded interactive API docs/assets by default. Templates autoescape; JS uses textContent, not source text as HTML. Do not interpolate metadata into commands, SQL, or log formats.

Client reader requests accept only validated gallery/page integers. Page fetching uses its own HTTP client without metadata API authorization/contact headers. Backend-specific credentials go only to their intended translator origin; source API credentials never accompany image or translation requests. Errors/logs omit secrets and raw source text. ID-only list/search responses omit source titles/tags from JSON and HTML DOM; it is display minimization, not authentication. Autocomplete necessarily exposes requested tag names and is an explicit user action. Reader mode separately controls title display. Use no-store on sensitive browser/API responses unless deliberately serving bounded image-cache content.

## 7. Safe defaults and configuration

Use pydantic-settings, validated ranges, secret fields excluded from repr/logs, and safe .env.example values. Existing collection rate/cap settings remain.

```dotenv
VIBESEARCH_WEB_HOST=127.0.0.1
VIBESEARCH_WEB_PORT=8000
VIBESEARCH_ALLOW_NON_LOOPBACK=false
VIBESEARCH_EMBED_QUEUE_SIZE=8
VIBESEARCH_EMBED_QUEUE_TIMEOUT_S=30
VIBESEARCH_TITLE_TRANSLATOR=none
VIBESEARCH_PAGE_TRANSLATOR=none
VIBESEARCH_IMAGE_CACHE_MAX_MB=512
VIBESEARCH_IMAGE_CACHE_TTL_HOURS=24
VIBESEARCH_TRANSLATION_QUEUE_SIZE=8
VIBESEARCH_TRANSLATION_MAX_PAGES_PER_JOB=30
VIBESEARCH_REFRESH_MAX_ITEMS_DEFAULT=50
```

For compatibility, IMAGE_CACHE_MAX_MB denotes MiB and must be documented as such. Refresh hard maximum is 100, not an environment value that can bypass the policy. Gallery translation hard maximum is 50. Settings for image limits/timeouts/concurrency and translation limits must be exposed or documented as fixed safe policy. Translator enum options belong in comments/documentation, never pipe-separated placeholder values.

## 8. Reader verification gate and descriptors

The operator-authorized bounded probe is recorded in [READER_VERIFICATION.md](READER_VERIFICATION.md), with [machine-readable results](READER_VERIFICATION_RESULTS.json). OpenAPI version 2.0.0+3ef54aa and five live details confirmed that root pages arrays arrive by default without optional includes. Fields are pages[*].number, path, width, and height; root media_id and num_pages supply bundle identity/count. The retrieved list schema has no page array. Five representative first-page fetches succeeded; the first sample's same page was additionally compared across the other three proposed image hosts. No redirects or 403/404s were observed. This establishes sampled source compatibility, not implementation correctness or universal CDN behavior.

Initial full-page allowlist: i1.nhentai.net as primary and i2.nhentai.net as the only bounded alternate, HTTPS port 443/default. Construct the URL from the approved origin plus the validated supplied pages[*].path. No hash-based routing. i3/i4 matched bytes for one sampled image but are not needed or allowed initially. Thumbnail hosts remain unverified and are excluded from initial browse/reader fetching. Do not infer universal host interchangeability from the probe. Upstream changes or incompatible records fail safely and require revalidation, not guessed paths or automatic host expansion.

Application PageDescriptor: positive contiguous one-based number; validated relative path; extension in webp/jpg/jpeg/png derived from the supplied path; width/height positive integers from the verified PageInfo fields. ReaderBundle includes gallery_id, media_id, num_pages, validated descriptors with exact count, and optional display title. Preserve the supplied full-page path; do not reconstruct it from media_id/page count or thumbnail suffixes. Zero-page records are non-readable. At inspection, all 100 stored details contained count-matching root pages arrays. Backfill validated projections from existing raw_json first; only missing/invalid records require bounded refresh. The probe validated all 364 descriptors across five live details, not every descriptor in the full stored corpus.

Descriptor paths cannot contain a scheme, authority, query, fragment, userinfo, traversal segments, backslashes, controls, or encoded equivalents. Normalize and parse once using a consistent URL implementation; reject ambiguous encodings rather than repeatedly decoding. Validate every resolved redirect URL before requesting it: exact approved HTTPS host, port 443/default, safe path, no userinfo. Automatic redirects off; at most two redirect hops for the entire attempt. Relative Location resolves against the current approved URL and is validated anew. No API headers are forwarded. Approved hosts resolving to non-public destinations are rejected. Routing/retry uses only the verified host policy; do not assume interchangeable hosts.

## 9. Reader refresh and image fetch

refresh-reader-metadata selects locally known, non-readable, metadata-valid records that are not inaccessible. Default attempt cap 50, hard cap 100. Pin the selected ID queue and original cap in a durable run; failures consume attempts, HTTP retries stay bounded, and resume never resets the overall cap. Checkpoint per completed attempt. Previously inaccessible IDs require the existing explicit recovery policy, not automatic retries on every reader open.

Confirmed detail 404 follows catalog deletion rules. Successful refreshed detail updates metadata/tag/page projections transactionally. Changed embedding text schedules index_pending. Missing descriptors yield a controlled reader error, not fabricated pages. Reader opens never implicitly launch metadata collection.

Image fetch: at most two concurrent fetches per process, one alternate-host retry only when the verified routing policy permits and the failure is transient or a permitted 403/404 host failure. No retry for size, format, path, or validation errors. Total wall-clock deadline 30 seconds covers network hops and retries; connect timeout 10 seconds and read-inactivity timeout 15 seconds are secondary. Stop streaming above 15 MiB regardless of Content-Length. Bound/deactivate HTTP content decompression so encoded responses cannot bypass the byte budget.

Validate actual image decoding, signature, dimensions, positive pixel count, at most 25,000,000 pixels and side at most 8192. Reject animated/multiframe files and unsupported formats. Signature alone is not complete validation. Validation must not block the event loop and must occur before publishing/cache success. Apply safe decoder limits before allocating full decoded images. Metadata-reported dimensions are not trusted as the file's actual dimensions. Client disconnects clean up temporary files and retain any busy fetch slot until work has actually stopped.

## 10. Image cache and filesystem lifecycle

Logical key: gallery ID, page number, descriptor-path hash. Lookup record: content hash, server-derived blob path, fetched_at, byte length, validated content type. Blobs are named by content hash under the configured cache root; no source path is used as a local filename.

A hit requires a non-expired record, existing validated blob, and matching stored size. TTL starts at successful fetch completion, not access. Expired records are not served. Misses stream to a bounded temporary file, validate, atomically rename into the same-filesystem blob store, then transactionally publish the lookup. Failed/partial results are never success entries. Concurrent misses for one logical key are coalesced; blob publication/eviction uses a cache synchronization mechanism.

Evict expired/oldest lookup records first; delete blobs only when no retained lookup references them and no active serve/write lease uses them. Disk cap is 512 MiB of unique committed blob bytes. Temporary/in-flight bytes are separately bounded by fetch concurrency times the per-fetch cap. If space cannot be reclaimed safely, fail admission rather than exceeding the cap. Cache reads do not extend TTL. Startup reconciliation removes stale temps and unreferenced blobs and drops dangling lookups without touching files outside the cache root. Never sweep another active process's files; the common process lock establishes ownership. No archive mode is included.

## 11. Translation milestones and immutable identity

Translators default to none. Disabled actions return a controlled disabled response without network/model work. Title and page cloud permissions are separate, explicit opt-ins disclosing title/image/extracted-text transfer and possible billing. No local-to-cloud fallback.

Both backend pin documents identify exact repository/provider/version, license review, invocation/schema, output format, model requirements/download sizes, health check, safe input limits, and measured RAM/VRAM where relevant. Remote localhost adapters are not assumed private/local internally; document whether the selected pipeline calls cloud providers. Target English initially. Title source precedence and source-language handling are pinned with the backend. Original titles remain source data; translated titles are separate display cache entries.

Title cache identity includes source-title hash, source/target language, backend/version and settings hash. Page translation identity includes actual source-image hash, page ID, target language, pinned pipeline/model versions and settings hash. Translation cache has a separate bounded default of 512 MiB and 24-hour fetch/result-time TTL; title cache entries expire after 24 hours and have a default maximum of 10,000 entries. No successful lookup ever points to expired or missing artifacts. Reuse the atomic/reference-aware publication rules where blobs are shared.

Job snapshots include exact source hashes, requested page range, backend/pipeline version, and non-secret settings. Secrets are resolved at execution time from their intended configuration, never serialized into job/cache keys or public status. If the required source/backend no longer matches the snapshot, fail with a controlled source_changed/backend_changed error rather than silently translating different content.

## 12. Durable translation jobs

Routes: POST /api/translate/title, /page, /gallery; GET /api/translate/jobs and /jobs/{id}; POST /jobs/{id}/cancel. Apply CSRF/origin controls. Status responses omit secrets/local filesystem paths. Store jobs/page progress durably in SQLite.

States: queued, running, succeeded, succeeded_with_errors, failed, cancelled. Only queued/running jobs can be cancelled; completed states never change retrospectively. All requested pages succeed means succeeded; some fail with at least one success means succeeded_with_errors; none succeed means failed. Cancellation is terminal even after some page progress.

Default queue capacity 8 includes queued, running, and cancelled executions still physically active. Default page worker concurrency 1. Admission and deduplication are atomic in one writer transaction. Non-null dedupe_key hashes canonical job type, gallery/page identity, exact source snapshot, page range/max_pages, backend/version, target language, and settings snapshot hash. Partial unique index covers queued/running; cancel-pending executions additionally reserve the key until stopped to prevent duplicate physical work.

Cancellation may mark state immediately but retains its execution slot until the backend stops or finishes. Late responses cannot publish artifacts or transition to success. Keep artifacts private to the job until final publication; cancelled job-owned outputs are discarded, without deleting shared artifacts owned by other completed jobs. HTTP timeout/cancellation does not prove backend compute stopped. Release its physical slot only on verified completion/cancellation; if uncertain, mark the backend unavailable pending health/recovery rather than starting more work.

Gallery jobs request a prefix of pages 1..max_pages unless a later version explicitly adds selection. Default min(num_pages, configured cap 30), hard max 50. Queue page work sequentially; continue after individual failures. Per-page deadline 180 seconds includes its retries; overall gallery cap 30 minutes. At most one retry for transient failures, none for input/credential/access validation errors; 429 retry is allowed only within deadlines and valid Retry-After pacing. Deadline exhaustion marks unfinished requested pages failed and computes final aggregate state, unless cancelled.

Startup retains queued snapshots and marks interrupted running jobs failed/interrupted. For a separate backend, verify no prior execution remains active before admitting new inference; otherwise remain unhealthy. No idempotent resume is assumed. Cleanup is lease/ownership aware.

## 13. Required acceptance tests and release evidence

Default CI uses neutral synthetic metadata, temporary stores and mocked HTTP/backends. Tests explicitly disable project .env loading or inject controlled settings; they must not depend on the operator's contact, credentials, model choice, or data directory. Live upstream/backend tests require explicit operator invocation. No new feature is accepted solely because v1 tests pass.

Required groups:
1. Migration: existing queued/valid/malformed rows, duplicate names, renames, rollback/restart, foreign keys, old manifests preserved, no inferred 404/deletion.
2. Filters: AND/OR/all/any, empty/unknown/wrong-type IDs, overlap exclusions, stable sorting, limits, wildcard escaping, valid-row autocomplete counts.
3. Ranking: complete eligible set before pagination, no global-top_k truncation, empty-set/no-index/no-model behavior, counts, tie order, stale/deletion warnings, zero external requests.
4. Web: HTML/JSON privacy, source escaping, Host/port/origin/CSRF checks, no unsafe forwarded headers, bounded queues, serialized load/encode, cancelled request retains actual worker slot, CLI lock conflicts.
5. Reader: verified fixtures, count/order/format/path validity, metadata refresh cap across restart, per-hop SSRF/redirect rejection before request, blocked destinations, per-page errors.
6. Fetch/cache: slow streaming total deadline, absent/false size headers, decompression, animation/decoder limits, concurrent misses, byte/temp bounds, shared blobs/active leases, TTL and crash reconciliation.
7. Translation: disabled zero-network, explicit cloud opt-in, snapshots/source changes, atomic non-null dedupe, gallery range identity, partial failure, cancellation/late success, physical capacity, interrupted backend recovery, cache expiry.
8. Regression: documented clean install includes pytest-asyncio for marked async tests; original collect/index/search/status contracts pass. Real-model smoke includes exact revision resolution and offline reload, with actionable failure diagnostics instead of an undifferentiated operation-failed message.

Record actual commands/results per phase, not invented acceptance evidence. Human semantic-relevance evaluation remains separate from execution correctness. Reader source verification is recorded in READER_VERIFICATION.md; image decoding/security/cache acceptance remains unimplemented and unverified. Translation backend pin documents are not yet completed and must be linked here before their respective implementation phases.
