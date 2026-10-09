import asyncio
import io

import httpx
import pytest
from PIL import Image

from vibesearch.image_cache import PageCache
from vibesearch.reader import ReaderError
from test_reader import detail


def different_media(media):
    raw = detail(f'galleries/{media}/1.png')
    raw['media_id'] = str(media)
    return raw


def image(fmt='PNG', size=(2, 3)):
    out = io.BytesIO()
    Image.new('RGB', size, 'white').save(out, format=fmt)
    return out.getvalue()


async def public(host):
    return ['93.184.216.34']


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data, delay=0, chunks=1):
        self.data, self.delay, self.chunks = data, delay, chunks

    async def __aiter__(self):
        for _ in range(self.chunks):
            await asyncio.sleep(self.delay)
            yield self.data


def run(coro):
    return asyncio.run(coro)


def test_decode_cache_and_coalescing(tmp_path):
    async def scenario():
        calls = []
        async def handler(request):
            calls.append(request)
            await asyncio.sleep(.01)
            return httpx.Response(200, content=image(), headers={'content-type': 'text/plain'})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cache = PageCache(tmp_path, client=client, resolver=public)
        results = await asyncio.gather(*(cache.get_page(7, detail(), 1) for _ in range(6)))
        assert all(r.content_type == 'image/png' and r.data == image() for r in results)
        assert len(calls) == 1
        assert 'authorization' not in calls[0].headers
        await cache.aclose()
        cache = PageCache(tmp_path, client=client, resolver=public)
        assert (await cache.get_page(7, detail(), 1)).data == image()
        assert len(calls) == 1
        await cache.aclose()
        await client.aclose()
    run(scenario())


@pytest.mark.parametrize('payload,headers', [(b'not an image', {}), (image()[:-10], {}),
    (image(), {'content-encoding': 'gzip'}), (image(size=(8193, 1)), {})])
def test_bad_images_not_cached(tmp_path, payload, headers):
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, stream=BytesStream(payload), headers=headers)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = PageCache(tmp_path, client=client, resolver=public)
            with pytest.raises(ReaderError):
                await cache.get_page(7, detail(), 1)
            assert len(calls) == 1
            assert not list((tmp_path / 'blobs').iterdir())
            assert not list((tmp_path / 'tmp').iterdir())
            await cache.aclose()
    run(scenario())


@pytest.mark.parametrize('location', ['https://evil.test/a.png', 'http://i1.nhentai.net/a.png',
    'https://i1.nhentai.net:444/a.png', 'https://user@i1.nhentai.net/a.png', '/a%2f.png'])
def test_redirect_rejected_before_request(tmp_path, location):
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={'location': location})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = PageCache(tmp_path, client=client, resolver=public)
            with pytest.raises(ReaderError):
                await cache.get_page(7, detail(), 1)
            assert len(calls) == 1
            await cache.aclose()
    run(scenario())


def test_private_dns_blocks_before_network(tmp_path):
    async def scenario():
        async def private(host):
            return ['127.0.0.1']
        def handler(request):
            pytest.fail('Unsafe request')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = PageCache(tmp_path, client=client, resolver=private)
            with pytest.raises(ReaderError):
                await cache.get_page(7, detail(), 1)
            await cache.aclose()
    run(scenario())


def test_bounded_alternate(tmp_path):
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request.url.host)
            return httpx.Response(404) if len(calls) == 1 else httpx.Response(200, content=image())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = PageCache(tmp_path, client=client, resolver=public)
            await cache.get_page(7, detail(), 1)
            assert calls == ['i1.nhentai.net', 'i2.nhentai.net']
            await cache.aclose()
    run(scenario())


def test_shared_blobs_eviction_ttl_and_reconciliation(tmp_path):
    async def scenario():
        clock = [10.]
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, content=image())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = PageCache(tmp_path, max_bytes=len(image()), ttl_seconds=5, clock=lambda: clock[0], client=client, resolver=public)
            await cache.get_page(7, detail(), 1)
            raw = different_media(43)
            await cache.get_page(7, raw, 1)
            assert len(list((tmp_path / 'blobs').iterdir())) == 1
            clock[0] = 16
            await cache.get_page(7, detail(), 1)
            assert len(calls) == 3
            await cache.aclose()
            (tmp_path / 'tmp' / 'stale').write_bytes(b'x')
            (tmp_path / 'blobs' / ('a' * 64)).write_bytes(b'x')
            cache = PageCache(tmp_path, client=client, resolver=public)
            assert not list((tmp_path / 'tmp').iterdir())
            assert not (tmp_path / 'blobs' / ('a' * 64)).exists()
            await cache.aclose()
    run(scenario())


def test_animation_rejected(tmp_path):
    output = io.BytesIO()
    Image.new('RGB', (2, 3), 'red').save(output, format='PNG', save_all=True,
        append_images=[Image.new('RGB', (2, 3), 'blue')], duration=100, loop=0)
    test_bad_images_not_cached(tmp_path, output.getvalue(), {})


@pytest.mark.parametrize('headers', [{}, {'content-length': '1'}])
def test_stream_byte_cap_ignores_missing_or_false_header(tmp_path, headers):
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, headers=headers,
                stream=BytesStream(b'x' * (1024 * 1024), chunks=16))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = PageCache(tmp_path, client=client, resolver=public)
            with pytest.raises(ReaderError, match='size limit'):
                await cache.get_page(7, detail(), 1)
            assert len(calls) == 1
            assert not list((tmp_path / 'tmp').iterdir())
            await cache.aclose()
    run(scenario())


def test_total_deadline_slow_stream(tmp_path):
    async def scenario():
        def handler(request):
            return httpx.Response(200, stream=BytesStream(b'x', delay=.015, chunks=20))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = PageCache(tmp_path, client=client, resolver=public, deadline_seconds=.04)
            with pytest.raises(ReaderError) as error:
                await cache.get_page(7, detail(), 1)
            assert error.value.status_code == 504
            assert not list((tmp_path / 'tmp').iterdir())
            assert not list((tmp_path / 'blobs').iterdir())
            await cache.aclose()
    run(scenario())


def test_two_concurrent_fetches_and_cancel_cleanup(tmp_path):
    async def scenario():
        active = [0, 0]
        class Slow(httpx.AsyncByteStream):
            async def __aiter__(self):
                active[0] += 1
                active[1] = max(active)
                try:
                    await asyncio.sleep(.03)
                    yield image()
                finally:
                    active[0] -= 1
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Slow()))) as client:
            cache = PageCache(tmp_path, client=client, resolver=public)
            tasks = [asyncio.create_task(cache.get_page(7, different_media(42 + i), 1)) for i in range(5)]
            await asyncio.sleep(.01)
            tasks[0].cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert active == [0, 2]
            assert not list((tmp_path / 'tmp').iterdir())
            await cache.aclose()
    run(scenario())


def test_dns_rechecked_on_approved_redirect(tmp_path):
    async def scenario():
        calls = []
        async def resolve(host):
            return ['93.184.216.34'] if host == 'i1.nhentai.net' else ['10.0.0.1']
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={'location': 'https://i2.nhentai.net/a.png'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = PageCache(tmp_path, client=client, resolver=resolve)
            with pytest.raises(ReaderError):
                await cache.get_page(7, detail(), 1)
            assert len(calls) == 1
            await cache.aclose()
    run(scenario())


def test_actual_connection_uses_validated_ip(monkeypatch):
    from vibesearch.image_cache import _PinnedBackend
    from httpcore._backends.auto import AutoBackend
    seen = []
    async def connect(self, host, port, *args):
        seen.append((host, port))
        return object()
    monkeypatch.setattr(AutoBackend, 'connect_tcp', connect)
    async def scenario():
        backend = _PinnedBackend(public)
        await backend.connect_tcp('i1.nhentai.net', 443)
        assert seen == [('93.184.216.34', 443)]
        async def private(host):
            return ['::1']
        backend = _PinnedBackend(private)
        with pytest.raises(ReaderError):
            await backend.connect_tcp('i1.nhentai.net', 443)
        assert len(seen) == 1
    run(scenario())


def test_cache_admission_below_blob_size(tmp_path):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=image()))) as client:
            cache = PageCache(tmp_path, max_bytes=1, client=client, resolver=public)
            with pytest.raises(ReaderError) as error:
                await cache.get_page(7, detail(), 1)
            assert error.value.status_code == 503
            assert not list((tmp_path / 'blobs').iterdir())
            assert not list((tmp_path / 'tmp').iterdir())
            await cache.aclose()
    run(scenario())


@pytest.mark.parametrize('fmt,extension,kind', [('JPEG', 'jpg', 'image/jpeg'), ('WEBP', 'webp', 'image/webp')])
def test_supported_real_decoders(tmp_path, fmt, extension, kind):
    async def scenario():
        data = image(fmt)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=data))) as client:
            cache = PageCache(tmp_path, client=client, resolver=public)
            result = await cache.get_page(7, detail(f'galleries/42/1.{extension}'), 1)
            assert result.data == data and result.content_type == kind
            await cache.aclose()
    run(scenario())


def test_format_extension_mismatch_rejected(tmp_path):
    test_bad_images_not_cached(tmp_path, image('JPEG'), {})


def test_cached_corrupt_blob_refetched(tmp_path):
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, content=image())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = PageCache(tmp_path, client=client, resolver=public)
            await cache.get_page(7, detail(), 1)
            blob = next((tmp_path / 'blobs').iterdir())
            blob.write_bytes(b'x' * len(image()))
            assert (await cache.get_page(7, detail(), 1)).data == image()
            assert len(calls) == 2
            await cache.aclose()
    run(scenario())


def test_symlink_blob_cannot_read_or_delete_external_target(tmp_path):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=image()))) as client:
            cache = PageCache(tmp_path / 'cache', client=client, resolver=public)
            await cache.get_page(7, detail(), 1)
            blob = next((tmp_path / 'cache' / 'blobs').iterdir())
            external = tmp_path / 'outside'
            external.write_bytes(image())
            blob.unlink()
            blob.symlink_to(external)
            assert (await cache.get_page(7, detail(), 1)).data == image()
            assert external.read_bytes() == image()
            assert not blob.is_symlink()
            await cache.aclose()
    run(scenario())


def test_redirect_hops_total_across_retry(tmp_path):
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) in (1, 2, 4):
                return httpx.Response(302, headers={'location': '/next.png'})
            return httpx.Response(403)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = PageCache(tmp_path, client=client, resolver=public)
            with pytest.raises(ReaderError, match='redirect'):
                await cache.get_page(7, detail(), 1)
            assert len(calls) == 4
            await cache.aclose()
    run(scenario())


def test_pixel_budget_before_decoder_load(tmp_path, monkeypatch):
    import struct
    import zlib
    data = bytearray(image())
    data[16:24] = struct.pack('>II', 6000, 6000)
    data[29:33] = struct.pack('>I', zlib.crc32(data[12:29]))
    test_bad_images_not_cached(tmp_path, bytes(data), {})


def test_safe_disk_failure(tmp_path, monkeypatch):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=image()))) as client:
            cache = PageCache(tmp_path, client=client, resolver=public)
            def fail(*args):
                raise OSError('private filesystem location')
            monkeypatch.setattr(cache, '_hit', fail)
            with pytest.raises(ReaderError) as error:
                await cache.get_page(7, detail(), 1)
            assert 'private' not in str(error.value)
            assert error.value.status_code == 503
            await cache.aclose()
    run(scenario())


def test_deadline_during_publication_no_late_success(tmp_path, monkeypatch):
    import os
    import time
    replace = os.replace
    def slow_replace(*args):
        time.sleep(.04)
        return replace(*args)
    monkeypatch.setattr(os, 'replace', slow_replace)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=image()))) as client:
            cache = PageCache(tmp_path, client=client, resolver=public, deadline_seconds=.02)
            with pytest.raises(ReaderError) as error:
                await cache.get_page(7, detail(), 1)
            assert error.value.status_code == 504
            assert not list((tmp_path / 'blobs').iterdir())
            assert not list((tmp_path / 'tmp').iterdir())
            await cache.aclose()
    run(scenario())


def test_eviction_keeps_retained_references_and_returned_bytes(tmp_path):
    import sqlite3
    async def scenario():
        first, second = image(), image(size=(3, 2))
        def handler(request):
            return httpx.Response(200, content=second if request.url.path.startswith('/galleries/44/') else first)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = PageCache(tmp_path, max_bytes=max(len(first), len(second)), client=client, resolver=public)
            page = await cache.get_page(7, detail(), 1)
            await cache.get_page(7, different_media(43), 1)
            await cache.get_page(7, different_media(44), 1)
            assert page.data == first
            blobs = list((tmp_path / 'blobs').iterdir())
            assert len(blobs) == 1 and blobs[0].read_bytes() == second
            with sqlite3.connect(tmp_path / 'lookup.sqlite3') as db:
                assert db.execute('SELECT COUNT(*) FROM lookups').fetchone()[0] == 1
            await cache.aclose()
    run(scenario())


def test_ttl_starts_at_success_not_fetch_start_and_hits_do_not_extend(tmp_path):
    async def scenario():
        clock, calls = [0.], []
        def handler(request):
            calls.append(request)
            clock[0] += 10
            return httpx.Response(200, content=image())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = PageCache(tmp_path, client=client, resolver=public, clock=lambda: clock[0], ttl_seconds=5)
            await cache.get_page(7, detail(), 1)
            clock[0] = 14
            await cache.get_page(7, detail(), 1)
            assert len(calls) == 1
            clock[0] = 15
            await cache.get_page(7, detail(), 1)
            assert len(calls) == 2
            await cache.aclose()
    run(scenario())


@pytest.mark.parametrize('page,expected', [(0, 404), (2, 404), (True, 404)])
def test_unknown_page_zero_network(tmp_path, page, expected):
    async def scenario():
        def handler(request):
            pytest.fail('Unknown pages must not fetch')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cache = PageCache(tmp_path, client=client, resolver=public)
            with pytest.raises(ReaderError) as error:
                await cache.get_page(7, detail(), page)
            assert error.value.status_code == expected
            await cache.aclose()
    run(scenario())
