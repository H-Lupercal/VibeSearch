"""Normalized local filtering and migration contracts."""

import json
import sqlite3

import pytest

from vibesearch.api_models import GalleryDetail
from vibesearch.catalog import Catalog
from vibesearch.filter import FilterService


def raw(gid=1, tags=None, date=100):
    return dict(
        id=gid,
        media_id=str(gid),
        title=dict(english="Quiet", pretty="Quiet", japanese=""),
        tags=tags or [],
        num_pages=1,
        upload_date=date,
        pages=[dict(number=1, path=f"galleries/{gid}/1.png", width=10, height=10)],
    )


def save(c, gid, tags, date=100):
    d = raw(gid, tags, date)
    c.save_detail(GalleryDetail.model_validate(d), d)


def tag(i, name, kind="tag"):
    return dict(id=i, name=name, type=kind)


def test_filters_semantics_and_privacy(tmp_path):
    with Catalog(tmp_path / "db") as c:
        save(
            c,
            1,
            [
                tag(1, "Rain"),
                tag(2, "Cozy"),
                tag(10, "A", "artist"),
                tag(20, "english", "language"),
            ],
        )
        save(
            c,
            2,
            [tag(2, "Cozy"), tag(11, "B", "artist"), tag(20, "english", "language")],
        )
        save(
            c,
            3,
            [tag(1, "Rain"), tag(10, "A", "artist"), tag(21, "japanese", "language")],
        )
        f = FilterService(c)
        assert f.eligible_ids({"artist": ["A", "B"], "language": ["english"]}) == {1, 2}
        assert f.eligible_ids({"tags_all": ["Rain", "Cozy"]}) == {1}
        assert f.eligible_ids({"tags_any": [1, 2], "exclude_tags": [1]}) == {2}
        assert f.eligible_ids({"tags_all": [1], "exclude_tags": [1]}) == set()
        assert f.eligible_ids({"tags_any": [999, 2]}) == {1, 2}
        assert f.eligible_ids({"tags_all": [999, 2]}) == set()
        assert f.eligible_ids({"tags_any": []}) == {1, 2, 3}
        assert f.eligible_ids({"artist": [2]}) == set()
        assert f.eligible_ids({"exclude_artists": [999]}) == {1, 2, 3}
        ids = f.list(display="id-only")
        assert [i["gallery_id"] for i in ids["items"]] == [3, 2, 1]
        assert all("title_display" not in i and "tags" not in i for i in ids["items"])
        assert f.list(limit=1, offset=1)["items"][0]["gallery_id"] == 2
        c.mark_inaccessible(1)
        assert f.eligible_ids() == {2, 3}


def test_duplicate_names_suggestions_escape_rename(tmp_path):
    with Catalog(tmp_path / "db") as c:
        save(c, 1, [tag(1, "Rain"), tag(3, "a_b")])
        save(c, 2, [tag(2, "RAIN"), tag(4, "axb")])
        f = FilterService(c)
        assert f.eligible_ids({"tags_all": ["rain"]}) == {1, 2}
        assert [i["id"] for i in f.suggest("tag", "a_")] == [3]
        assert len(f.suggest("tag", "rain")) == 2
        save(c, 1, [tag(1, "Sunny")])
        assert f.eligible_ids({"tags_any": ["rain"]}) == {2}
        assert f.suggest("tag", "sun")[0]["count"] == 1
        with pytest.raises(ValueError):
            f.suggest("unknown", "")
        with pytest.raises(ValueError):
            f.list(limit=101)
        with pytest.raises(ValueError):
            f.eligible_ids({"not_a_field": [1]})
        with pytest.raises(sqlite3.IntegrityError):
            c.db.execute("INSERT INTO gallery_tags VALUES(999,1)")


def test_restart_migration_preserves_invalid_access_state_and_queued(tmp_path):
    path = tmp_path / "db"
    with Catalog(path) as c:
        save(c, 1, [tag(1, "Rain")])
        c.queue_candidate(2)
        c.db.execute("UPDATE galleries SET raw_json='invalid' WHERE id=1")
        c.db.execute("DROP TABLE gallery_tags")
        c.db.execute("DROP TABLE tags")
        c.db.execute("DELETE FROM schema_version")
        c.db.commit()
    with Catalog(path) as c:
        assert c.get(1)["state"] == "index_pending"
        assert c.get(1)["validation_error"]
        assert c.get(1)["raw_json"] == "invalid"
        assert c.get(2)["upload_date"] is None
        assert FilterService(c).eligible_ids() == set()
        c.save_detail(GalleryDetail.model_validate(raw()), raw())
        assert c.get(1)["validation_error"] is None
        assert FilterService(c).eligible_ids() == {1}
    with Catalog(path) as c:
        assert FilterService(c).eligible_ids() == {1}


def test_missing_pages_remain_searchable_and_raw_unchanged(tmp_path):
    d = raw()
    d.pop("pages")
    with Catalog(tmp_path / "db") as c:
        c.save_detail(GalleryDetail.model_validate(d), d)
        assert not c.get(1)["readable"]
        assert c.get(1)["validation_error"] is None
        assert json.loads(c.get(1)["raw_json"]) == d
        assert FilterService(c).eligible_ids() == {1}
