"""Durable attempt-capped reader metadata refresh using the paced metadata client."""

import json
from uuid import uuid4

from .api_client import APIError, APIStatusError, DetailNotFound


async def refresh_metadata(catalog, client, *, max_items=50):
    if not 1 <= max_items <= 100:
        raise ValueError("Reader refresh cap must be 1..100")
    db = catalog.db
    with db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS reader_refresh_runs (id TEXT PRIMARY KEY, queue_json TEXT NOT NULL, position INTEGER NOT NULL, item_limit INTEGER NOT NULL, status TEXT NOT NULL)"
        )
    run = db.execute(
        "SELECT * FROM reader_refresh_runs WHERE status='running' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if run is None:
        ids = [
            r[0]
            for r in db.execute(
                "SELECT id FROM galleries WHERE raw_json IS NOT NULL AND validation_error IS NULL AND state NOT IN ('queued','inaccessible') AND readable=0 ORDER BY id LIMIT ?",
                (max_items,),
            )
        ]
        run_id = uuid4().hex
        with db:
            db.execute(
                "INSERT INTO reader_refresh_runs VALUES(?,?,0,?,?)",
                (run_id, json.dumps(ids), max_items, "running"),
            )
        run = db.execute(
            "SELECT * FROM reader_refresh_runs WHERE id=?", (run_id,)
        ).fetchone()
    ids = json.loads(run["queue_json"])
    position = run["position"]
    cap = min(max_items, run["item_limit"])
    refreshed = errors = 0
    while position < min(len(ids), cap):
        gid = ids[position]
        # Claim the attempt before issuing network traffic. A killed request consumes its slot.
        position += 1
        with db:
            db.execute(
                "UPDATE reader_refresh_runs SET position=? WHERE id=?",
                (position, run["id"]),
            )
        try:
            detail, raw = await client.get_detail(gid)
            catalog.save_detail(detail, raw)
            refreshed += 1
        except DetailNotFound:
            catalog.mark_inaccessible(gid)
            errors += 1
        except APIStatusError as exc:
            catalog.record_error(gid, "reader_http")
            errors += 1
            if exc.status_code in (401, 403):
                raise
        except APIError:
            catalog.record_error(gid, "reader_fetch")
            errors += 1
    with db:
        db.execute(
            "UPDATE reader_refresh_runs SET status='completed' WHERE id=?", (run["id"],)
        )
    return {
        "run_id": run["id"],
        "attempted": position,
        "refreshed": refreshed,
        "errors": errors,
        "status": "completed",
    }
