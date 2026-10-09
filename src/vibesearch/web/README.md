# Local web contract (v2.0)

Run one ASGI worker only. `create_app(settings=None, provider=None, page_cache=None)`
accepts an embedding provider and asynchronous PageCache for offline tests. The application
holds `data_lock(data_dir)` through startup migration, serving, inference draining and cache
shutdown. SQLite connections are opened and closed in the executing thread. Search only
uses the offline local model cache; missing compatible resources return 503.

All GET list/search/reader routes accept `display=full|id-only` (default from Settings).
ID-only omits source titles and tags in JSON and HTML. Autocomplete is an explicit exception.
There are no translation controls or routes and no external assets, API documentation UI,
CORS or forwarded-header trust. Sensitive responses use `Cache-Control: no-store`.

- `GET /`: browse/query/filter interface; no source metadata embedded initially.
- `GET /gallery/{positive id}`: reader, page navigation and on-demand image proxy.
- `GET /api/galleries`, `/api/search/filter`: `limit=1..100` (50), `offset>=0` (0).
  Returns `{items,total,limit,offset}`.
- `GET /api/search/vibe`: `q` nonblank, maximum 500 characters; `top_k=1..50` (20).
  Returns SearchService result including hits, coverage counts and pending warning.
- Lists and vibe accept repeated exact filter query parameters: tag, tags, tags_any,
  tags_all, artist, character, parody, group, language, category, exclude_tags,
  exclude_artists. Decimal selections become integer tag IDs; other strings are names.
  For numeric names or mixed JSON selections use `filters={"artist":["123",5]}`;
  this JSON object merges with repeated selections. Unsupported fields are rejected.
- `GET /api/suggest`: required supported `field`, `prefix` (max 500), `limit=1..50` (20).
  Returns `{items:[{id,type,name,count}]}`. Called only by user autocomplete action.
- `GET /api/gallery/{id}/pages/{positive page}`: validated image bytes with media type;
  calls `PageCache.get_page(id,raw,page)`; never accepts source URLs from the browser.
- `GET /api/session`: `{csrf_token}` bound to a signed, HttpOnly, SameSite=Strict cookie.
  Any future mutation must supply `Origin` exactly matching an approved local origin and
  `X-CSRF-Token` from this endpoint. No mutation endpoint is introduced in v2.0.

Errors are `{detail:{code,message}}`: malformed input 422, unknown local gallery/page 404,
invalid reader descriptors 409, busy queue or missing model/index 503. Framework parameter
validation uses its normal 422 details. Host mismatches (including wrong/missing port) are
400; mutation origin/session/token failures are 403. Queue capacity bounds waiting jobs;
start admission expires after the configured timeout. Cancelled queued jobs are removed,
and cancellation of running requests cannot release the physical inference worker.

Non-loopback binding needs explicit opt-in and warns that public hosting is unsupported.
Wildcard bind addresses are never accepted Host values. Approved hosts are localhost,
127.0.0.1 and a configured concrete bind host, each with the configured port.
