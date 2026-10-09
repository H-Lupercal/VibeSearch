"""Reader refresh is bounded and preserves metadata/index consistency."""

import pytest

from vibesearch.api_client import DetailNotFound
from vibesearch.api_models import GalleryDetail
from vibesearch.catalog import Catalog
from vibesearch.reader_refresh import refresh_metadata
from test_filters import raw


@pytest.mark.asyncio
async def test_refresh_failures_consume_cap_and_changes_schedule_index(tmp_path):
    with Catalog(tmp_path / "db") as c:
        for gid in range(1, 5):
            d = raw(gid)
            d.pop("pages")
            c.save_detail(GalleryDetail.model_validate(d), d)

        class Client:
            calls = []

            async def get_detail(self, gid):
                self.calls.append(gid)
                if gid == 1:
                    raise DetailNotFound()
                d = raw(gid)
                d["title"]["pretty"] = "Updated"
                return GalleryDetail.model_validate(d), d

        client = Client()
        result = await refresh_metadata(c, client, max_items=2)
        assert result["attempted"] == 2 and client.calls == [1, 2]
        assert c.get(1)["state"] == "inaccessible"
        assert c.get(2)["readable"] and c.get(2)["state"] == "index_pending"
        assert not c.get(3)["readable"]
        with pytest.raises(ValueError):
            await refresh_metadata(c, client, max_items=101)


@pytest.mark.asyncio
async def test_refresh_interruption_pins_queue_and_attempt_budget(tmp_path):
    with Catalog(tmp_path / "db") as c:
        for gid in range(1, 5):
            d = raw(gid)
            d.pop("pages")
            c.save_detail(GalleryDetail.model_validate(d), d)

        class Interrupted:
            async def get_detail(self, gid):
                raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            await refresh_metadata(c, Interrupted(), max_items=2)

        class Recovered:
            calls = []

            async def get_detail(self, gid):
                self.calls.append(gid)
                d = raw(gid)
                return GalleryDetail.model_validate(d), d

        client = Recovered()
        r = await refresh_metadata(c, client, max_items=100)
        assert client.calls == [2]
        assert r["attempted"] == 2
