import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest


def test_web_factory_exists():
    from vibesearch.web import create_app
    assert callable(create_app)


@pytest.mark.asyncio
async def test_worker_cancellation_retains_physical_slot():
    from vibesearch.web.queue import InferenceQueue, QueueBusy
    started, release = threading.Event(), threading.Event()
    calls = []
    def slow():
        calls.append('first')
        started.set()
        release.wait(3)
    async with InferenceQueue(1, .05) as queue:
        first = asyncio.create_task(queue.submit(slow))
        await asyncio.to_thread(started.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(queue.submit(lambda: calls.append('second')))
        await asyncio.sleep(.01)
        with pytest.raises(QueueBusy):
            await queue.submit(lambda: None)
        assert calls == ['first']
        release.set()
        await second
        assert calls == ['first', 'second']


@pytest.mark.asyncio
async def test_queued_cancel_removes_waiter_and_admission_timeout():
    from vibesearch.web.queue import InferenceQueue, QueueBusy
    started, release = threading.Event(), threading.Event()
    def slow():
        started.set()
        release.wait(3)
    async with InferenceQueue(1, .04) as queue:
        first = asyncio.create_task(queue.submit(slow))
        await asyncio.to_thread(started.wait, 1)
        queued = asyncio.create_task(queue.submit(lambda: 1))
        await asyncio.sleep(.01)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        with pytest.raises(QueueBusy):
            await queue.submit(lambda: 2)
        release.set()
        await first
        assert await queue.submit(lambda: 3) == 3


@pytest.mark.asyncio
async def test_shutdown_cancellation_drains_physical_worker():
    from vibesearch.web.queue import InferenceQueue
    started, release = threading.Event(), threading.Event()
    def slow():
        started.set()
        release.wait(3)
    queue = await InferenceQueue(1, 1).__aenter__()
    request = asyncio.create_task(queue.submit(slow))
    assert await asyncio.to_thread(started.wait, 1)
    shutdown = asyncio.create_task(queue.__aexit__(None, None, None))
    await asyncio.sleep(.01)
    shutdown.cancel()
    await asyncio.sleep(.01)
    try:
        assert not shutdown.done(), 'Shutdown released a physically active worker'
    finally:
        release.set()
        request.cancel()
        await asyncio.gather(request, shutdown, return_exceptions=True)


@pytest.fixture
def settings(tmp_path):
    from vibesearch.config import Settings
    return Settings(_env_file=None, data_dir=tmp_path, web_port=8000)


@pytest.fixture
def seeded(settings):
    from vibesearch.api_models import GalleryDetail
    from vibesearch.catalog import Catalog
    raw = {'id': 12, 'media_id': '12', 'title': {'english': '<script>secret title</script>', 'pretty': '', 'japanese': ''},
           'tags': [{'id': 5, 'type': 'artist', 'name': 'secret artist'}],
           'num_pages': 2, 'upload_date': 10,
           'pages': [{'number': n, 'path': f'galleries/12/{n}.jpg', 'width': 2, 'height': 2} for n in (1, 2)]}
    with Catalog(settings.catalog_path) as cat:
        cat.save_detail(GalleryDetail.model_validate(raw), raw)
    return raw


@pytest.mark.asyncio
async def test_routes_privacy_escaping_validation_and_page_proxy(settings, seeded):
    from vibesearch.web import create_app
    class Cache:
        calls = []
        async def get_page(self, gid, raw, number):
            self.calls.append((gid, raw, number))
            return SimpleNamespace(data=b'image', content_type='image/jpeg')
        async def aclose(self):
            pass
    cache = Cache()
    app = create_app(settings, page_cache=cache)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
            full = await client.get('/gallery/12')
            assert full.status_code == 200
            assert '&lt;script&gt;secret title&lt;/script&gt;' in full.text
            assert '<script>secret title</script>' not in full.text
            for path in ('/', '/gallery/12', '/api/galleries', '/api/search/filter'):
                response = await client.get(path, params={'display': 'id-only'})
                assert response.status_code == 200, response.text
                assert 'secret title' not in response.text
                assert 'secret artist' not in response.text
                assert response.headers['cache-control'] == 'no-store'
            assert (await client.get('/api/galleries?limit=101')).status_code == 422
            assert (await client.get('/api/search/vibe?q=%20')).status_code == 422
            assert (await client.get('/api/search/vibe?q=x&top_k=51')).status_code == 422
            assert (await client.get('/api/galleries?unknown=x')).status_code == 422
            page = await client.get('/api/gallery/12/pages/2')
            assert page.status_code == 200 and page.content == b'image'
            assert cache.calls == [(12, seeded, 2)]
            assert (await client.get('/api/gallery/12/pages/3')).status_code == 404
            assert (await client.get('/gallery/99')).status_code == 404
            assert (await client.get('/docs')).status_code == 404


@pytest.mark.asyncio
async def test_host_origin_csrf_and_forwarded_headers(settings):
    from vibesearch.web import create_app
    app = create_app(settings)
    @app.post('/mutation')
    async def mutation():
        return {'ok': True}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
            for host in ('evil.test:8000', '127.0.0.1:9999', '127.0.0.1', 'localhost:8000.evil'):
                assert (await client.get('/', headers={'host': host})).status_code == 400
            good = await client.get('/', headers={'x-forwarded-host': 'evil.test', 'x-forwarded-proto': 'https'})
            assert good.status_code == 200
            assert 'HttpOnly' in good.headers['set-cookie']
            assert 'SameSite=strict' in good.headers['set-cookie']
            assert (await client.post('/mutation')).status_code == 403
            csrf = (await client.get('/api/session')).json()['csrf_token']
            for origin in ('http://evil.test:8000', 'http://127.0.0.1:9999', 'null'):
                assert (await client.post('/mutation', headers={'origin': origin, 'x-csrf-token': csrf})).status_code == 403
            assert (await client.post('/mutation', headers={'origin': 'http://127.0.0.1:8000', 'x-csrf-token': 'bad'})).status_code == 403
            assert (await client.post('/mutation', headers={'origin': 'http://127.0.0.1:8000', 'x-csrf-token': csrf})).status_code == 200
            unusual = await client.post('/mutation', headers=[(b'origin', b'http://127.0.0.1:8000'), (b'x-csrf-token', b'\xff')])
            assert unusual.status_code == 403
            forged = await client.get('/api/session', headers=[(b'cookie', b'vibesearch_session=' + b'a' * 32 + b'.' + b'\xff' * 64)])
            assert forged.status_code == 200


def test_non_loopback_requires_opt_in(settings):
    from vibesearch.web import create_app
    settings.web_host = '0.0.0.0'
    with pytest.raises(ValueError, match='loopback'):
        create_app(settings)


@pytest.mark.asyncio
async def test_real_search_injected_provider_is_offline_and_private(settings, seeded, monkeypatch):
    from vibesearch.web import create_app
    from vibesearch.catalog import Catalog
    from vibesearch.indexer import Indexer
    from test_indexer import FakeEmbedder
    import socket
    provider = FakeEmbedder()
    with Catalog(settings.catalog_path) as cat:
        Indexer(cat, provider, str(settings.chroma_path)).run()
    def forbidden(*args, **kwargs):
        raise AssertionError('Search attempted network access')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    app = create_app(settings, provider=provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8000') as client:
            result = await client.get('/api/search/vibe?q=quiet&display=id-only&artist=5')
            assert result.status_code == 200, result.text
            assert 'secret title' not in result.text and 'secret artist' not in result.text
            assert result.json()['eligible_total'] == 1
            assert result.json()['eligible_indexed'] == 1
            assert result.json()['hits'][0]['gallery_id'] == 12
            assert provider.queries == ['quiet']
            empty = await client.get('/api/search/vibe?q=quiet&artist=999')
            assert empty.status_code == 200 and empty.json()['eligible_total'] == 0
            assert provider.queries == ['quiet']
            suggestions = await client.get('/api/suggest?field=artist&prefix=secret')
            assert suggestions.json()['items'][0]['name'] == 'secret artist'
            assert (await client.get('/api/suggest?field=invalid')).status_code == 422
            assert (await client.get('/api/search/filter?filters=%7B%22artist%22%3A%5Btrue%5D%7D')).status_code == 422


@pytest.mark.asyncio
async def test_missing_index_and_invalid_reader_are_controlled(settings, seeded):
    from vibesearch.web import create_app
    from vibesearch.catalog import Catalog
    from test_indexer import FakeEmbedder
    app = create_app(settings, provider=FakeEmbedder())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8000') as client:
            result = await client.get('/api/search/vibe?q=quiet')
            assert result.status_code == 503
            assert result.json()['detail']['code'] == 'index_unavailable'
            assert 'secret' not in result.text
            with Catalog(settings.catalog_path) as cat:
                cat.db.execute("UPDATE galleries SET raw_json=json_remove(raw_json,'$.pages') WHERE id=12")
                cat.db.commit()
            assert (await client.get('/gallery/12')).status_code == 409
            assert (await client.get('/api/gallery/12/pages/1')).status_code == 409


@pytest.mark.asyncio
async def test_lifespan_holds_and_releases_common_lock(settings):
    from vibesearch.web import create_app
    from vibesearch.locking import DataBusy, data_lock
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        with pytest.raises(DataBusy):
            with data_lock(settings.data_dir):
                pass
    with data_lock(settings.data_dir):
        pass


@pytest.mark.asyncio
async def test_asgi_cancelled_inference_retains_slot(settings, seeded):
    from vibesearch.web import create_app
    from vibesearch.catalog import Catalog
    from vibesearch.indexer import Indexer
    from test_indexer import FakeEmbedder
    class Blocking(FakeEmbedder):
        def __init__(self):
            super().__init__()
            self.started, self.release = threading.Event(), threading.Event()
            self.active = 0
            self.maximum = 0
        def embed_query(self, text):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.queries.append(text)
            if text == 'first':
                self.started.set()
                self.release.wait(3)
            self.active -= 1
            return [1., 0.]
    provider = Blocking()
    with Catalog(settings.catalog_path) as cat:
        Indexer(cat, provider, str(settings.chroma_path)).run()
    settings.embed_queue_size = 1
    settings.embed_queue_timeout_s = 1
    app = create_app(settings, provider=provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8000') as client:
            first = asyncio.create_task(client.get('/api/search/vibe?q=first'))
            assert await asyncio.to_thread(provider.started.wait, 2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(client.get('/api/search/vibe?q=second'))
            await asyncio.sleep(.03)
            overloaded = await client.get('/api/search/vibe?q=third')
            assert overloaded.status_code == 503
            assert provider.queries == ['first']
            provider.release.set()
            assert (await second).status_code == 200
            assert provider.maximum == 1
            assert provider.queries == ['first', 'second']
