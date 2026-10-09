"""Single-worker, paced API v2 client for metadata requests only."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import time
from collections.abc import Awaitable, Callable

import httpx
from pydantic import ValidationError

from .api_models import GalleryDetail, GalleryListResponse


class APIError(Exception):
    """A request failed; the message never contains request headers or credentials."""


class APIStatusError(APIError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Metadata API returned HTTP {status_code}")


class DetailNotFound(APIStatusError):
    def __init__(self):
        super().__init__(404)


class APISchemaError(APIError):
    pass


class APIRateLimited(APIStatusError):
    def __init__(self):
        super().__init__(429)


class MetadataClient:
    def __init__(
        self,
        *,
        origin: str = "https://nhentai.net",
        api_key: str | None = None,
        user_agent: str,
        list_per_minute: float | None = None,
        detail_per_minute: float | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 15.0,
        max_retries: int = 3,
        max_retry_delay: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("user_agent must be nonempty")
        if origin.rstrip("/").endswith("/api/v2"):
            raise ValueError("origin must not include /api/v2")
        if not origin.startswith("https://") and transport is None:
            raise ValueError("origin must use HTTPS")
        if max_retries < 0 or max_retry_delay <= 0:
            raise ValueError("invalid retry configuration")
        self.rates = {
            "list": list_per_minute if list_per_minute is not None else (24 if api_key else 12),
            "detail": detail_per_minute if detail_per_minute is not None else (36 if api_key else 16),
        }
        if any(not 0 < rate <= (30 if kind == "list" else 45) for kind, rate in self.rates.items()):
            raise ValueError("request rates must be positive and within published ceilings")
        self.clock, self.wall_clock, self.sleep, self.jitter = clock, wall_clock, sleep, jitter
        self.max_retries, self.max_retry_delay = max_retries, max_retry_delay
        self._next_allowed = {"list": 0.0, "detail": 0.0}
        headers = {"User-Agent": user_agent}
        if api_key:
            headers["Authorization"] = f"Key {api_key}"
        self._client = httpx.AsyncClient(
            base_url=origin.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            transport=transport,
            follow_redirects=False,
        )

    async def __aenter__(self) -> MetadataClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _pace(self, kind: str) -> None:
        remaining = self._next_allowed[kind] - self.clock()
        if remaining > 0:
            await self.sleep(remaining)
        self._next_allowed[kind] = self.clock() + 60 / self.rates[kind]

    def _retry_delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                seconds = float(retry_after)
                if 0 <= seconds < float("inf"):
                    return seconds
            except ValueError:
                try:
                    target = parsedate_to_datetime(retry_after)
                    if target.tzinfo is not None:
                        return max(0.0, (target - self.wall_clock()).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(self.max_retry_delay, 2**attempt + self.jitter())

    async def _get(self, path: str, *, kind: str, params: dict[str, int] | None = None) -> dict:
        for attempt in range(self.max_retries + 1):
            await self._pace(kind)
            try:
                response = await self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == self.max_retries:
                    raise APIError("Metadata API transport failure") from exc
                await self.sleep(self._retry_delay(attempt))
                continue
            if response.status_code == 404 and kind == "detail":
                raise DetailNotFound()
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == self.max_retries:
                    if response.status_code == 429:
                        raise APIRateLimited()
                    raise APIStatusError(response.status_code)
                await self.sleep(self._retry_delay(attempt, response.headers.get("Retry-After") if response.status_code == 429 else None))
                continue
            if response.status_code != 200:
                raise APIStatusError(response.status_code)
            try:
                payload = response.json()
            except ValueError as exc:
                raise APISchemaError("Invalid metadata JSON") from exc
            if not isinstance(payload, dict):
                raise APISchemaError("Expected a metadata object")
            return payload
        raise AssertionError("unreachable retry state")

    async def list_galleries(self, page: int, per_page: int = 25) -> GalleryListResponse:
        if page < 1 or not 1 <= per_page <= 100:
            raise ValueError("invalid pagination")
        raw = await self._get("/api/v2/galleries", kind="list", params={"page": page, "per_page": per_page})
        try:
            return GalleryListResponse.model_validate(raw)
        except ValidationError as exc:
            raise APISchemaError("Invalid gallery list schema") from exc

    async def get_detail(self, gallery_id: int) -> tuple[GalleryDetail, dict]:
        if gallery_id < 1:
            raise ValueError("gallery_id must be positive")
        raw = await self._get(f"/api/v2/galleries/{gallery_id}", kind="detail")
        try:
            detail = GalleryDetail.model_validate(raw)
        except ValidationError as exc:
            raise APISchemaError("Invalid gallery detail schema") from exc
        if detail.id != gallery_id:
            raise APISchemaError("Gallery detail ID does not match request")
        return detail, raw
