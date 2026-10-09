"""Persistent Chroma adapter with externally computed dense vectors only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .embeddings import validate_vectors


class ChromaVectorStore:
    def __init__(self, path: str | Path, fingerprint: str, dimension: int, *,
                 manifest: dict[str, Any] | None = None, create: bool = False) -> None:
        import chromadb
        if dimension < 1 or len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("Invalid index dimension or fingerprint")
        self.dimension, self.fingerprint = dimension, fingerprint
        self.name = f"galleries_{fingerprint[:24]}"
        self.client = chromadb.PersistentClient(path=str(path))
        metadata = {"fingerprint": fingerprint, "dimension": dimension, "metric": "cosine"}
        if manifest is not None:
            metadata["manifest"] = __import__("json").dumps(manifest, sort_keys=True)
        if create:
            self.collection = self.client.get_or_create_collection(
                name=self.name, metadata=metadata, configuration={"hnsw": {"space": "cosine"}},
                embedding_function=None)
        else:
            try:
                self.collection = self.client.get_collection(name=self.name, embedding_function=None)
            except Exception as exc:
                raise LookupError("No index exists for this embedding contract; run index first") from exc
        actual = self.collection.metadata or {}
        if (actual.get("fingerprint") != fingerprint or actual.get("dimension") != dimension
                or actual.get("metric") != "cosine" or
                (manifest is not None and actual.get("manifest") != metadata["manifest"])):
            raise ValueError("Existing collection has an incompatible index contract")
        config = getattr(self.collection, "configuration", None)
        if config and isinstance(config, dict) and config.get("hnsw", {}).get("space") != "cosine":
            raise ValueError("Existing collection is not configured for cosine distance")

    def count(self) -> int:
        return self.collection.count()

    def contains(self, gallery_id: int) -> bool:
        return bool(self.collection.get(ids=[str(gallery_id)], include=[])["ids"])

    def ids(self) -> set[int]:
        return {int(value) for value in self.collection.get(include=[])["ids"]}

    def document(self, gallery_id: int) -> str | None:
        """Return this contract's stored embedding input, if present."""
        result = self.collection.get(ids=[str(gallery_id)], include=["documents"])
        return result["documents"][0] if result["ids"] else None

    def documents(self) -> dict[int, str]:
        """Return stored embedding inputs for active-contract freshness checks."""
        result = self.collection.get(include=["documents"])
        return dict(zip((int(value) for value in result["ids"]), result["documents"]))

    def upsert(self, record: Any, embedding: list[float]) -> None:
        validate_vectors([embedding], self.dimension)
        gallery_id, document, metadata = self._record_fields(record)
        self.collection.upsert(ids=[gallery_id], embeddings=[embedding],
                               documents=[document], metadatas=[metadata])

    @staticmethod
    def _record_fields(record: Any) -> tuple[str, str, dict[str, Any]]:
        get = (lambda key: record[key]) if isinstance(record, dict) else (lambda key: getattr(record, key))
        gallery_id = int(get("id"))
        metadata = {"gallery_id": gallery_id, "media_id": str(get("media_id")),
                    "title_display": str(get("title_display")),
                    "num_pages": int(get("num_pages")), "upload_date": int(get("upload_date"))}
        return str(gallery_id), str(get("rich_text")), metadata

    def sync_record(self, record: Any) -> bool:
        """Refresh metadata only when the existing vector represents this document."""
        gallery_id, document, metadata = self._record_fields(record)
        current = self.collection.get(ids=[gallery_id], include=["metadatas", "documents"])
        if not current["ids"]:
            raise LookupError("Vector disappeared before metadata synchronization")
        if current["documents"][0] != document:
            raise ValueError("Stored document is stale; re-embed before synchronizing")
        if current["metadatas"][0] == metadata and current["documents"][0] == document:
            return False
        self.collection.update(ids=[gallery_id], metadatas=[metadata])
        return True

    def delete(self, gallery_id: int) -> None:
        self.collection.delete(ids=[str(gallery_id)])

    def query(self, vector: list[float], limit: int) -> list[tuple[int, float]]:
        validate_vectors([vector], self.dimension)
        if limit < 1:
            raise ValueError("limit must be positive")
        limit = min(limit, self.count())
        if not limit:
            return []
        result = self.collection.query(query_embeddings=[vector], n_results=limit, include=["distances"])
        return [(int(gallery_id), float(distance)) for gallery_id, distance in
                zip(result["ids"][0], result["distances"][0])]
