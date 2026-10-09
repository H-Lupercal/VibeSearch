"""Regression coverage for independently reproduced v2 review defects."""
import asyncio
import io
import json
import threading
import time

import httpx
import pytest
from PIL import Image

from vibesearch.api_models import GalleryDetail
from vibesearch.catalog import Catalog
from vibesearch.config import Settings
from vibesearch.filter import FilterService
from vibesearch.image_cache import PageCache
from vibesearch.reader import ReaderError, reader_bundle
from vibesearch.web.app import create_app
from vibesearch.web.queue import InferenceQueue


def raw(gid=1, name="Old", tag_id=1):
    return dict(id=gid, media_id=str(gid), title=dict(english="Neutral", pretty="Neutral"),
                tags=[dict(id=tag_id, type="tag", name=name)], num_pages=1, upload_date=10,
                pages=[dict(number=1, path=f"galleries/{gid}/1.png", width=2, height=3)])


def image(color):
    buf = io.BytesIO()
    Image.new("RGB", (2, 3), color).save(buf, format="PNG")
    return buf.getvalue()


async def public(host):
    return ["93.184.216.34"]


def test_backfill_uses_detail_observation_not_gallery_or_index_order(tmp_path):
    path = tmp_path / "catalog"
    with Catalog(path) as c:
        for gid, name in [(1, "Old"), (2, "Old"), (1, "New")]:
            detail = raw(gid, name)
            c.save_detail(GalleryDetail.model_validate(detail), detail)
        c.mark_indexed(2)  # Indexing is not a new observation of source tags.
        c.db.execute("DELETE FROM schema_version")
        c.db.commit()
    with Catalog(path) as c:
        assert FilterService(c).suggest("tag", "")[0]["name"] == "New"


def test_one_validator_preserves_supplied_paths(tmp_path):
    detail = raw()
    detail["pages"][0]["path"] = "galleries/1/supplied-page.png"
    with Catalog(tmp_path / "catalog") as c:
        c.save_detail(GalleryDetail.model_validate(detail), detail)
        assert c.get(1)["readable"] == 1
        assert reader_bundle(detail)["pages"][0]["path"] == detail["pages"][0]["path"]


@pytest.mark.parametrize("field", ["tag", "num_pages", "upload_date"])
def test_bad_historical_numeric_projection_is_isolated(tmp_path, field):
    path = tmp_path / "catalog"
    bad = raw()
    if field == "tag":
        bad["tags"][0]["id"] = 2**63
    else:
        bad[field] = 2**63
    with Catalog(path) as c:
        good = raw(2)
        c.save_detail(GalleryDetail.model_validate(good), good)
        c.queue_candidate(1)
        c.db.execute("UPDATE galleries SET raw_json=?,state='indexed',indexed_hash='preserved' WHERE id=1", (json.dumps(bad),))
        c.db.execute("DELETE FROM schema_version")
        c.db.commit()
    with Catalog(path) as c:
        row = c.get(1)
        assert row["validation_error"] == "invalid_metadata"
        assert row["indexed_hash"] == "preserved" and row["state"] == "indexed"
        assert json.loads(row["raw_json"]) == bad
        assert FilterService(c).eligible_ids() == {2}


def test_oversized_filter_ids_are_unknown(tmp_path):
    with Catalog(tmp_path / "catalog") as c:
        detail = raw()
        c.save_detail(GalleryDetail.model_validate(detail), detail)
        assert FilterService(c).eligible_ids({"tags_any": [2**63]}) == set()
        assert FilterService(c).eligible_ids({"exclude_tags": [-(2**63)-1]}) == {1}


def test_web_integer_boundaries_are_controlled(tmp_path):
    async def run():
        app = create_app(Settings(_env_file=None, data_dir=tmp_path), provider=object())
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
                for path in ["/gallery/9223372036854775808", "/api/gallery/9223372036854775808/pages/1", "/api/gallery/1/pages/9223372036854775808"]:
                    assert (await client.get(path)).status_code == 422
                response = await client.get("/api/galleries?tags_any=9223372036854775808")
                assert response.status_code == 200 and response.json()["total"] == 0
    asyncio.run(run())


def test_rollback_preserves_evicted_blob(tmp_path):
    async def run():
        first, second = image("white"), image("red")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=first))) as client:
            cache = PageCache(tmp_path, max_bytes=max(len(first), len(second)), client=client, resolver=public)
            await cache.get_page(1, raw(), 1)
            with cache._connect() as db:
                old = db.execute("SELECT digest FROM lookups").fetchone()[0]
            temp = cache.temps / "probe"
            temp.write_bytes(second)
            stop = threading.Event()
            stop.set()
            with pytest.raises(ReaderError):
                cache._publish("new", temp, second, "image/png", time.monotonic()+10, stop)
            with cache._connect() as db:
                assert db.execute("SELECT digest FROM lookups").fetchone()[0] == old
            assert cache._blob(old).read_bytes() == first
            await cache.aclose()
    asyncio.run(run())


def test_repeated_cancel_retains_decoder_slot_and_shutdown(tmp_path, monkeypatch):
    import vibesearch.image_cache as mod
    async def run():
        started, release = threading.Event(), threading.Event()
        real_decode = mod._decode
        def blocked(data, extension):
            started.set()
            assert release.wait(3), "test must release blocked decoder"
            return real_decode(data, extension)
        monkeypatch.setattr(mod, "_decode", blocked)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=image("white")))) as client:
            cache = PageCache(tmp_path, client=client, resolver=public)
            request = asyncio.create_task(cache.get_page(1, raw(), 1))
            assert await asyncio.to_thread(started.wait, 1)
            request.cancel()
            await asyncio.sleep(.02)
            closing = asyncio.create_task(cache.aclose())
            await asyncio.sleep(.02)
            try:
                assert not closing.done()
                assert mod._FETCH_SLOTS._value == 1
                closing.cancel()
                await asyncio.sleep(.02)
                assert not closing.done()
                assert mod._FETCH_SLOTS._value == 1
            finally:
                release.set()
                await asyncio.gather(request, closing, return_exceptions=True)
            assert mod._FETCH_SLOTS._value == 2
            assert not list(cache.temps.iterdir())
    asyncio.run(run())


def test_repeated_queue_shutdown_retains_worker():
    async def run():
        started, release = threading.Event(), threading.Event()
        def blocked():
            started.set()
            assert release.wait(3)
            return 1
        queue = InferenceQueue(1, 1)
        await queue.__aenter__()
        request = asyncio.create_task(queue.submit(blocked))
        assert await asyncio.to_thread(started.wait, 1)
        closing = asyncio.create_task(queue.__aexit__(None, None, None))
        await asyncio.sleep(.01)
        closing.cancel()
        await asyncio.sleep(.01)
        closing.cancel()
        await asyncio.sleep(.01)
        try:
            assert not closing.done()
            assert queue.worker is not None and not queue.worker.done()
        finally:
            release.set()
            await asyncio.gather(request, closing, return_exceptions=True)
    asyncio.run(run())
