import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from vibesearch.api_client import APIRateLimited, APISchemaError, APIStatusError, DetailNotFound, MetadataClient


def detail(i):
    return {"id": i, "media_id": str(i), "title": {"english": "Quiet study", "pretty": "Quiet study"}, "tags": [], "num_pages": 4, "upload_date": 1}


@pytest.mark.asyncio
async def test_list_and_detail_are_distinct_and_paths_are_not_doubled():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/galleries"):
            return httpx.Response(200, json={"result": [{"id": 8, "media_id": "8", "english_title": "Quiet study", "num_pages": 4, "tag_ids": [3]}], "num_pages": 1, "per_page": 25})
        return httpx.Response(200, json={**detail(8), "future_field": "allowed"})

    async with MetadataClient(user_agent="VibeSearch/0.1 contact@example.org", transport=httpx.MockTransport(handler), sleep=lambda _: asyncio.sleep(0), clock=lambda: 0) as client:
        listing = await client.list_galleries(1)
        full, raw = await client.get_detail(listing.result[0].id)
    assert full.title.english == "Quiet study" and raw["future_field"] == "allowed"
    assert [request.url.path for request in requests] == ["/api/v2/galleries", "/api/v2/galleries/8"]
    assert dict(requests[0].url.params) == {"page": "1", "per_page": "25"}


@pytest.mark.asyncio
async def test_retry_after_and_pacing_with_mock_clock():
    now = [0.0]
    waits = []
    calls = [0]

    async def sleep(delay):
        waits.append(delay)
        now[0] += delay

    def handler(request):
        calls[0] += 1
        if calls[0] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=detail(1))

    async with MetadataClient(user_agent="VibeSearch/0.1", transport=httpx.MockTransport(handler), clock=lambda: now[0], sleep=sleep) as client:
        await client.get_detail(1)
    assert calls[0] == 2
    assert waits == [7]


@pytest.mark.asyncio
async def test_http_date_and_retry_ceiling():
    now = [0.0]
    waits = []

    async def sleep(delay):
        waits.append(delay)
        now[0] += delay

    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "Thu, 01 Jan 2026 00:00:09 GMT"})

    async with MetadataClient(user_agent="VibeSearch/0.1", transport=httpx.MockTransport(handler), clock=lambda: now[0], sleep=sleep, wall_clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc), max_retries=2) as client:
        with pytest.raises(APIRateLimited):
            await client.get_detail(1)
    assert waits == [9, 9]


@pytest.mark.asyncio
async def test_404_schema_auth_and_key_redaction():
    async def request_status(status, payload=None):
        async with MetadataClient(user_agent="VibeSearch/0.1", api_key="private-token", transport=httpx.MockTransport(lambda req: httpx.Response(status, json=payload))) as client:
            return await client.get_detail(1)

    with pytest.raises(DetailNotFound):
        await request_status(404)
    for status in (400, 401, 403, 422):
        with pytest.raises(APIStatusError) as exc:
            await request_status(status)
        assert exc.value.status_code == status and "private-token" not in str(exc.value)
    with pytest.raises(APISchemaError):
        await request_status(200, {"id": 1})
    with pytest.raises(APISchemaError):
        await request_status(200, detail(2))


def test_reject_double_prefix_and_invalid_bounds():
    with pytest.raises(ValueError):
        MetadataClient(origin="https://example.org/api/v2", user_agent="VibeSearch/0.1")
    with pytest.raises(ValueError):
        MetadataClient(user_agent="VibeSearch/0.1", detail_per_minute=46)

@pytest.mark.asyncio
async def test_optional_list_per_page_and_nullable_japanese_title():
    def handler(request):
        if request.url.path.endswith("/galleries"):
            return httpx.Response(200, json={"result": [], "num_pages": 0})
        return httpx.Response(200, json={**detail(1), "title": {"english": "A", "pretty": "A", "japanese": None}})

    async with MetadataClient(user_agent="VibeSearch/0.1", transport=httpx.MockTransport(handler)) as client:
        assert (await client.list_galleries(1)).per_page == 25
        parsed, _ = await client.get_detail(1)
        assert parsed.title.japanese is None

    for missing in ("english", "pretty"):
        invalid = detail(1)
        del invalid["title"][missing]
        async with MetadataClient(user_agent="VibeSearch/0.1", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=invalid))) as client:
            with pytest.raises(APISchemaError):
                await client.get_detail(1)
