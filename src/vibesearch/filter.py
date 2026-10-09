"""Exact typed-tag filters over validated local catalog projections."""

from __future__ import annotations

import json
import unicodedata

from .catalog import SQLITE_MAX

FIELDS = {
    "tag": "tag",
    "tags": "tag",
    "tags_any": "tag",
    "tags_all": "tag",
    "artist": "artist",
    "character": "character",
    "parody": "parody",
    "group": "group",
    "language": "language",
    "category": "category",
    "exclude_tags": "tag",
    "exclude_artists": "artist",
}
ELIGIBLE = "g.raw_json IS NOT NULL AND g.validation_error IS NULL AND g.upload_date IS NOT NULL AND g.state NOT IN ('queued','inaccessible')"


def normalize(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


class FilterService:
    def __init__(self, catalog):
        self.catalog = catalog

    def _selection(self, value, kind):
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("Tag selection must be an integer ID or name")
        if isinstance(value, int):
            if not -SQLITE_MAX - 1 <= value <= SQLITE_MAX:
                return []  # Unknown identity; never bind an oversized Python int.
            return [
                r[0]
                for r in self.catalog.db.execute(
                    "SELECT tag_id FROM tags WHERE type=? AND tag_id=?", (kind, value)
                )
            ]
        return [
            r[0]
            for r in self.catalog.db.execute(
                "SELECT tag_id FROM tags WHERE type=? AND name_norm=?",
                (kind, normalize(value)),
            )
        ]

    def _where(self, filters):
        clauses, params = [ELIGIBLE], []
        for field, values in (filters or {}).items():
            if field not in FIELDS:
                raise ValueError("Unsupported filter field")
            if not isinstance(values, (list, tuple)):
                values = [values]
            if len(values) > 100:
                raise ValueError("Too many filter selections")
            if not values:
                continue
            groups = [self._selection(v, FIELDS[field]) for v in values]
            if field == "tags_all":
                for ids in groups:
                    if not ids:
                        clauses.append("0")
                        continue
                    marks = ",".join("?" for _ in ids)
                    clauses.append(
                        f"EXISTS(SELECT 1 FROM gallery_tags gt WHERE gt.gallery_id=g.id AND gt.tag_id IN ({marks}))"
                    )
                    params.extend(ids)
            else:
                ids = sorted({i for group in groups for i in group})
                if not ids:
                    if not field.startswith("exclude_"):
                        clauses.append("0")
                    continue
                marks = ",".join("?" for _ in ids)
                expr = f"EXISTS(SELECT 1 FROM gallery_tags gt WHERE gt.gallery_id=g.id AND gt.tag_id IN ({marks}))"
                clauses.append("NOT " + expr if field.startswith("exclude_") else expr)
                params.extend(ids)
        return " AND ".join(clauses), params

    def eligible_ids(self, filters=None):
        where, args = self._where(filters)
        return {
            r[0]
            for r in self.catalog.db.execute(
                "SELECT g.id FROM galleries g WHERE " + where, args
            )
        }

    def list(self, filters=None, *, limit=50, offset=0, display="full"):
        if not 1 <= limit <= 100 or offset < 0 or display not in ("full", "id-only"):
            raise ValueError("Invalid listing options")
        where, args = self._where(filters)
        total = self.catalog.db.execute(
            "SELECT count(*) FROM galleries g WHERE " + where, args
        ).fetchone()[0]
        rows = self.catalog.db.execute(
            "SELECT g.* FROM galleries g WHERE "
            + where
            + " ORDER BY g.upload_date DESC,g.id DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
        items = []
        for r in rows:
            item = {
                "gallery_id": r["id"],
                "num_pages": r["num_pages"],
                "upload_date": r["upload_date"],
                "readable": bool(r["readable"]),
                "state": r["state"],
            }
            if display == "full":
                d = json.loads(r["raw_json"])
                titles = d["title"]
                item["title_display"] = next(
                    (
                        titles.get(k)
                        for k in ("pretty", "english", "japanese")
                        if titles.get(k)
                    ),
                    str(r["id"]),
                )
                item["tags"] = d["tags"]
            items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def suggest(self, field, prefix="", limit=20):
        if (
            field not in FIELDS
            or field.startswith("exclude_")
            or not 1 <= limit <= 50
            or len(prefix) > 500
        ):
            raise ValueError("Invalid autocomplete options")
        escaped = (
            normalize(prefix)
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
            + "%"
        )
        sql = (
            "SELECT t.tag_id,t.type,t.name,count(DISTINCT g.id) AS n FROM tags t JOIN gallery_tags gt ON gt.tag_id=t.tag_id JOIN galleries g ON g.id=gt.gallery_id WHERE "
            + ELIGIBLE
            + " AND t.type=? AND t.name_norm LIKE ? ESCAPE '\\' GROUP BY t.tag_id ORDER BY t.name_norm,t.tag_id LIMIT ?"
        )
        return [
            {"id": r["tag_id"], "type": r["type"], "name": r["name"], "count": r["n"]}
            for r in self.catalog.db.execute(sql, (FIELDS[field], escaped, limit))
        ]
