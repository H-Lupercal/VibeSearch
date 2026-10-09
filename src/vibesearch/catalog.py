"""Durable canonical metadata, queue, and collection checkpoints."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .api_models import GalleryDetail
from .reader import ReaderError, reader_bundle

SQLITE_MAX = (1 << 63) - 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def content_hash(detail: GalleryDetail) -> str:
    """Hash only source fields used by the embedding text contract, in source order."""
    fields = {
        "title": detail.title.model_dump(),
        "tags": [tag.model_dump() for tag in detail.tags],
    }
    encoded = json.dumps(
        fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Catalog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS galleries (
                id INTEGER PRIMARY KEY CHECK(id > 0),
                state TEXT NOT NULL CHECK(state IN ('queued','fetched','index_pending','indexed','inaccessible')),
                raw_json TEXT, content_hash TEXT, indexed_hash TEXT,
                error_category TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, ceiling_id INTEGER, last_scanned_page INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                fetched_count INTEGER NOT NULL DEFAULT 0, item_limit INTEGER
            );
            CREATE TABLE IF NOT EXISTS run_attempts (
                run_id TEXT NOT NULL REFERENCES runs(run_id), gallery_id INTEGER NOT NULL,
                PRIMARY KEY (run_id, gallery_id)
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT NOT NULL REFERENCES runs(run_id), page INTEGER NOT NULL,
                queued INTEGER NOT NULL, scanned_at TEXT NOT NULL,
                PRIMARY KEY (run_id, page)
            );
        """)
        # Keep checkpoints in databases created before run accounting existed.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
        if "fetched_count" not in columns:
            self.db.execute(
                "ALTER TABLE runs ADD COLUMN fetched_count INTEGER NOT NULL DEFAULT 0"
            )
        if "item_limit" not in columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN item_limit INTEGER")
        self.db.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Atomic, repeatable v2 projections; never infer source deletion."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            version = self.db.execute("SELECT version FROM schema_version").fetchone()
            if version and version[0] >= 3:
                return
            columns = {
                row[1] for row in self.db.execute("PRAGMA table_info(galleries)")
            }
            additions = {
                "upload_date": "INTEGER",
                "num_pages": "INTEGER",
                "readable": "INTEGER NOT NULL DEFAULT 0",
                "validation_error": "TEXT",
                "reader_error": "TEXT",
                "pages_json": "TEXT",
                "detail_observed_at": "TEXT",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    self.db.execute(
                        f"ALTER TABLE galleries ADD COLUMN {name} {declaration}"
                    )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS tags (tag_id INTEGER PRIMARY KEY, type TEXT NOT NULL, "
                "name TEXT NOT NULL, name_norm TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS tags_lookup ON tags(type,name_norm)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS gallery_tags (gallery_id INTEGER NOT NULL REFERENCES galleries(id), "
                "tag_id INTEGER NOT NULL REFERENCES tags(tag_id), PRIMARY KEY(gallery_id,tag_id))"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS gallery_tags_by_tag ON gallery_tags(tag_id,gallery_id)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS gallery_sort ON galleries(upload_date DESC,id DESC)"
            )
            # Old schemas did not distinguish detail fetches from index updates.
            # updated_at is the best available historical observation fallback.
            self.db.execute("UPDATE galleries SET detail_observed_at=updated_at "
                            "WHERE raw_json IS NOT NULL AND detail_observed_at IS NULL")
            for row in self.db.execute(
                "SELECT id,raw_json FROM galleries WHERE raw_json IS NOT NULL ORDER BY detail_observed_at,id"
            ).fetchall():
                self._project(row["id"], row["raw_json"])
            self.db.execute("DELETE FROM schema_version")
            self.db.execute("INSERT INTO schema_version VALUES(3)")

    def _project(self, gallery_id: int, encoded: str) -> None:
        """Rebuild filter/reader projections inside the caller's transaction."""
        self.db.execute("DELETE FROM gallery_tags WHERE gallery_id=?", (gallery_id,))
        try:
            raw = json.loads(encoded)
            detail = GalleryDetail.model_validate(raw)
            if detail.id != gallery_id:
                raise ValueError("identity mismatch")
            numbers = [detail.id, detail.upload_date, detail.num_pages, *(tag.id for tag in detail.tags)]
            if any(not -SQLITE_MAX - 1 <= value <= SQLITE_MAX for value in numbers):
                raise ValueError("integer outside storage range")
        except (ValueError, TypeError):
            self.db.execute(
                "UPDATE galleries SET validation_error='invalid_metadata',readable=0,pages_json=NULL "
                "WHERE id=?",
                (gallery_id,),
            )
            return
        for tag in detail.tags:
            name_norm = unicodedata.normalize("NFC", tag.name).casefold()
            self.db.execute(
                "INSERT INTO tags VALUES(?,?,?,?) ON CONFLICT(tag_id) DO UPDATE SET "
                "type=excluded.type,name=excluded.name,name_norm=excluded.name_norm",
                (tag.id, tag.type, tag.name, name_norm),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO gallery_tags VALUES(?,?)", (gallery_id, tag.id)
            )
        pages = raw.get("pages")
        try:
            reader_bundle(raw, display="id-only")
            valid = True
        except ReaderError:
            valid = False
        self.db.execute(
            "UPDATE galleries SET upload_date=?,num_pages=?,validation_error=NULL,readable=?,"
            "reader_error=?,pages_json=? WHERE id=?",
            (
                detail.upload_date,
                detail.num_pages,
                int(valid),
                None if valid else "invalid_descriptors",
                json.dumps(pages) if valid else None,
                gallery_id,
            ),
        )

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def queue_candidate(self, gallery_id: int) -> bool:
        if not 0 < gallery_id <= SQLITE_MAX:
            raise ValueError("gallery_id must be positive")
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO galleries(id,state,created_at,updated_at) VALUES(?,?,?,?)",
                (gallery_id, "queued", _now(), _now()),
            )
        return cursor.rowcount == 1

    def queue_page(self, run_id: str, page: int, ids: list[int]) -> int:
        """Atomically persist candidates and the list-page checkpoint."""
        if page < 1 or any(not 0 < i <= SQLITE_MAX for i in ids):
            raise ValueError("invalid page or gallery ID")
        added = 0
        with self.db:
            if self.get_run(run_id) is None:
                raise ValueError("unknown run")
            for gallery_id in ids:
                added += self.db.execute(
                    "INSERT OR IGNORE INTO galleries(id,state,created_at,updated_at) VALUES(?,?,?,?)",
                    (gallery_id, "queued", _now(), _now()),
                ).rowcount
            self.db.execute(
                "INSERT INTO checkpoints(run_id,page,queued,scanned_at) VALUES(?,?,?,?) "
                "ON CONFLICT(run_id,page) DO UPDATE SET queued=excluded.queued, scanned_at=excluded.scanned_at",
                (run_id, page, added, _now()),
            )
            self.db.execute(
                "UPDATE runs SET last_scanned_page=max(last_scanned_page, ?),updated_at=? WHERE run_id=?",
                (page, _now(), run_id),
            )
        return added

    def save_detail(
        self,
        detail: GalleryDetail,
        raw: dict[str, Any] | None = None,
        *,
        run_id: str | None = None,
    ) -> bool:
        """Persist raw JSON first; return whether index work is needed."""
        raw = raw if raw is not None else detail.model_dump()
        if not 0 < detail.id <= SQLITE_MAX:
            raise ValueError("gallery_id outside storage range")
        if detail.model_dump() != GalleryDetail.model_validate(raw).model_dump():
            raise ValueError("raw/detail mismatch")
        encoded = json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = content_hash(detail)
        with self.db:
            if run_id is not None:
                self._record_attempt(run_id, detail.id)
            row = self.get(detail.id)
            pending = (
                row is None
                or row["indexed_hash"] != digest
                or row["state"] == "inaccessible"
            )
            state = "index_pending" if pending else "indexed"
            self.db.execute(
                "INSERT INTO galleries(id,state,raw_json,content_hash,indexed_hash,error_category,created_at,updated_at,detail_observed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "state=excluded.state,raw_json=excluded.raw_json,content_hash=excluded.content_hash,"
                "error_category=NULL,updated_at=excluded.updated_at,detail_observed_at=excluded.detail_observed_at",
                (detail.id, state, encoded, digest, None, None, _now(), _now(), _now()),
            )
            self._project(detail.id, encoded)
            if run_id is not None:
                self.db.execute(
                    "UPDATE runs SET fetched_count=fetched_count+1,updated_at=? WHERE run_id=?",
                    (_now(), run_id),
                )
        return pending

    def _record_attempt(self, run_id: str, gallery_id: int) -> None:
        """Record an outcome transactionally; interrupted requests remain retryable."""
        self.db.execute(
            "INSERT INTO run_attempts(run_id,gallery_id) VALUES(?,?)",
            (run_id, gallery_id),
        )

    def attempted_ids(self, run_id: str) -> set[int]:
        return {
            row[0]
            for row in self.db.execute(
                "SELECT gallery_id FROM run_attempts WHERE run_id=?", (run_id,)
            )
        }

    def set_item_limit(self, run_id: str, limit: int) -> int:
        """Pin the original run cap; resumption may only lower it."""
        with self.db:
            self.db.execute(
                "UPDATE runs SET item_limit=? WHERE run_id=? AND item_limit IS NULL",
                (limit, run_id),
            )
        run = self.get_run(run_id)
        if run is None:
            raise ValueError("unknown run")
        return min(limit, run["item_limit"])

    def mark_indexed(self, gallery_id: int) -> None:
        with self.db:
            cursor = self.db.execute(
                "UPDATE galleries SET indexed_hash=content_hash,state='indexed',updated_at=? "
                "WHERE id=? AND state='index_pending' AND content_hash IS NOT NULL",
                (_now(), gallery_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("gallery is not pending indexing")

    def mark_inaccessible(self, gallery_id: int, *, run_id: str | None = None) -> None:
        """A confirmed detail 404 requires vector deletion; retain raw metadata."""
        with self.db:
            if run_id is not None:
                self._record_attempt(run_id, gallery_id)
            self.db.execute(
                "INSERT INTO galleries(id,state,error_category,created_at,updated_at) VALUES(?,'inaccessible','not_found',?,?) "
                "ON CONFLICT(id) DO UPDATE SET state='inaccessible',error_category='not_found',updated_at=excluded.updated_at",
                (gallery_id, _now(), _now()),
            )

    def mark_deleted(self, gallery_id: int) -> None:
        """Confirm a vector deletion only after the indexer has applied it."""
        with self.db:
            self.db.execute(
                "UPDATE galleries SET indexed_hash=NULL,updated_at=? WHERE id=? AND state='inaccessible'",
                (_now(), gallery_id),
            )

    def record_error(
        self, gallery_id: int, category: str, *, run_id: str | None = None
    ) -> None:
        with self.db:
            if run_id is not None:
                self._record_attempt(run_id, gallery_id)
            self.db.execute(
                "UPDATE galleries SET error_category=?,updated_at=? WHERE id=?",
                (category, _now(), gallery_id),
            )

    def list_pending(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM galleries WHERE state='index_pending' ORDER BY id"
        ).fetchall()

    def list_deletions(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM galleries WHERE state='inaccessible' AND indexed_hash IS NOT NULL ORDER BY id"
        ).fetchall()

    def list_candidates(self, *, ceiling_id: int | None = None) -> list[int]:
        if ceiling_id is None:
            return [
                r[0]
                for r in self.db.execute(
                    "SELECT id FROM galleries WHERE state='queued' ORDER BY id DESC"
                )
            ]
        return [
            r[0]
            for r in self.db.execute(
                "SELECT id FROM galleries WHERE state='queued' AND id<=? ORDER BY id DESC",
                (ceiling_id,),
            )
        ]

    def get(self, gallery_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM galleries WHERE id=?", (gallery_id,)
        ).fetchone()

    def counts(self) -> dict[str, int]:
        result = {
            state: 0
            for state in (
                "queued",
                "fetched",
                "index_pending",
                "indexed",
                "inaccessible",
            )
        }
        for state, count in self.db.execute(
            "SELECT state,count(*) FROM galleries GROUP BY state"
        ):
            result[state] = count
        result["pending"] = result["queued"] + result["index_pending"]
        result["delete_pending"] = self.db.execute(
            "SELECT count(*) FROM galleries WHERE state='inaccessible' AND indexed_hash IS NOT NULL"
        ).fetchone()[0]
        return result

    def start_run(self) -> str:
        run_id = uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO runs(run_id,status,created_at,updated_at) VALUES(?,'running',?,?)",
                (run_id, _now(), _now()),
            )
        return run_id

    def latest_run(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM runs ORDER BY created_at DESC,rowid DESC LIMIT 1"
        ).fetchone()

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()

    def set_ceiling(self, run_id: str, ceiling_id: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET ceiling_id=?,updated_at=? WHERE run_id=? AND ceiling_id IS NULL",
                (ceiling_id, _now(), run_id),
            )

    def set_run_status(self, run_id: str, status: str) -> None:
        if status not in {"running", "paused", "completed", "failed"}:
            raise ValueError("invalid run status")
        with self.db:
            self.db.execute(
                "UPDATE runs SET status=?,updated_at=? WHERE run_id=?",
                (status, _now(), run_id),
            )
