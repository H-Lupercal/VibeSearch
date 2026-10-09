"""Bounded, resumable metadata collection over a changing page listing."""

from __future__ import annotations

from dataclasses import dataclass

from filelock import FileLock, Timeout

from .api_client import APIRateLimited, APISchemaError, DetailNotFound, MetadataClient
from .catalog import Catalog


@dataclass(frozen=True)
class CollectionResult:
    run_id: str
    scanned: int = 0
    deduplicated: int = 0
    fetched: int = 0
    skipped: int = 0
    inaccessible: int = 0
    pending: int = 0
    pages_scanned: int = 0
    status: str = "completed"


class Collector:
    def __init__(
        self, client: MetadataClient, catalog: Catalog, *,
        max_items: int = 100, max_pages: int = 5, per_page: int = 25,
    ) -> None:
        if not 1 <= max_items <= 100 or not 1 <= max_pages <= 5 or not 1 <= per_page <= 100:
            raise ValueError("collection caps must be within 1..100 items, 1..5 pages, 1..100 per page")
        self.client, self.catalog = client, catalog
        self.max_items, self.max_pages, self.per_page = max_items, max_pages, per_page

    async def collect(self, *, run_id: str | None = None) -> CollectionResult:
        # A run is selected and updated across awaits; SQLite transactions alone
        # cannot prevent another connection from collecting the same run.
        lock = FileLock(str(self.catalog.path.resolve()) + ".collector.lock")
        try:
            with lock.acquire(timeout=0):
                return await self._collect_locked(run_id=run_id)
        except Timeout as exc:
            raise RuntimeError("collection already in progress for this catalog") from exc

    async def _collect_locked(self, *, run_id: str | None = None) -> CollectionResult:
        if run_id is None:
            latest = self.catalog.latest_run()
            # Older collectors left exhausted runs paused; retire them so the
            # durable queue can be processed under a fresh bounded run.
            if latest is not None and latest["item_limit"] is not None and (
                latest["fetched_count"] >= latest["item_limit"]
                or len(self.catalog.attempted_ids(latest["run_id"])) >= 2 * latest["item_limit"]
            ):
                if latest["status"] != "completed":
                    self.catalog.set_run_status(latest["run_id"], "completed")
                latest = None
            run_id = latest["run_id"] if latest is not None and latest["status"] in {"running", "paused", "failed"} else self.catalog.start_run()
        run = self.catalog.get_run(run_id)
        if run is None:
            raise ValueError("unknown run")
        if run["status"] == "completed":
            raise ValueError("completed run cannot be resumed")
        item_limit = self.catalog.set_item_limit(run_id, self.max_items)
        attempted = self.catalog.attempted_ids(run_id)
        # The valid-detail cap and the safety budget are independent. Outcomes
        # (including 404/schema errors) are durable across interrupted runs.
        attempt_limit = 2 * item_limit
        if run["fetched_count"] >= item_limit or len(attempted) >= attempt_limit:
            self.catalog.set_run_status(run_id, "completed")
            raise ValueError("completed run cannot be resumed")
        self.catalog.set_run_status(run_id, "running")
        scanned = deduplicated = skipped = inaccessible = pages_scanned = 0
        fetched = run["fetched_count"]
        ceiling = run["ceiling_id"]

        def at_cap() -> bool:
            return fetched >= item_limit or len(attempted) >= attempt_limit

        async def details(listed_ids: list[int] = ()) -> None:
            nonlocal fetched, skipped, inaccessible
            # Queued work precedes bounded refresh of already-known listing IDs.
            queued = self.catalog.list_candidates(ceiling_id=ceiling)
            refresh = [gallery_id for gallery_id in listed_ids
                       if (row := self.catalog.get(gallery_id)) is not None
                       and row["state"] in {"fetched", "index_pending", "indexed", "inaccessible"}]
            for gallery_id in queued + refresh:
                if at_cap():
                    break
                if gallery_id in attempted:
                    continue
                try:
                    detail, raw = await self.client.get_detail(gallery_id)
                except DetailNotFound:
                    self.catalog.mark_inaccessible(gallery_id, run_id=run_id)
                    attempted.add(gallery_id)
                    inaccessible += 1
                    continue
                except APISchemaError:
                    self.catalog.record_error(gallery_id, "schema", run_id=run_id)
                    attempted.add(gallery_id)
                    skipped += 1
                    continue
                self.catalog.save_detail(detail, raw, run_id=run_id)
                attempted.add(gallery_id)
                fetched += 1

        try:
            # The durable queue has priority over page enumeration after a crash.
            await details()
            if not at_cap():
                start_page = max(1, run["last_scanned_page"] - 2) if run["last_scanned_page"] else 1
                for page in range(start_page, start_page + self.max_pages):
                    listing = await self.client.list_galleries(page, self.per_page)
                    pages_scanned += 1
                    scanned += len(listing.result)
                    if ceiling is None:
                        ceiling = max((item.id for item in listing.result), default=0)
                        self.catalog.set_ceiling(run_id, ceiling)
                    eligible = [item.id for item in listing.result if item.id <= ceiling]
                    # Existing rows and repeated IDs are not requeued.
                    added = self.catalog.queue_page(run_id, page, eligible)
                    deduplicated += len(eligible) - added
                    skipped += len(listing.result) - len(eligible)
                    await details(eligible)
                    if at_cap() or page >= listing.num_pages or not listing.result:
                        break
        except APIRateLimited:
            self.catalog.set_run_status(run_id, "paused")
            raise
        except Exception:
            self.catalog.set_run_status(run_id, "failed")
            raise
        # Only transport interruptions pause/fail. Reaching either bound ends
        # this run, leaving queued IDs for the next invocation.
        status = "completed"
        self.catalog.set_run_status(run_id, status)
        return CollectionResult(
            run_id=run_id, scanned=scanned, deduplicated=deduplicated,
            fetched=fetched, skipped=skipped, inaccessible=inaccessible,
            pending=len(self.catalog.list_candidates(ceiling_id=ceiling)),
            pages_scanned=pages_scanned, status=status,
        )
