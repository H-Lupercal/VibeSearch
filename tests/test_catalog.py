import json
import sqlite3

import pytest

from vibesearch.api_models import GalleryDetail
from vibesearch.catalog import Catalog, content_hash


def record(name="Quiet study", when=10):
    return GalleryDetail.model_validate({
        "id": 12, "media_id": "12", "title": {"english": name, "japanese": "", "pretty": ""},
        "tags": [{"id": 5, "type": "artist", "name": "Example"}],
        "num_pages": 4, "upload_date": when,
    })


def test_raw_durability_hash_and_reindex(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    with Catalog(path) as catalog:
        assert catalog.queue_candidate(12) is True
        assert catalog.queue_candidate(12) is False
        raw = {**record().model_dump(), "extra": "retained"}
        assert catalog.save_detail(record(), raw)
        assert len(catalog.list_pending()) == 1
        assert json.loads(catalog.get(12)["raw_json"])["extra"] == "retained"
        catalog.mark_indexed(12)
        assert not catalog.save_detail(record(when=11))
        assert catalog.get(12)["state"] == "indexed"
        assert content_hash(record(when=11)) == content_hash(record())
        assert catalog.save_detail(record("New title"))
        assert catalog.get(12)["state"] == "index_pending"
    with Catalog(path) as reopened:
        assert reopened.get(12)["state"] == "index_pending"
        assert json.loads(reopened.get(12)["raw_json"])["title"]["english"] == "New title"


def test_404_retains_raw_and_schedules_deletion(tmp_path):
    with Catalog(tmp_path / "db") as catalog:
        catalog.save_detail(record())
        catalog.mark_indexed(12)
        catalog.mark_inaccessible(12)
        assert catalog.get(12)["state"] == "inaccessible"
        assert catalog.get(12)["raw_json"]
        assert [row["id"] for row in catalog.list_deletions()] == [12]
        catalog.mark_deleted(12)
        assert catalog.list_deletions() == []
        assert catalog.save_detail(record())
        assert catalog.get(12)["state"] == "index_pending"


def test_checkpoint_queue_is_durable_and_deduplicated(tmp_path):
    path = tmp_path / "db"
    with Catalog(path) as catalog:
        run = catalog.start_run()
        catalog.set_ceiling(run, 30)
        assert catalog.queue_page(run, 1, [30, 29, 29]) == 2
        assert catalog.queue_page(run, 2, [29, 28]) == 1
        assert catalog.get_run(run)["last_scanned_page"] == 2
    with Catalog(path) as catalog:
        assert catalog.list_candidates(ceiling_id=30) == [30, 29, 28]
        assert catalog.counts()["queued"] == 3
        assert catalog.get_run(run)["ceiling_id"] == 30
        assert catalog.db.execute("SELECT count(*) FROM checkpoints WHERE run_id=?", (run,)).fetchone()[0] == 2


def test_run_detail_counter_and_raw_save_roll_back_together(tmp_path):
    with Catalog(tmp_path / "db") as catalog:
        run_id = catalog.start_run()
        catalog.queue_candidate(12)
        catalog.db.execute("""CREATE TRIGGER reject_counter BEFORE UPDATE OF fetched_count ON runs
            BEGIN SELECT RAISE(ABORT, 'counter failure'); END""")
        with pytest.raises(sqlite3.IntegrityError):
            catalog.save_detail(record(), run_id=run_id)
        assert catalog.get(12)["state"] == "queued"
        assert catalog.get(12)["raw_json"] is None
        assert catalog.get_run(run_id)["fetched_count"] == 0
        assert catalog.attempted_ids(run_id) == set()
        catalog.db.execute("DROP TRIGGER reject_counter")
        catalog.save_detail(record(), run_id=run_id)
        assert catalog.get_run(run_id)["fetched_count"] == 1
        assert catalog.attempted_ids(run_id) == {12}


def test_existing_catalog_adds_run_accounting_without_losing_checkpoint(tmp_path):
    path = tmp_path / "db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE runs (run_id TEXT PRIMARY KEY, ceiling_id INTEGER,
            last_scanned_page INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        db.execute("INSERT INTO runs VALUES ('prior', 12, 3, 'paused', 'old', 'old')")
    with Catalog(path) as catalog:
        prior = catalog.get_run("prior")
        assert prior["last_scanned_page"] == 3
        assert prior["fetched_count"] == 0 and prior["item_limit"] is None
        assert catalog.set_item_limit("prior", 5) == 5
        assert catalog.attempted_ids("prior") == set()


def test_invalid_attempts_do_not_increment_valid_detail_count(tmp_path):
    path = tmp_path / "db"
    with Catalog(path) as catalog:
        run_id = catalog.start_run()
        for gallery_id in (13, 14):
            catalog.queue_candidate(gallery_id)
        catalog.mark_inaccessible(13, run_id=run_id)
        catalog.record_error(14, "schema", run_id=run_id)
        assert catalog.get_run(run_id)["fetched_count"] == 0
        assert catalog.attempted_ids(run_id) == {13, 14}
    with Catalog(path) as reopened:
        assert reopened.get_run(run_id)["fetched_count"] == 0
        assert reopened.attempted_ids(run_id) == {13, 14}
