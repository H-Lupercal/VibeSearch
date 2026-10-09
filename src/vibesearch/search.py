"""Offline search over exactly one active embedding contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .embeddings import EmbeddingProvider, validate_vectors
from .indexer import IndexCatalog, embedding_manifest, index_fingerprint, read_manifest
from .text_builder import TEMPLATE_VERSION, canonical_gallery
from .vector_store import ChromaVectorStore


@dataclass(frozen=True)
class SearchHit:
    gallery_id: int
    cosine_distance: float
    num_pages: int
    title_display: str | None = None
    tags: tuple[Any, ...] = ()


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[SearchHit, ...]
    pending_warning: bool
    indexed_count: int
    missing_count: int = 0


class SearchService:
    def __init__(self, catalog: IndexCatalog, provider: EmbeddingProvider, chroma_path: str,
                 *, template_version: str = TEMPLATE_VERSION, text_max_chars: int = 4096,
                 store_factory: type[ChromaVectorStore] = ChromaVectorStore) -> None:
        self.catalog, self.provider, self.chroma_path = catalog, provider, chroma_path
        self.manifest = embedding_manifest(provider, template_version, text_max_chars)
        self.fingerprint = index_fingerprint(self.manifest)
        self.store_factory = store_factory

    def search(self, query: str, *, top_k: int = 20, display: str = "full") -> SearchResult:
        if not query.strip():
            raise ValueError("Search query must not be empty")
        if top_k < 1:
            raise ValueError("top_k must be positive")
        if display not in ("full", "id-only"):
            raise ValueError("display must be full or id-only")
        if read_manifest(self.catalog, self.fingerprint) != self.manifest:
            raise LookupError("No compatible index manifest; run index for this embedding contract")
        store = self.store_factory(self.chroma_path, self.fingerprint, self.provider.dimension,
                                   manifest=self.manifest, create=False)
        count = store.count()
        if not count:
            raise LookupError("The local index is empty; collect and index gallery details first")
        eligible_rows = self.catalog.db.execute(
            "SELECT id,raw_json FROM galleries WHERE raw_json IS NOT NULL "
            "AND state NOT IN ('inaccessible','queued')").fetchall()
        eligible = {row["id"] for row in eligible_rows}
        documents = store.documents()
        present = set(documents)
        missing_count = len(eligible - present)
        stale = any(row["id"] in documents and documents[row["id"]] !=
                    canonical_gallery(json.loads(row["raw_json"]),
                                      max_chars=self.manifest["text_max_chars"]).rich_text
                    for row in eligible_rows)
        vector = self.provider.embed_query(query)
        validate_vectors([vector], self.provider.dimension)
        # Hydrate against SQLite to avoid returning inaccessible or missing IDs.
        # Querying the whole local collection permits filling top_k after exclusions.
        hits = []
        for gallery_id, distance in store.query(vector, count):
            row = self.catalog.get(gallery_id)
            if row is None or row["state"] == "inaccessible" or not row["raw_json"]:
                continue
            detail = json.loads(row["raw_json"])
            if display == "full":
                titles = detail.get("title") or {}
                title = next((titles.get(key) for key in ("pretty", "english", "japanese")
                              if titles.get(key)), str(gallery_id))
                tags = tuple(detail.get("tags") or ())
            else:
                title, tags = None, ()
            hits.append(SearchHit(gallery_id, distance, int(detail["num_pages"]), title, tags))
            if len(hits) >= top_k:
                break
        pending = bool(stale or missing_count or self.catalog.list_pending() or self.catalog.list_deletions()
                       or (present - eligible))
        return SearchResult(tuple(hits), pending, count, missing_count)
