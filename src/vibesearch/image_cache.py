"""Bounded verified image fetching and reference-aware, content-addressed cache.

The caller holds the application's exclusive data-directory process lock. Test
clients are trusted injection points; production connections pin validated DNS
addresses while retaining the approved hostname for TLS verification/SNI.
"""
import asyncio
from dataclasses import dataclass
import hashlib
import io
import ipaddress
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit
import warnings

from httpcore._backends.auto import AutoBackend
import httpx
from PIL import Image

from .reader import ReaderError, reader_bundle, safe_path

HOSTS = ('i1.nhentai.net', 'i2.nhentai.net')
BYTE_CAP = 15 * 1024 * 1024
PIXEL_CAP = 25_000_000
SIDE_CAP = 8192
# Shared by all cache instances in the serving event loop.
_FETCH_SLOTS: asyncio.Semaphore | None = None
_FETCH_LOOP = None


async def _dns(host):
    rows = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return list({row[4][0] for row in rows})


async def _addresses(resolver, host):
    try:
        addresses = await resolver(host)
        if not addresses or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in addresses):
            raise ReaderError('Image destination rejected', 502)
        return addresses
    except ReaderError:
        raise
    except Exception:
        raise ReaderError('Image destination unavailable', 502) from None


class _PinnedBackend(AutoBackend):
    def __init__(self, resolver):
        self.resolver = resolver

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host not in HOSTS or port != 443:
            raise ReaderError('Image destination rejected', 502)
        addresses = await _addresses(self.resolver, host)
        return await super().connect_tcp(addresses[0], port, timeout, local_address, socket_options)


def _url(url):
    try:
        if any(ord(c) < 33 or ord(c) == 127 for c in url) or '?' in url or '#' in url or '\\' in url:
            raise ValueError()
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.port not in (None, 443) or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        if parsed.query or parsed.fragment or '%' in parsed.netloc:
            raise ValueError()
        safe_path(parsed.path, relative=False)
    except (ValueError, ReaderError):
        raise ReaderError('Image destination rejected', 502) from None
    return parsed.hostname


@dataclass(frozen=True)
class CachedPage:
    data: bytes
    content_type: str


def _decode(data, extension):
    expected = {'png': 'PNG', 'jpg': 'JPEG', 'jpeg': 'JPEG', 'webp': 'WEBP'}[extension]
    signatures = {'PNG': data.startswith(b'\x89PNG\r\n\x1a\n'),
                  'JPEG': data.startswith(b'\xff\xd8\xff'),
                  'WEBP': data.startswith(b'RIFF') and data[8:12] == b'WEBP'}
    if not signatures[expected]:
        raise ReaderError('Invalid image data', 502)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as im:
                width, height = im.size
                if im.format != expected or width <= 0 or height <= 0 or max(width, height) > SIDE_CAP or width * height > PIXEL_CAP or getattr(im, 'n_frames', 1) != 1 or getattr(im, 'is_animated', False):
                    raise ValueError()
                im.verify()
            with Image.open(io.BytesIO(data)) as im:
                im.load()
        return {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}[expected]
    except Exception:
        raise ReaderError('Invalid image data', 502) from None


async def _drain(task):
    """Keep physical ownership despite repeated cancellation of this waiter."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    try:
        task.result()
    except BaseException:
        pass


async def _thread(function, *args, on_cancel=None):
    # Cancellation cannot free a physical slot or remove a temp being used by
    # a decoder/disk thread that is still executing.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        await _drain(task)
        raise


class _Transient(Exception):
    pass


class PageCache:
    def __init__(self, root, max_bytes=512 * 1024 * 1024, ttl_seconds=86400, *, client=None,
                 resolver=None, clock=time.time, deadline_seconds: float = 30):
        if max_bytes <= 0 or ttl_seconds <= 0 or not 0 < deadline_seconds <= 30:
            raise ValueError('Invalid cache policy')
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs, self.temps = self.root / 'blobs', self.root / 'tmp'
        for directory in (self.blobs, self.temps):
            if directory.is_symlink():
                raise ReaderError('Unsafe cache directory', 503)
            directory.mkdir(exist_ok=True)
        self.db = self.root / 'lookup.sqlite3'
        if self.db.is_symlink():
            raise ReaderError('Unsafe cache directory', 503)
        self.max_bytes, self.ttl_seconds = max_bytes, ttl_seconds
        self.clock, self.deadline = clock, deadline_seconds
        self.resolver = resolver or _dns
        self._owns_client = client is None
        if client is None:
            transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
            # HTTPX has no public network-backend injection; preserve its SSL
            # context and replace only the pool backend to prevent DNS rebinding.
            transport._pool._network_backend = _PinnedBackend(self.resolver)
            client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                       timeout=httpx.Timeout(15, connect=10))
        self.client = client
        self.lock = asyncio.Lock()
        self.inflight = {}
        self.waiters = {}
        self.closed = False
        self._reconcile()

    def _connect(self):
        return sqlite3.connect(self.db)

    def _blob(self, digest):
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ReaderError('Invalid cache record', 503)
        return self.blobs / digest

    def _sweep(self, db):
        retained = {row[0] for row in db.execute('SELECT DISTINCT digest FROM lookups')}
        for path in self.blobs.iterdir():
            if path.name not in retained and (path.is_file() or path.is_symlink()):
                path.unlink()

    def _reconcile(self):
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS lookups (key TEXT PRIMARY KEY, digest TEXT NOT NULL, fetched REAL NOT NULL, size INTEGER NOT NULL, type TEXT NOT NULL)')
            for key, digest, fetched, size, kind in db.execute('SELECT * FROM lookups').fetchall():
                try:
                    path = self._blob(digest)
                    good = not path.is_symlink() and path.is_file() and path.stat().st_size == size and fetched + self.ttl_seconds > self.clock() and kind in {'image/png', 'image/jpeg', 'image/webp'}
                except (ReaderError, OSError):
                    good = False
                if not good:
                    db.execute('DELETE FROM lookups WHERE key=?', (key,))
            while self._usage(db) > self.max_bytes:
                db.execute('DELETE FROM lookups WHERE key=(SELECT key FROM lookups ORDER BY fetched,key LIMIT 1)')
            db.commit()
            self._sweep(db)
        for path in self.temps.iterdir():
            if path.is_file() or path.is_symlink():
                path.unlink()

    @staticmethod
    def _usage(db):
        return db.execute('SELECT COALESCE(SUM(size),0) FROM (SELECT digest,MAX(size) AS size FROM lookups GROUP BY digest)').fetchone()[0]

    def _hit(self, key, extension):
        with self._connect() as db:
            row = db.execute('SELECT digest,fetched,size,type FROM lookups WHERE key=?', (key,)).fetchone()
            if row:
                digest, fetched, size, kind = row
                try:
                    path = self._blob(digest)
                    if 0 < size <= min(BYTE_CAP, self.max_bytes) and not path.is_symlink() and fetched + self.ttl_seconds > self.clock() and path.stat().st_size == size:
                        data = path.read_bytes()
                        if hashlib.sha256(data).hexdigest() == digest and _decode(data, extension) == kind:
                            return CachedPage(data, kind)
                except (OSError, ReaderError):
                    pass
                db.execute('DELETE FROM lookups WHERE key=?', (key,))
                db.commit()
                self._sweep(db)
        return None

    def _publish(self, key, temp, data, kind, expires, stop):
        try:
            return self._publish_impl(key, temp, data, kind, expires, stop)
        except Exception:
            with self._connect() as db:
                self._sweep(db)
            raise

    def _publish_impl(self, key, temp, data, kind, expires, stop):
        digest = hashlib.sha256(data).hexdigest()
        if len(data) > self.max_bytes:
            raise ReaderError('Image cache capacity exceeded', 503)
        with self._connect() as db:
            db.execute('DELETE FROM lookups WHERE fetched<=?', (self.clock() - self.ttl_seconds,))
            db.execute('DELETE FROM lookups WHERE key=?', (key,))
            exists = db.execute('SELECT 1 FROM lookups WHERE digest=?', (digest,)).fetchone()
            required = 0 if exists else len(data)
            while self._usage(db) + required > self.max_bytes:
                row = db.execute('SELECT key FROM lookups ORDER BY fetched,key LIMIT 1').fetchone()
                if not row:
                    raise ReaderError('Image cache capacity exceeded', 503)
                db.execute('DELETE FROM lookups WHERE key=?', row)
            target = self._blob(digest)
            if stop.is_set() or time.monotonic() >= expires:
                raise ReaderError('Image fetch timed out', 504)
        # Eviction commits before irreversible deletion. If reclamation fails,
        # do not publish another blob or exceed the committed disk cap.
        with self._connect() as db:
            self._sweep(db)
            if stop.is_set() or time.monotonic() >= expires:
                raise ReaderError('Image fetch timed out', 504)
            os.replace(temp, target)
            if stop.is_set() or time.monotonic() >= expires:
                raise ReaderError('Image fetch timed out', 504)
            db.execute('INSERT INTO lookups VALUES (?,?,?,?,?)', (key, digest, self.clock(), len(data), kind))
        return CachedPage(data, kind)

    async def get_page(self, gallery_id, raw, page_number):
        if self.closed:
            raise ReaderError('Image cache closed', 503)
        bundle = reader_bundle(raw, display='ids')
        if type(gallery_id) is not int or gallery_id != bundle['gallery_id']:
            raise ReaderError('Gallery unavailable', 404)
        if type(page_number) is not int or not 1 <= page_number <= bundle['num_pages']:
            raise ReaderError('Page unavailable', 404)
        descriptor = bundle['pages'][page_number - 1]
        key = f"{gallery_id}:{page_number}:" + hashlib.sha256(descriptor['path'].encode()).hexdigest()
        async with self.lock:
            if key not in self.inflight:
                self.inflight[key] = asyncio.create_task(self._get(key, descriptor))
                self.waiters[key] = 0
            task = self.inflight[key]
            self.waiters[key] += 1
        try:
            return await asyncio.shield(task)
        finally:
            async with self.lock:
                self.waiters[key] -= 1
                if not self.waiters[key]:
                    if not task.done():
                        task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    del self.inflight[key]
                    del self.waiters[key]

    async def _get(self, key, descriptor):
        try:
            return await self._get_impl(key, descriptor)
        except (OSError, sqlite3.Error):
            raise ReaderError('Image cache unavailable', 503) from None

    async def _get_impl(self, key, descriptor):
        global _FETCH_SLOTS, _FETCH_LOOP
        async with self.lock:
            hit = await _thread(self._hit, key, descriptor['extension'])
        if hit:
            return hit
        loop = asyncio.get_running_loop()
        if _FETCH_LOOP is not loop:
            _FETCH_LOOP, _FETCH_SLOTS = loop, asyncio.Semaphore(2)
        try:
            assert _FETCH_SLOTS is not None
            expires = time.monotonic() + self.deadline
            async with asyncio.timeout(self.deadline):
                async with _FETCH_SLOTS:
                    fd, name = tempfile.mkstemp(dir=self.temps, prefix='fetch-')
                    temp = Path(name)
                    try:
                        redirects = [0]
                        data = b''
                        with os.fdopen(fd, 'wb') as file:
                            for attempt, host in enumerate(HOSTS):
                                try:
                                    data = await self._fetch(f'https://{host}/{descriptor["path"]}', file, redirects)
                                    break
                                except _Transient:
                                    if attempt == 1:
                                        raise ReaderError('Image source unavailable', 502) from None
                                    file.seek(0)
                                    file.truncate()
                            file.flush()
                            os.fsync(file.fileno())
                        kind = await _thread(_decode, data, descriptor['extension'])
                        async with self.lock:
                            stop = threading.Event()
                            return await _thread(self._publish, key, temp, data, kind, expires, stop, on_cancel=stop.set)
                    finally:
                        temp.unlink(missing_ok=True)
        except TimeoutError:
            raise ReaderError('Image fetch timed out', 504) from None
        except OSError:
            raise ReaderError('Image cache unavailable', 503) from None

    async def _fetch(self, url, file, redirects):
        for hop in range(3):
            host = _url(url)
            await _addresses(self.resolver, host)
            try:
                # Build explicitly so an injected client cannot forward its auth,
                # cookies or API headers. Production ignores environment proxies.
                request = httpx.Request('GET', url, headers={'Accept-Encoding': 'identity'},
                                        extensions={'timeout': {'connect': 10, 'read': 15, 'write': 15, 'pool': 10}})
                response = await self.client.send(request, stream=True, follow_redirects=False, auth=None)
                try:
                    if response.status_code in (301, 302, 303, 307, 308):
                        if redirects[0] >= 2 or 'location' not in response.headers:
                            raise ReaderError('Image redirect rejected', 502)
                        redirects[0] += 1
                        location = response.headers['location']
                        if any(ord(c) < 33 or ord(c) == 127 for c in location) or '\\' in location:
                            raise ReaderError('Image redirect rejected', 502)
                        url = urljoin(url, location)
                        _url(url)
                        continue
                    if response.status_code in (403, 404, 408, 429) or response.status_code >= 500:
                        raise _Transient()
                    if response.status_code != 200:
                        raise ReaderError('Image source unavailable', 502)
                    if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                        raise ReaderError('Encoded image response rejected', 502)
                    length = response.headers.get('content-length')
                    if length is not None:
                        try:
                            if int(length) < 0 or int(length) > BYTE_CAP:
                                raise ReaderError('Image size limit exceeded', 502)
                        except ValueError:
                            raise ReaderError('Invalid image response', 502) from None
                    chunks, size = [], 0
                    # MockTransport responses may be pre-read; real responses use
                    # raw chunks, never HTTPX's automatically decompressed stream.
                    stream = response.aiter_raw() if not response.is_stream_consumed else response.aiter_bytes()
                    async for chunk in stream:
                        size += len(chunk)
                        if size > BYTE_CAP:
                            raise ReaderError('Image size limit exceeded', 502)
                        file.write(chunk)
                        chunks.append(chunk)
                    return b''.join(chunks)
                finally:
                    await response.aclose()
            except httpx.DecodingError:
                raise ReaderError('Invalid image response', 502) from None
            except httpx.TransportError:
                raise _Transient() from None
        raise ReaderError('Image redirect rejected', 502)

    async def aclose(self):
        self.closed = True
        cleanup = asyncio.create_task(self._close())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await _drain(cleanup)
            raise

    async def _close(self):
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_client:
            await self.client.aclose()
