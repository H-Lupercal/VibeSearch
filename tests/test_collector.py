import asyncio

import httpx
import pytest

from vibesearch.api_client import APIRateLimited, MetadataClient
from vibesearch.catalog import Catalog
from vibesearch.collector import Collector


def detail(i):
    return {"id": i, "media_id": str(i), "title": {"english": f"Gallery {i}", "pretty": f"Gallery {i}"}, "tags": [], "num_pages": 2, "upload_date": 1}


def listing(ids, pages=1, per_page=25):
    return {"result": [{"id": i, "media_id": str(i), "english_title": f"Gallery {i}", "num_pages": 2} for i in ids], "num_pages": pages, "per_page": per_page}


def client(handler):
    return MetadataClient(user_agent="VibeSearch/0.1", transport=httpx.MockTransport(handler), sleep=lambda _: asyncio.sleep(0), clock=lambda: 0)


@pytest.mark.asyncio
async def test_cap_100_deduplication_and_detail_fields(tmp_path):
    counts = {"details": 0}

    def handler(request):
        if request.url.path.endswith("/galleries"):
            page = int(request.url.params["page"])
            ids = range(125, 25, -1) if page == 1 else range(100, 0, -1)
            return httpx.Response(200, json=listing(ids, pages=2, per_page=100))
        counts["details"] += 1
        return httpx.Response(200, json=detail(int(request.url.path.rsplit("/", 1)[1])))

    with Catalog(tmp_path / "db") as catalog:
        async with client(handler) as api:
            outcome = await Collector(api, catalog, per_page=100, max_items=100).collect()
        assert outcome.fetched == 100 and outcome.pages_scanned == 1
        assert counts["details"] == 100
        assert catalog.counts()["index_pending"] == 100
        assert catalog.get(125)["raw_json"] is not None
        assert catalog.get(125)["content_hash"]


@pytest.mark.asyncio
async def test_restart_queue_first_overlap_and_404(tmp_path):
    path = tmp_path / "db"
    calls = []
    fail = [True]

    def handler(request):
        if request.url.path.endswith("/galleries"):
            page = int(request.url.params["page"])
            calls.append(("list", page))
            return httpx.Response(200, json=listing({1: [10, 9], 2: [9, 8], 3: [8, 7], 4: [7, 6]}.get(page, []), pages=4))
        gallery_id = int(request.url.path.rsplit("/", 1)[1])
        calls.append(("detail", gallery_id))
        if gallery_id == 10 and fail[0]:
            return httpx.Response(503)
        if gallery_id == 9:
            return httpx.Response(404)
        return httpx.Response(200, json=detail(gallery_id))

    with Catalog(path) as catalog:
        async with MetadataClient(user_agent="VibeSearch/0.1", max_retries=0, transport=httpx.MockTransport(handler), sleep=lambda _: asyncio.sleep(0), clock=lambda: 0) as api:
            with pytest.raises(Exception):
                await Collector(api, catalog, max_pages=4).collect()
        assert catalog.counts()["queued"] == 2
        run = catalog.latest_run()["run_id"]
    fail[0] = False
    calls.clear()
    with Catalog(path) as catalog:
        async with client(handler) as api:
            result = await Collector(api, catalog, max_pages=4).collect()
        assert result.run_id == run
        assert calls[0] == ("detail", 10)
        assert ("list", 1) in calls
        assert result.inaccessible == 1
        assert result.deduplicated >= 2
        assert catalog.counts()["inaccessible"] == 1
        assert catalog.get_run(run)["last_scanned_page"] == 4
        assert catalog.counts()["index_pending"] == 4


@pytest.mark.asyncio
async def test_rescan_from_two_pages_back(tmp_path):
    pages = []
    with Catalog(tmp_path / "db") as catalog:
        run = catalog.start_run()
        catalog.set_ceiling(run, 50)
        catalog.queue_page(run, 4, [])
        catalog.set_run_status(run, "paused")

        def handler(request):
            if request.url.path.endswith("/galleries"):
                page = int(request.url.params["page"])
                pages.append(page)
                return httpx.Response(200, json=listing([50 - page], pages=5))
            return httpx.Response(200, json=detail(int(request.url.path.rsplit("/", 1)[1])))

        async with client(handler) as api:
            await Collector(api, catalog, max_pages=2).collect()
        assert pages == [2, 3]


@pytest.mark.asyncio
async def test_persistent_rate_limit_pauses_durable_run(tmp_path):
    with Catalog(tmp_path / "db") as catalog:
        async with MetadataClient(user_agent="VibeSearch/0.1", max_retries=0, transport=httpx.MockTransport(lambda request: httpx.Response(429)), sleep=lambda _: asyncio.sleep(0)) as api:
            with pytest.raises(APIRateLimited):
                await Collector(api, catalog).collect()
        assert catalog.latest_run()["status"] == "paused"


@pytest.mark.asyncio
async def test_rate_limited_run_resumes_with_same_id(tmp_path):
    limited = [True]

    def handler(request):
        if limited[0]:
            return httpx.Response(429)
        if request.url.path.endswith("/galleries"):
            return httpx.Response(200, json=listing([1]))
        return httpx.Response(200, json=detail(1))

    with Catalog(tmp_path / "db") as catalog:
        async with MetadataClient(user_agent="VibeSearch/0.1", max_retries=0,
                                  transport=httpx.MockTransport(handler),
                                  sleep=lambda _: asyncio.sleep(0), clock=lambda: 0) as api:
            with pytest.raises(APIRateLimited):
                await Collector(api, catalog, max_items=1).collect()
            run_id = catalog.latest_run()["run_id"]
            assert catalog.get_run(run_id)["status"] == "paused"
            limited[0] = False
            result = await Collector(api, catalog, max_items=1).collect()
        assert result.run_id == run_id
        assert result.fetched == 1 and result.status == "completed"


@pytest.mark.asyncio
async def test_invalid_detail_remains_queued_without_repeated_request(tmp_path):
    attempts = []

    def handler(request):
        if request.url.path.endswith("/galleries"):
            return httpx.Response(200, json=listing([4], pages=3))
        attempts.append(4)
        return httpx.Response(200, json={"id": 4})

    with Catalog(tmp_path / "db") as catalog:
        async with client(handler) as api:
            result = await Collector(api, catalog, max_pages=3).collect()
        assert result.skipped == 1
        assert result.pending == 1
        assert attempts == [4]
        assert catalog.get(4)["error_category"] == "schema"


@pytest.mark.asyncio
async def test_run_cap_survives_failure_restart_and_pins_original_limit(tmp_path):
    path = tmp_path / "db"
    calls = []
    interrupt = [True]

    def handler(request):
        if request.url.path.endswith("/galleries"):
            return httpx.Response(200, json=listing(range(100, 0, -1), pages=1, per_page=100))
        gallery_id = int(request.url.path.rsplit("/", 1)[1])
        calls.append(gallery_id)
        if gallery_id == 40 and interrupt[0]:
            return httpx.Response(503)
        return httpx.Response(200, json=detail(gallery_id))

    with Catalog(path) as catalog:
        async with MetadataClient(user_agent="VibeSearch/0.1", max_retries=0, transport=httpx.MockTransport(handler), sleep=lambda _: asyncio.sleep(0), clock=lambda: 0) as api:
            with pytest.raises(Exception):
                await Collector(api, catalog, max_items=100, per_page=100).collect()
        run_id = catalog.latest_run()["run_id"]
        assert catalog.get_run(run_id)["fetched_count"] == 60
        assert len(catalog.attempted_ids(run_id)) == 60

    interrupt[0] = False
    with Catalog(path) as catalog:
        async with client(handler) as api:
            outcome = await Collector(api, catalog, max_items=100, per_page=100).collect()
        assert outcome.run_id == run_id
        assert outcome.fetched == 100 and outcome.status == "completed"
        assert len(catalog.attempted_ids(run_id)) == 100
        assert catalog.get_run(run_id)["fetched_count"] == 100
        assert catalog.counts()["index_pending"] == 100
        before = len(calls)
        async with client(handler) as api:
            with pytest.raises(ValueError, match="completed run"):
                await Collector(api, catalog, max_items=100, per_page=100).collect(run_id=run_id)
        assert len(calls) == before


@pytest.mark.asyncio
async def test_lower_original_limit_cannot_expand_on_resume(tmp_path):
    calls = []
    with Catalog(tmp_path / "db") as catalog:
        run_id = catalog.start_run()
        catalog.queue_page(run_id, 1, [3, 2, 1])

        def handler(request):
            gallery_id = int(request.url.path.rsplit("/", 1)[1])
            calls.append(gallery_id)
            return httpx.Response(200, json=detail(gallery_id))

        async with client(handler) as api:
            first = await Collector(api, catalog, max_items=1).collect(run_id=run_id)
        assert first.fetched == 1 and calls == [3]
        async with client(handler) as api:
            with pytest.raises(ValueError, match="completed run"):
                await Collector(api, catalog, max_items=100).collect(run_id=run_id)
        assert calls == [3]
        assert catalog.get_run(run_id)["item_limit"] == 1


@pytest.mark.asyncio
async def test_bounded_refresh_tombstones_indexed_id_once_even_after_resume(tmp_path):
    path = tmp_path / "db"
    seen = []
    fail_page_two = [True]
    with Catalog(path) as catalog:
        from vibesearch.api_models import GalleryDetail
        catalog.save_detail(GalleryDetail.model_validate(detail(12)), detail(12))
        catalog.mark_indexed(12)

    def handler(request):
        if request.url.path.endswith("/galleries"):
            page = int(request.url.params["page"])
            if page == 2 and fail_page_two[0]:
                return httpx.Response(503)
            return httpx.Response(200, json=listing([12, 11], pages=2))
        gallery_id = int(request.url.path.rsplit("/", 1)[1])
        seen.append(gallery_id)
        return httpx.Response(404) if gallery_id == 12 else httpx.Response(200, json=detail(gallery_id))

    with Catalog(path) as catalog:
        async with MetadataClient(user_agent="VibeSearch/0.1", max_retries=0, transport=httpx.MockTransport(handler), sleep=lambda _: asyncio.sleep(0), clock=lambda: 0) as api:
            with pytest.raises(Exception):
                await Collector(api, catalog, max_pages=2).collect()
        run_id = catalog.latest_run()["run_id"]
        assert seen == [11, 12]
        assert catalog.get(12)["state"] == "inaccessible"
        assert [row["id"] for row in catalog.list_deletions()] == [12]
    fail_page_two[0] = False
    with Catalog(path) as catalog:
        async with client(handler) as api:
            outcome = await Collector(api, catalog, max_pages=2).collect()
        assert outcome.run_id == run_id
        assert seen == [11, 12]
        assert catalog.get_run(run_id)["fetched_count"] == 1
        assert len(catalog.attempted_ids(run_id)) == 2
        assert outcome.status == "completed"


@pytest.mark.asyncio
async def test_refresh_changed_indexed_record_and_404_share_detail_budget(tmp_path):
    from vibesearch.api_models import GalleryDetail

    requests = []
    with Catalog(tmp_path / "db") as catalog:
        for gallery_id in (9, 8):
            catalog.save_detail(GalleryDetail.model_validate(detail(gallery_id)), detail(gallery_id))
            catalog.mark_indexed(gallery_id)

        def handler(request):
            if request.url.path.endswith("/galleries"):
                return httpx.Response(200, json=listing([9, 8], pages=2))
            gallery_id = int(request.url.path.rsplit("/", 1)[1])
            requests.append(gallery_id)
            if gallery_id == 9:
                return httpx.Response(404)
            updated = detail(8)
            updated["title"]["english"] = "Revised title"
            return httpx.Response(200, json=updated)

        async with client(handler) as api:
            outcome = await Collector(api, catalog, max_items=2, max_pages=2).collect()
        assert requests == [9, 8]
        assert outcome.fetched == 1 and outcome.status == "completed"
        assert catalog.get(9)["state"] == "inaccessible"
        assert catalog.get(8)["state"] == "index_pending"
        assert catalog.get_run(outcome.run_id)["fetched_count"] == 1
        assert len(catalog.attempted_ids(outcome.run_id)) == 2
        async with client(handler) as api:
            with pytest.raises(ValueError, match="completed run"):
                await Collector(api, catalog, max_items=2).collect(run_id=outcome.run_id)
        assert requests == [9, 8]


@pytest.mark.asyncio
async def test_valid_cap_completes_and_next_run_drains_queued_work(tmp_path):
    calls = []

    def handler(request):
        if request.url.path.endswith("/galleries"):
            return httpx.Response(200, json=listing([2, 1]))
        gallery_id = int(request.url.path.rsplit("/", 1)[1])
        calls.append(gallery_id)
        return httpx.Response(200, json=detail(gallery_id))

    with Catalog(tmp_path / "db") as catalog:
        async with client(handler) as api:
            first = await Collector(api, catalog, max_items=1).collect()
            assert first.status == "completed" and first.fetched == 1
            assert first.pending == 1 and calls == [2]
            second = await Collector(api, catalog, max_items=1).collect()
        assert second.run_id != first.run_id
        assert second.status == "completed" and second.fetched == 1
        assert calls == [2, 1]
        assert catalog.get_run(first.run_id)["fetched_count"] == 1
        assert catalog.get_run(second.run_id)["fetched_count"] == 1


@pytest.mark.asyncio
async def test_404_does_not_consume_valid_cap_but_attempts_are_bounded(tmp_path):
    calls = []

    def handler(request):
        if request.url.path.endswith("/galleries"):
            return httpx.Response(200, json=listing([4, 3, 2, 1]))
        gallery_id = int(request.url.path.rsplit("/", 1)[1])
        calls.append(gallery_id)
        return httpx.Response(404) if gallery_id == 4 else httpx.Response(200, json=detail(gallery_id))

    with Catalog(tmp_path / "db") as catalog:
        async with client(handler) as api:
            result = await Collector(api, catalog, max_items=1).collect()
        assert calls == [4, 3]
        assert result.fetched == 1 and result.inaccessible == 1
        assert result.status == "completed" and result.pending == 2
        assert len(catalog.attempted_ids(result.run_id)) == 2

    calls.clear()

    def missing_handler(request):
        if request.url.path.endswith("/galleries"):
            return httpx.Response(200, json=listing([4, 3, 2, 1]))
        calls.append(int(request.url.path.rsplit("/", 1)[1]))
        return httpx.Response(404)

    with Catalog(tmp_path / "missing-db") as catalog:
        async with client(missing_handler) as api:
            first = await Collector(api, catalog, max_items=1).collect()
            second = await Collector(api, catalog, max_items=1).collect()
        assert first.status == "completed" and first.fetched == 0
        assert second.run_id != first.run_id
        assert calls == [4, 3, 2, 1]
        assert len(catalog.attempted_ids(first.run_id)) == 2
        assert len(catalog.attempted_ids(second.run_id)) == 2


@pytest.mark.asyncio
async def test_schema_attempt_budget_is_finite(tmp_path):
    calls = []

    def handler(request):
        if request.url.path.endswith("/galleries"):
            return httpx.Response(200, json=listing([4, 3, 2, 1], pages=3))
        calls.append(int(request.url.path.rsplit("/", 1)[1]))
        return httpx.Response(200, json={"id": calls[-1]})

    with Catalog(tmp_path / "db") as catalog:
        async with client(handler) as api:
            result = await Collector(api, catalog, max_items=1, max_pages=3).collect()
        assert calls == [4, 3]
        assert result.fetched == 0 and result.status == "completed"
        assert result.pending == 4
        assert len(catalog.attempted_ids(result.run_id)) == 2


@pytest.mark.asyncio
async def test_legacy_paused_at_cap_is_retired(tmp_path):
    calls = []
    with Catalog(tmp_path / "db") as catalog:
        old = catalog.start_run()
        catalog.set_item_limit(old, 1)
        catalog.queue_page(old, 1, [2, 1])
        from vibesearch.api_models import GalleryDetail
        catalog.save_detail(GalleryDetail.model_validate(detail(2)), detail(2), run_id=old)
        catalog.set_run_status(old, "paused")

        def handler(request):
            if request.url.path.endswith("/galleries"):
                return httpx.Response(200, json=listing([2, 1]))
            calls.append(int(request.url.path.rsplit("/", 1)[1]))
            return httpx.Response(200, json=detail(calls[-1]))

        async with client(handler) as api:
            next_run = await Collector(api, catalog, max_items=1).collect()
        assert catalog.get_run(old)["status"] == "completed"
        assert next_run.run_id != old and calls == [1]


@pytest.mark.asyncio
async def test_attempt_budget_persists_across_failed_run(tmp_path):
    path = tmp_path / "db"
    fail = [True]
    calls = []

    def handler(request):
        if request.url.path.endswith("/galleries"):
            return httpx.Response(200, json=listing([2, 1]))
        gallery_id = int(request.url.path.rsplit("/", 1)[1])
        calls.append(gallery_id)
        if gallery_id == 2:
            return httpx.Response(404)
        return httpx.Response(503) if fail[0] else httpx.Response(200, json=detail(1))

    with Catalog(path) as catalog:
        async with MetadataClient(user_agent="VibeSearch/0.1", max_retries=0,
                                  transport=httpx.MockTransport(handler),
                                  sleep=lambda _: asyncio.sleep(0), clock=lambda: 0) as api:
            with pytest.raises(Exception):
                await Collector(api, catalog, max_items=1).collect()
        run_id = catalog.latest_run()["run_id"]
        assert catalog.get_run(run_id)["status"] == "failed"
        assert catalog.attempted_ids(run_id) == {2}

    fail[0] = False
    with Catalog(path) as catalog:
        async with client(handler) as api:
            result = await Collector(api, catalog, max_items=1).collect()
        assert result.run_id == run_id and result.fetched == 1
        assert result.status == "completed"
        assert calls == [2, 1, 1]
        assert catalog.attempted_ids(run_id) == {2, 1}


@pytest.mark.asyncio
async def test_two_connections_cannot_collect_same_run_concurrently(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    detail_calls = []

    async def handler(request):
        if request.url.path.endswith("/galleries"):
            return httpx.Response(200, json=listing([1]))
        detail_calls.append(1)
        entered.set()
        await release.wait()
        return httpx.Response(200, json=detail(1))

    path = tmp_path / "db"
    with Catalog(path) as first, Catalog(path) as second:
        run_id = first.start_run()
        async with client(handler) as api:
            collecting = asyncio.create_task(Collector(api, first).collect(run_id=run_id))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                with pytest.raises(RuntimeError, match="already in progress"):
                    await Collector(api, second).collect(run_id=run_id)
                assert second.get_run(run_id)["status"] == "running"
                assert second.attempted_ids(run_id) == set()
            finally:
                release.set()
            outcome = await collecting
        assert outcome.run_id == run_id and outcome.status == "completed"
        assert detail_calls == [1]
        assert second.get_run(run_id)["status"] == "completed"
        assert second.attempted_ids(run_id) == {1}


@pytest.mark.asyncio
async def test_cancelled_collection_releases_lock(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(request):
        if request.url.path.endswith("/galleries"):
            entered.set()
            await release.wait()
            return httpx.Response(200, json=listing([1]))
        return httpx.Response(200, json=detail(1))

    path = tmp_path / "db"
    with Catalog(path) as first, Catalog(path) as second:
        async with client(handler) as api:
            task = asyncio.create_task(Collector(api, first).collect())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            outcome = await Collector(api, second).collect()
        assert outcome.status == "completed" and outcome.fetched == 1


@pytest.mark.asyncio
async def test_inaccessible_listing_recovers_in_later_run_not_same_run(tmp_path):
    path = tmp_path / "db"
    fail_page_two = [True]
    recovered = [False]
    calls = []

    def handler(request):
        if request.url.path.endswith("/galleries"):
            page = int(request.url.params["page"])
            if page == 2 and fail_page_two[0]:
                return httpx.Response(503)
            return httpx.Response(200, json=listing([12], pages=2))
        calls.append(12)
        return httpx.Response(200, json=detail(12)) if recovered[0] else httpx.Response(404)

    with Catalog(path) as catalog:
        async with MetadataClient(user_agent="VibeSearch/0.1", max_retries=0,
                                  transport=httpx.MockTransport(handler),
                                  sleep=lambda _: asyncio.sleep(0), clock=lambda: 0) as api:
            with pytest.raises(Exception):
                await Collector(api, catalog, max_pages=2).collect()
            run_id = catalog.latest_run()["run_id"]
            assert catalog.get(12)["state"] == "inaccessible"
            fail_page_two[0] = False
            recovered[0] = True
            await Collector(api, catalog, max_pages=2).collect()
            assert calls == [12]
            outcome = await Collector(api, catalog, max_pages=2).collect()
        assert outcome.run_id != run_id
        assert calls == [12, 12]
        assert outcome.fetched == 1
        assert catalog.get(12)["state"] == "index_pending"
        assert catalog.get(12)["error_category"] is None


@pytest.mark.asyncio
async def test_inaccessible_refresh_respects_attempt_budget(tmp_path):
    calls = []

    def handler(request):
        if request.url.path.endswith("/galleries"):
            return httpx.Response(200, json=listing([9, 8, 7], pages=3))
        calls.append(int(request.url.path.rsplit("/", 1)[1]))
        return httpx.Response(404)

    with Catalog(tmp_path / "db") as catalog:
        for gallery_id in (9, 8, 7):
            catalog.mark_inaccessible(gallery_id)
        async with client(handler) as api:
            outcome = await Collector(api, catalog, max_items=1, max_pages=3).collect()
        assert outcome.status == "completed" and outcome.fetched == 0
        assert calls == [9, 8]
        assert catalog.attempted_ids(outcome.run_id) == {9, 8}
