"""Offline search, safe rendering and exact local browser boundaries."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import warnings

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .queue import InferenceQueue, QueueBusy

ROOT = Path(__file__).parent
TEMPLATES = Environment(loader=FileSystemLoader(ROOT / 'templates'), autoescape=select_autoescape(['html']))
FILTER_FIELDS = {'tag', 'tags', 'tags_any', 'tags_all', 'artist', 'character', 'parody', 'group',
                 'language', 'category', 'exclude_tags', 'exclude_artists'}


def error(status, code, message):
    return HTTPException(status, detail={'code': code, 'message': message})


def _filters(request, options):
    result = {}
    allowed = FILTER_FIELDS | set(options) | {'display', 'filters'}
    if set(request.query_params) - allowed:
        raise error(422, 'invalid_filter', 'Unsupported query parameter')
    try:
        if 'filters' in request.query_params:
            raw = request.query_params['filters']
            if len(raw) > 16000:
                raise ValueError()
            result = json.loads(raw)
            if not isinstance(result, dict) or set(result) - FILTER_FIELDS:
                raise ValueError()
            for key, value in result.items():
                if not isinstance(value, list) or len(value) > 100:
                    raise ValueError()
                if any(type(item) not in (str, int) for item in value):
                    raise ValueError()
        for key in FILTER_FIELDS & set(request.query_params):
            values = request.query_params.getlist(key)
            result.setdefault(key, []).extend(int(v) if v.isascii() and v.isdecimal() else v for v in values)
        if any(len(v) > 100 or any(isinstance(s, str) and len(s) > 500 for s in v) for v in result.values()):
            raise ValueError()
    except (ValueError, TypeError):
        raise error(422, 'invalid_filter', 'Filters must contain supported fields and bounded ID/name lists') from None
    return result


def create_app(settings=None, provider=None, page_cache=None):
    from vibesearch.config import Settings
    from ..catalog import Catalog, SQLITE_MAX
    from vibesearch.filter import FilterService
    from vibesearch.embeddings import LocalSentenceTransformer
    from vibesearch.locking import data_lock
    from vibesearch.reader import ReaderError, reader_bundle

    settings = settings or Settings()
    host, port = settings.web_host, settings.web_port
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == 'localhost'
    if not loopback:
        if not settings.allow_non_loopback:
            raise ValueError('Non-loopback web binding requires explicit opt-in')
        warnings.warn('Non-loopback serving is not supported for public hosting', RuntimeWarning, stacklevel=2)
    hosts = {'localhost', '127.0.0.1'}
    if host not in {'0.0.0.0', '::', '*'}:
        hosts.add(host)
    authorities = {f'[{h}]:{port}' if ':' in h else f'{h}:{port}' for h in hosts}
    origins = {'http://' + a for a in authorities}
    secret = secrets.token_bytes(32)
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    provider = provider if provider is not None else LocalSentenceTransformer(
        settings.embedding_model, revision=settings.embedding_revision,
        device='cpu' if settings.embedding_device == 'auto' else settings.embedding_device,
        batch_size=settings.embedding_batch_size, normalize=settings.embedding_normalize, offline=True)

    def db_call(operation):
        with Catalog(settings.catalog_path) as catalog:
            return operation(catalog)

    async def run_db(operation):
        return await asyncio.to_thread(db_call, operation)

    @asynccontextmanager
    async def lifespan(app):
        from vibesearch.image_cache import PageCache
        # The process lock is held through physically-active queue/cache shutdown.
        with data_lock(settings.data_dir):
            await run_db(lambda catalog: None)
            cache = page_cache if page_cache is not None else PageCache(
                settings.data_dir / 'image-cache',
                max_bytes=settings.image_cache_max_mb * 1024 * 1024,
                ttl_seconds=settings.image_cache_ttl_hours * 3600)
            app.state.page_cache = cache
            try:
                async with InferenceQueue(settings.embed_queue_size, settings.embed_queue_timeout_s) as queue:
                    app.state.inference = queue
                    yield
            finally:
                await cache.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.provider = provider

    def sign(value, label):
        return hmac.new(secret, (label + value).encode(), hashlib.sha256).hexdigest()

    def session_nonce(cookie):
        if not cookie or not cookie.isascii() or len(cookie) != 97 or cookie[32] != '.':
            return None
        nonce, signature = cookie.split('.', 1)
        if not hmac.compare_digest(signature, sign(nonce, 'session:')):
            return None
        return nonce

    @app.middleware('http')
    async def browser_boundary(request, call_next):
        # Duplicated Host/Origin headers are ambiguous and must fail closed.
        hosts_received = request.headers.getlist('host')
        if len(hosts_received) != 1 or hosts_received[0] not in authorities:
            return JSONResponse({'detail': {'code': 'host_rejected', 'message': 'Host is not approved'}}, status_code=400)
        nonce = session_nonce(request.cookies.get('vibesearch_session'))
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin_headers = request.headers.getlist('origin')
            csrf = request.headers.get('x-csrf-token', '')
            if len(origin_headers) != 1 or origin_headers[0] not in origins or not nonce or not csrf.isascii() or not hmac.compare_digest(csrf, sign(nonce, 'csrf:')):
                return JSONResponse({'detail': {'code': 'csrf_rejected', 'message': 'Approved origin and session CSRF token required'}}, status_code=403)
        fresh = nonce is None
        nonce = nonce or secrets.token_hex(16)
        request.state.csrf_token = sign(nonce, 'csrf:')
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        if fresh:
            response.set_cookie('vibesearch_session', nonce + '.' + sign(nonce, 'session:'), httponly=True,
                                samesite='strict', secure=False, path='/')
        return response

    def display_mode(display):
        mode = settings.display_mode if display is None else display
        if mode not in {'full', 'id-only'}:
            raise error(422, 'invalid_display', 'Display must be full or id-only')
        return mode

    async def gallery_raw(gid):
        def lookup(catalog):
            row = catalog.get(gid)
            if row is None or row['state'] in {'queued', 'inaccessible'} or not row['raw_json']:
                raise error(404, 'not_found', 'Local gallery not found')
            if row['validation_error'] is not None:
                raise error(409, 'metadata_invalid', 'Local gallery metadata needs repair')
            return json.loads(row['raw_json'])
        return await run_db(lookup)

    @app.get('/api/session')
    async def session(request: Request):
        return {'csrf_token': request.state.csrf_token}

    @app.get('/', response_class=HTMLResponse)
    async def home(display: str | None = None):
        return TEMPLATES.get_template('home.html').render(display=display_mode(display))

    @app.get('/assets/{asset}')
    async def asset(asset: str):
        if asset not in {'app.js', 'app.css'}:
            raise error(404, 'not_found', 'Asset not found')
        return Response((ROOT / 'static' / asset).read_bytes(), media_type='text/javascript' if asset.endswith('.js') else 'text/css')

    @app.get('/api/galleries')
    @app.get('/api/search/filter')
    async def listing(request: Request, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), display: str | None = None):
        filters, mode = _filters(request, {'limit', 'offset'}), display_mode(display)
        try:
            return await run_db(lambda cat: FilterService(cat).list(filters, limit=limit, offset=offset, display=mode))
        except ValueError:
            raise error(422, 'invalid_filter', 'Invalid filter selections') from None

    @app.get('/api/suggest')
    async def suggest(request: Request, field: str, prefix: str = Query('', max_length=500), limit: int = Query(20, ge=1, le=50)):
        if set(request.query_params) - {'field', 'prefix', 'limit'} or field not in FILTER_FIELDS or field.startswith('exclude_'):
            raise error(422, 'invalid_field', 'Unsupported autocomplete field')
        try:
            return {'items': await run_db(lambda cat: FilterService(cat).suggest(field, prefix, limit))}
        except ValueError:
            raise error(422, 'invalid_field', 'Invalid autocomplete options') from None

    @app.get('/api/search/vibe')
    async def vibe(request: Request, q: str = Query(..., min_length=1, max_length=500), top_k: int = Query(20, ge=1, le=50), display: str | None = None):
        if not q.strip():
            raise error(422, 'invalid_query', 'Query must not be blank')
        filters, mode = _filters(request, {'q', 'top_k'}), display_mode(display)
        def search(catalog):
            from vibesearch.search import SearchService
            eligible = FilterService(catalog).eligible_ids(filters)
            # Manifest/provider construction may load a model: avoid it for no candidates.
            if not eligible:
                return {'hits': [], 'eligible_total': 0, 'eligible_indexed': 0, 'eligible_not_indexed': 0,
                        'indexed_count': 0, 'missing_count': 0, 'pending_warning': False}
            service = SearchService(catalog, provider, str(settings.chroma_path),
                                    template_version=settings.text_template_version)
            result = service.search(q, top_k=top_k, display=mode, eligible_ids=eligible)
            return asdict(result) if is_dataclass(result) else result
        try:
            return await app.state.inference.submit(lambda: db_call(search))
        except QueueBusy:
            raise error(503, 'inference_busy', 'Inference capacity unavailable; retry later') from None
        except (LookupError, RuntimeError, OSError):
            raise error(503, 'index_unavailable', 'Compatible local model/index unavailable; cache the pinned model and run index before serving') from None
        except ValueError:
            raise error(422, 'invalid_search', 'Invalid search or filter options') from None

    @app.get('/gallery/{gallery_id}', response_class=HTMLResponse)
    async def gallery(gallery_id: int, display: str | None = None):
        if not 0 < gallery_id <= SQLITE_MAX:
            raise error(422, 'invalid_id', 'Gallery ID must be positive')
        raw, mode = await gallery_raw(gallery_id), display_mode(display)
        try:
            bundle = await asyncio.to_thread(reader_bundle, raw, mode)
        except ReaderError as exc:
            raise error(exc.status_code, 'reader_unavailable', exc.message) from None
        return TEMPLATES.get_template('gallery.html').render(bundle=bundle, display=mode)

    @app.get('/api/gallery/{gallery_id}/pages/{page_number}')
    async def page(gallery_id: int, page_number: int):
        if not 0 < gallery_id <= SQLITE_MAX or not 0 < page_number <= SQLITE_MAX:
            raise error(422, 'invalid_page', 'Gallery ID and page must be positive')
        raw = await gallery_raw(gallery_id)
        try:
            bundle = await asyncio.to_thread(reader_bundle, raw, 'id-only')
            if page_number > bundle['num_pages']:
                raise error(404, 'not_found', 'Local page not found')
            image = await app.state.page_cache.get_page(gallery_id, raw, page_number)
            return Response(image.data, media_type=image.content_type)
        except ReaderError as exc:
            raise error(exc.status_code, 'reader_unavailable', exc.message) from None

    return app
