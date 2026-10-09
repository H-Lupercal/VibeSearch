"""Reproducible index contracts and replayable single-writer indexing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from .embeddings import EmbeddingProvider, validate_vectors
from .text_builder import TEMPLATE_VERSION, canonical_gallery
from .vector_store import ChromaVectorStore


class IndexCatalog(Protocol):
    """Catalog API: db (SQLite connection), get, list_pending, list_deletions,
    mark_indexed, mark_deleted. SQLite stores raw_json and content_hash.
    """
    db: Any
    def get(self, gallery_id: int) -> Any: ...
    def list_pending(self) -> list[Any]: ...
    def list_deletions(self) -> list[Any]: ...
    def mark_indexed(self, gallery_id: int) -> None: ...
    def mark_deleted(self, gallery_id: int) -> None: ...


def embedding_manifest(provider: EmbeddingProvider, template_version: str = TEMPLATE_VERSION,
                       text_max_chars: int = 4096) -> dict[str, Any]:
    if template_version != TEMPLATE_VERSION:
        raise ValueError("Unsupported text template; rebuild text with a supported template")
    if text_max_chars < 1:
        raise ValueError("text_max_chars must be positive")
    revision = getattr(provider, "revision", None)
    if not revision:
        raise ValueError("Embedding provider must expose its resolved immutable revision")
    return {"model_id": provider.model_id, "revision": revision,
            "dimension": provider.dimension, "normalize": bool(getattr(provider, "normalize", True)),
            "metric": "cosine", "document_prefix": str(getattr(provider, "document_prefix", "")),
            "query_prefix": str(getattr(provider, "query_prefix", "")),
            "template_version": template_version, "text_max_chars": text_max_chars}


def index_fingerprint(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")).hexdigest()


def read_manifest(catalog: IndexCatalog, fingerprint: str) -> dict[str, Any] | None:
    try:
        row = catalog.db.execute("SELECT manifest_json FROM index_manifests WHERE fingerprint=?",
                                 (fingerprint,)).fetchone()
    except __import__("sqlite3").OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    return json.loads(row[0]) if row else None


def save_manifest(catalog: IndexCatalog, fingerprint: str, manifest: dict[str, Any]) -> None:
    with catalog.db:
        catalog.db.execute("CREATE TABLE IF NOT EXISTS index_manifests "
                           "(fingerprint TEXT PRIMARY KEY, manifest_json TEXT NOT NULL)")
        catalog.db.execute("INSERT OR IGNORE INTO index_manifests VALUES (?,?)",
                           (fingerprint, json.dumps(manifest, sort_keys=True)))


@dataclass(frozen=True)
class IndexResult:
    fingerprint: str
    indexed: int = 0
    skipped: int = 0
    deleted: int = 0


class Indexer:
    def __init__(self, catalog: IndexCatalog, provider: EmbeddingProvider, chroma_path: str,
                 *, template_version: str = TEMPLATE_VERSION, text_max_chars: int = 4096,
                 store_factory: type[ChromaVectorStore] = ChromaVectorStore) -> None:
        self.catalog, self.provider, self.chroma_path = catalog, provider, chroma_path
        self.manifest = embedding_manifest(provider, template_version, text_max_chars)
        self.fingerprint = index_fingerprint(self.manifest)
        self.store_factory = store_factory

    def run(self) -> IndexResult:
        store = self.store_factory(self.chroma_path, self.fingerprint, self.provider.dimension,
                                   manifest=self.manifest, create=True)
        existing = read_manifest(self.catalog, self.fingerprint)
        if existing is not None and existing != self.manifest:
            raise ValueError("SQLite manifest differs from the active collection")
        save_manifest(self.catalog, self.fingerprint, self.manifest)
        deleted = 0
        # The catalog has one global indexed_hash, so do not clear it while an
        # older contract still holds the inaccessible record.
        collections = [store]
        for fingerprint, raw_manifest in self.catalog.db.execute(
                "SELECT fingerprint,manifest_json FROM index_manifests WHERE fingerprint!=?",
                (self.fingerprint,)).fetchall():
            manifest = json.loads(raw_manifest)
            try:
                collections.append(self.store_factory(self.chroma_path, fingerprint,
                    manifest["dimension"], manifest=manifest, create=False))
            except LookupError:
                # A persisted manifest can outlive a removed collection.
                continue
        for row in self.catalog.db.execute("SELECT id,indexed_hash FROM galleries WHERE state='inaccessible'").fetchall():
            gallery_id = row["id"]
            for collection in collections:
                if collection.contains(gallery_id):
                    collection.delete(gallery_id)
                    deleted += 1
            if row["indexed_hash"] is not None:
                self.catalog.mark_deleted(gallery_id)
        # Every active detail is eligible: a new fingerprint must re-embed
        # records already marked indexed for another contract.
        rows = self.catalog.db.execute(
            "SELECT id,state,raw_json FROM galleries WHERE raw_json IS NOT NULL "
            "AND state NOT IN ('inaccessible','queued') ORDER BY id").fetchall()
        indexed = skipped = 0
        for row in rows:
            gallery_id = row["id"]
            record = canonical_gallery(json.loads(row["raw_json"]),
                                       max_chars=self.manifest["text_max_chars"])
            # The catalog marker is shared by all contracts. Only the document
            # in this collection proves that its vector represents this text,
            # even when a source-field hash has changed without changing text.
            if store.document(gallery_id) == record.rich_text:
                store.sync_record(record)
                if row["state"] != "indexed":
                    # Persist the marker only after the Chroma metadata refresh;
                    # an interruption leaves pending work to replay safely.
                    if row["state"] != "index_pending":
                        with self.catalog.db:
                            self.catalog.db.execute("UPDATE galleries SET state='index_pending' WHERE id=?", (gallery_id,))
                    self.catalog.mark_indexed(gallery_id)
                    indexed += 1
                else:
                    skipped += 1
                continue
            vector = self.provider.embed_documents([record.rich_text])[0]
            validate_vectors([vector], self.provider.dimension)
            store.upsert(record, vector)
            # The Chroma write precedes the durable SQLite indexed marker.
            # Missing-vector reconciliation replays this operation on restart.
            if row["state"] != "index_pending":
                with self.catalog.db:
                    self.catalog.db.execute("UPDATE galleries SET state='index_pending' WHERE id=?", (gallery_id,))
            self.catalog.mark_indexed(gallery_id)
            indexed += 1
        return IndexResult(self.fingerprint, indexed, skipped, deleted)
