"""Fingerprint, replay, persistence, and vector validation checks."""

import pytest

from vibesearch.api_models import GalleryDetail
from vibesearch.catalog import Catalog
from vibesearch.embeddings import validate_vectors
from vibesearch.indexer import Indexer
from vibesearch.vector_store import ChromaVectorStore


class FakeEmbedder:
    model_id = "local/fixed"
    dimension = 2
    normalize = True
    document_prefix = ""
    query_prefix = ""

    def __init__(self, revision="a" * 40):
        self.revision = revision
        self.documents = []
        self.queries = []

    def embed_documents(self, texts):
        self.documents.extend(texts)
        return [[1.0, 0.0] if "Quiet" in text else [0.0, 1.0] for text in texts]

    def embed_query(self, text):
        self.queries.append(text)
        return [1.0, 0.0]


def save(catalog, gallery_id=1, title="Quiet", *, num_pages=8, upload_date=100, media_id=None):
    detail = GalleryDetail.model_validate({"id": gallery_id, "media_id": str(gallery_id),
        "title": {"english": title, "pretty": title}, "tags": [
        {"id": 10, "type": "tag", "name": "Cozy"}], "num_pages": num_pages, "upload_date": upload_date})
    if media_id is not None:
        detail.media_id = media_id
    catalog.save_detail(detail)


def test_idempotent_changed_contract_and_reconciliation(tmp_path):
    with Catalog(tmp_path / "catalog.db") as catalog:
        save(catalog)
        provider = FakeEmbedder()
        indexer = Indexer(catalog, provider, str(tmp_path / "chroma"))
        assert indexer.run().indexed == 1
        assert indexer.run().skipped == 1
        assert len(provider.documents) == 1
        save(catalog, title="Active")
        assert indexer.run().indexed == 1
        assert len(provider.documents) == 2
        new_provider = FakeEmbedder("b" * 40)
        changed = Indexer(catalog, new_provider, str(tmp_path / "chroma"))
        assert changed.fingerprint != indexer.fingerprint
        assert Indexer(catalog, new_provider, str(tmp_path / "chroma"), text_max_chars=128).fingerprint != changed.fingerprint
        new_provider.query_prefix = "query: "
        assert Indexer(catalog, new_provider, str(tmp_path / "chroma")).fingerprint != changed.fingerprint
        new_provider.query_prefix = ""
        assert changed.run().indexed == 1
        assert len(new_provider.documents) == 1
        store = ChromaVectorStore(tmp_path / "chroma", changed.fingerprint, 2,
                                  manifest=changed.manifest)
        store.delete(1)
        assert changed.run().indexed == 1
        catalog.mark_inaccessible(1)
        assert changed.run().deleted == 2
        assert catalog.get(1)["indexed_hash"] is None
        assert store.count() == 0
        old = ChromaVectorStore(tmp_path / "chroma", indexer.fingerprint, 2,
                                manifest=indexer.manifest)
        assert old.count() == 0  # cleaned before the shared marker was cleared
        assert indexer.run().deleted == 0


def test_metadata_only_change_refreshes_chroma_without_embedding(tmp_path):
    with Catalog(tmp_path / "catalog.db") as catalog:
        save(catalog)
        provider = FakeEmbedder()
        indexer = Indexer(catalog, provider, str(tmp_path / "chroma"))
        indexer.run()
        digest = catalog.get(1)["indexed_hash"]
        save(catalog, num_pages=17, upload_date=300, media_id="new-media")
        assert catalog.get(1)["state"] == "indexed"
        assert catalog.get(1)["indexed_hash"] == digest
        assert indexer.run().skipped == 1
        assert len(provider.documents) == 1
        store = ChromaVectorStore(tmp_path / "chroma", indexer.fingerprint, 2,
                                  manifest=indexer.manifest)
        data = store.collection.get(ids=["1"], include=["metadatas", "documents", "embeddings"])
        assert data["metadatas"][0]["media_id"] == "new-media"
        assert data["metadatas"][0]["num_pages"] == 17
        assert data["metadatas"][0]["upload_date"] == 300
        assert list(data["embeddings"][0]) == [1.0, 0.0]


def test_text_equivalent_pending_refresh_reuses_vector_and_recovers_marker(tmp_path, monkeypatch):
    with Catalog(tmp_path / "catalog.db") as catalog:
        save(catalog)
        provider = FakeEmbedder()
        indexer = Indexer(catalog, provider, str(tmp_path / "chroma"))
        assert indexer.run().indexed == 1
        original_hash = catalog.get(1)["indexed_hash"]
        save(catalog, title=" Quiet ", media_id="refreshed-media")
        pending = catalog.get(1)
        assert pending["state"] == "index_pending"
        assert pending["content_hash"] != original_hash

        original_mark_indexed = catalog.mark_indexed
        def interrupted(_gallery_id):
            raise RuntimeError("interrupted")
        monkeypatch.setattr(catalog, "mark_indexed", interrupted)
        with pytest.raises(RuntimeError, match="interrupted"):
            indexer.run()
        assert catalog.get(1)["state"] == "index_pending"
        assert catalog.get(1)["indexed_hash"] == original_hash
        assert provider.documents == ["Title: Quiet\nTags: Cozy"]

        store = ChromaVectorStore(tmp_path / "chroma", indexer.fingerprint, 2,
                                  manifest=indexer.manifest)
        data = store.collection.get(ids=["1"], include=["metadatas", "documents", "embeddings"])
        assert data["documents"] == ["Title: Quiet\nTags: Cozy"]
        assert data["metadatas"][0]["media_id"] == "refreshed-media"
        assert list(data["embeddings"][0]) == [1.0, 0.0]

        monkeypatch.setattr(catalog, "mark_indexed", original_mark_indexed)
        assert indexer.run().indexed == 1
        assert catalog.get(1)["state"] == "indexed"
        assert catalog.get(1)["indexed_hash"] == pending["content_hash"]
        assert provider.documents == ["Title: Quiet\nTags: Cozy"]
        assert indexer.run().skipped == 1
        save(catalog, title="Active")
        assert indexer.run().indexed == 1
        assert provider.documents[-1] == "Title: Active\nTags: Cozy"
        assert len(provider.documents) == 2


def test_other_contract_cannot_mark_stale_vector_current(tmp_path):
    with Catalog(tmp_path / "catalog.db") as catalog:
        save(catalog)
        a_provider, b_provider = FakeEmbedder(), FakeEmbedder("b" * 40)
        a = Indexer(catalog, a_provider, str(tmp_path / "chroma"))
        b = Indexer(catalog, b_provider, str(tmp_path / "chroma"))
        assert a.run().indexed == b.run().indexed == 1
        save(catalog, title="Active")
        assert b.run().indexed == 1
        assert catalog.get(1)["state"] == "indexed"
        assert a.run().indexed == 1
        assert a_provider.documents == ["Title: Quiet\nTags: Cozy", "Title: Active\nTags: Cozy"]
        store = ChromaVectorStore(tmp_path / "chroma", a.fingerprint, 2, manifest=a.manifest)
        row = store.collection.get(ids=["1"], include=["documents", "embeddings"])
        assert row["documents"] == ["Title: Active\nTags: Cozy"]
        assert list(row["embeddings"][0]) == [0.0, 1.0]
        assert a.run().skipped == 1
        assert len(a_provider.documents) == 2


def test_cross_contract_crash_after_upsert_replays(tmp_path, monkeypatch):
    with Catalog(tmp_path / "catalog.db") as catalog:
        save(catalog)
        a = Indexer(catalog, FakeEmbedder(), str(tmp_path / "chroma"))
        b = Indexer(catalog, FakeEmbedder("b" * 40), str(tmp_path / "chroma"))
        a.run()
        b.run()
        save(catalog, title="Active")
        b.run()
        original = catalog.mark_indexed
        monkeypatch.setattr(catalog, "mark_indexed", lambda *_: (_ for _ in ()).throw(RuntimeError("interrupted")))
        with pytest.raises(RuntimeError, match="interrupted"):
            a.run()
        monkeypatch.setattr(catalog, "mark_indexed", original)
        assert a.run().indexed == 1
        assert catalog.get(1)["state"] == "indexed"


def test_deletion_failure_retains_marker_for_retry(tmp_path, monkeypatch):
    with Catalog(tmp_path / "catalog.db") as catalog:
        save(catalog)
        old = Indexer(catalog, FakeEmbedder(), str(tmp_path / "chroma"))
        new = Indexer(catalog, FakeEmbedder("b" * 40), str(tmp_path / "chroma"))
        old.run()
        new.run()
        catalog.mark_inaccessible(1)
        original = ChromaVectorStore.delete
        def fail_old(self, gallery_id):
            if self.fingerprint == old.fingerprint:
                raise RuntimeError("delete interrupted")
            return original(self, gallery_id)
        monkeypatch.setattr(ChromaVectorStore, "delete", fail_old)
        with pytest.raises(RuntimeError, match="delete interrupted"):
            new.run()
        assert catalog.get(1)["indexed_hash"] is not None
        monkeypatch.setattr(ChromaVectorStore, "delete", original)
        assert new.run().deleted == 1
        assert catalog.get(1)["indexed_hash"] is None


def test_crash_after_upsert_replays(tmp_path, monkeypatch):
    with Catalog(tmp_path / "catalog.db") as catalog:
        save(catalog)
        indexer = Indexer(catalog, FakeEmbedder(), str(tmp_path / "chroma"))
        original = catalog.mark_indexed
        monkeypatch.setattr(catalog, "mark_indexed", lambda *_: (_ for _ in ()).throw(RuntimeError("interrupted")))
        with pytest.raises(RuntimeError, match="interrupted"):
            indexer.run()
        monkeypatch.setattr(catalog, "mark_indexed", original)
        assert indexer.run().indexed == 1
        assert catalog.get(1)["state"] == "indexed"


def test_vector_validation_precedes_chroma_write(tmp_path):
    provider = FakeEmbedder()
    with Catalog(tmp_path / "catalog.db") as catalog:
        save(catalog)
        indexer = Indexer(catalog, provider, str(tmp_path / "chroma"))
        provider.embed_documents = lambda _texts: [[float("nan"), 0.0]]
        with pytest.raises(ValueError, match="non-finite"):
            indexer.run()
        assert catalog.get(1)["state"] == "index_pending"
    with pytest.raises(ValueError, match="dimension"):
        validate_vectors([[0.0]], 2)


@pytest.mark.parametrize(("model_id", "document", "query"), [
    ("BAAI/bge-m3", "record", "mood"),
    ("Qwen/Qwen3-Embedding-0.6B", "record",
     "Instruct: Given a natural-language description, retrieve relevant gallery metadata\nQuery: mood"),
    ("nomic-ai/nomic-embed-text-v1.5", "search_document: record", "search_query: mood"),
])
def test_local_model_prefixes_and_search_local_files_only(monkeypatch, model_id, document, query):
    import sentence_transformers
    from vibesearch.embeddings import LocalSentenceTransformer

    constructed, inputs, encode_options = [], [], []
    class FakeModel:
        def __init__(self, *args, **kwargs):
            constructed.append((args, kwargs))

        def get_sentence_embedding_dimension(self):
            return 2

        def __getitem__(self, key):
            from types import SimpleNamespace
            return SimpleNamespace(auto_model=SimpleNamespace(config=SimpleNamespace(_commit_hash="a" * 40)))

        def encode(self, texts, **kwargs):
            inputs.extend(texts)
            encode_options.append(kwargs)
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", FakeModel)
    provider = LocalSentenceTransformer(model_id, offline=True, device="cpu")
    assert provider.embed_documents(["record"]) == [[1.0, 0.0]]
    assert provider.embed_query("mood") == [1.0, 0.0]
    assert inputs == [document, query]
    assert all(options["prompt"] == "" for options in encode_options)
    assert constructed[0][1]["local_files_only"] is True
    assert constructed[0][1]["trust_remote_code"] is False
    assert provider.revision == "a" * 40


def test_model_prefixes_affect_index_contract():
    from vibesearch.embeddings import LocalSentenceTransformer
    from vibesearch.indexer import embedding_manifest, index_fingerprint

    standard = LocalSentenceTransformer("Qwen/Qwen3-Embedding-0.6B")
    alternate = LocalSentenceTransformer("Qwen/Qwen3-Embedding-0.6B", query_prefix="")
    for provider in (standard, alternate):
        provider._revision, provider._dimension = "a" * 40, 2
        provider._model = object()
    assert index_fingerprint(embedding_manifest(standard)) != index_fingerprint(embedding_manifest(alternate))
