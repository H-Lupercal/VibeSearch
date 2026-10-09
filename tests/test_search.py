"""Local ranked retrieval and source-only hydration checks."""

import pytest

from vibesearch.catalog import Catalog
from vibesearch.indexer import Indexer
from vibesearch.search import SearchService
from test_indexer import FakeEmbedder, save


def test_ranked_source_display_pending_and_inaccessible(tmp_path):
    with Catalog(tmp_path / "catalog.db") as catalog:
        save(catalog, 1, "Quiet")
        save(catalog, 2, "Active")
        provider = FakeEmbedder()
        Indexer(catalog, provider, str(tmp_path / "chroma")).run()
        service = SearchService(catalog, provider, str(tmp_path / "chroma"))
        result = service.search("quiet", top_k=12)
        assert [hit.gallery_id for hit in result.hits] == [1, 2]
        assert result.hits[0].cosine_distance < result.hits[1].cosine_distance
        assert result.hits[0].title_display == "Quiet"
        assert result.hits[0].tags[0]["name"] == "Cozy"
        assert result.indexed_count == 2
        assert provider.queries == ["quiet"]
        only_ids = service.search("quiet", top_k=1, display="id-only")
        assert only_ids.hits[0].title_display is None
        assert not only_ids.hits[0].tags
        save(catalog, 2, "Updated")
        assert service.search("quiet").pending_warning
        catalog.mark_inaccessible(1)
        result = service.search("quiet")
        assert [hit.gallery_id for hit in result.hits] == [2]


def test_empty_and_mismatched_contract(tmp_path):
    with Catalog(tmp_path / "catalog.db") as catalog:
        provider = FakeEmbedder()
        service = SearchService(catalog, provider, str(tmp_path / "chroma"))
        with pytest.raises(ValueError, match="empty"):
            service.search("  ")
        with pytest.raises(LookupError, match="manifest"):
            service.search("quiet")
        Indexer(catalog, provider, str(tmp_path / "chroma")).run()
        with pytest.raises(LookupError, match="empty"):
            service.search("quiet")
        with pytest.raises(LookupError, match="manifest"):
            SearchService(catalog, FakeEmbedder("b" * 40), str(tmp_path / "chroma")).search("quiet")


def test_interrupted_new_contract_warns_of_missing_eligible_records(tmp_path):
    with Catalog(tmp_path / "catalog.db") as catalog:
        save(catalog, 1)
        save(catalog, 2)
        old = Indexer(catalog, FakeEmbedder(), str(tmp_path / "chroma"))
        old.run()
        new_provider = FakeEmbedder("b" * 40)
        new = Indexer(catalog, new_provider, str(tmp_path / "chroma"))
        original = new_provider.embed_documents
        def stop_after_first(texts):
            if len(new_provider.documents) == 1:
                raise RuntimeError("interrupted reindex")
            return original(texts)
        new_provider.embed_documents = stop_after_first
        with pytest.raises(RuntimeError, match="interrupted reindex"):
            new.run()
        assert catalog.get(1)["state"] == "indexed"
        assert catalog.get(2)["state"] == "indexed"
        result = SearchService(catalog, new_provider, str(tmp_path / "chroma")).search("quiet")
        assert result.indexed_count == 1
        assert result.missing_count == 1
        assert result.pending_warning
        new_provider.embed_documents = original
        new.run()
        assert not SearchService(catalog, new_provider, str(tmp_path / "chroma")).search("quiet").pending_warning


def test_other_contract_update_warns_until_active_contract_reindexes(tmp_path):
    with Catalog(tmp_path / "catalog.db") as catalog:
        save(catalog)
        a_provider, b_provider = FakeEmbedder(), FakeEmbedder("b" * 40)
        a = Indexer(catalog, a_provider, str(tmp_path / "chroma"))
        b = Indexer(catalog, b_provider, str(tmp_path / "chroma"))
        a.run()
        b.run()
        service = SearchService(catalog, a_provider, str(tmp_path / "chroma"))
        assert not service.search("quiet").pending_warning
        save(catalog, title="Active")
        b.run()
        assert catalog.get(1)["state"] == "indexed"
        result = service.search("quiet")
        assert result.pending_warning
        assert result.missing_count == 0
        a.run()
        assert not service.search("quiet").pending_warning
