"""CLI boundary tests: no model downloads and no live API requests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from vibesearch import cli
from vibesearch.api_client import APIError, APIRateLimited, APISchemaError, APIStatusError
from vibesearch.api_models import GalleryDetail, GalleryListResponse
from vibesearch.catalog import Catalog
from vibesearch.config import Settings

runner = CliRunner()


@pytest.fixture
def configured(monkeypatch, tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, user_agent_contact="operator@example.invalid",
                        api_key="private-test-token")
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    return settings


def forbidden(*args, **kwargs):
    raise AssertionError("unrelated boundary invoked")


def test_help_includes_all_commands_without_initialization(monkeypatch):
    monkeypatch.setattr(cli, "Settings", forbidden)
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0, result.output
    for name in ("collect", "index", "search", "status", "reindex"):
        assert name in result.output


def test_collect_bounds_rates_and_never_loads_model(configured, monkeypatch):
    observed = {}

    class FakeClient:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    class FakeCollector:
        def __init__(self, client, catalog, **kwargs):
            assert isinstance(client, FakeClient)
            assert isinstance(catalog, Catalog)
            observed.update(kwargs)

        async def collect(self):
            return SimpleNamespace(run_id="sample", status="completed", pages_scanned=1,
                                   scanned=2, deduplicated=0, fetched=2, skipped=0,
                                   inaccessible=0, pending=0)

    monkeypatch.setattr(cli, "MetadataClient", FakeClient)
    monkeypatch.setattr(cli, "Collector", FakeCollector)
    monkeypatch.setattr(cli, "LocalSentenceTransformer", forbidden)
    result = runner.invoke(cli.app, ["collect", "--max-items", "2", "--max-pages", "1", "--per-page", "3"])
    assert result.exit_code == 0, result.output
    assert "fetched=2" in result.output
    assert (observed["max_items"], observed["max_pages"], observed["per_page"]) == (2, 1, 3)
    assert observed["list_per_minute"] == configured.list_rate_authenticated
    assert observed["detail_per_minute"] == configured.detail_rate_authenticated
    assert observed["api_key"] == "private-test-token"
    assert "private-test-token" not in result.output
    assert runner.invoke(cli.app, ["collect", "--max-items", "101"]).exit_code != 0


def test_collect_requires_contact_before_network(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None, data_dir=tmp_path,
                                                            user_agent_contact=""))
    monkeypatch.setattr(cli, "MetadataClient", forbidden)
    result = runner.invoke(cli.app, ["collect"])
    assert result.exit_code == 2
    assert "Invalid configuration" in result.output


@pytest.mark.parametrize("command", ["index", "reindex"])
def test_index_commands_use_local_contract_without_api(configured, monkeypatch, command):
    seen = {}
    monkeypatch.setattr(cli, "MetadataClient", forbidden)
    monkeypatch.setattr(cli, "LocalSentenceTransformer", lambda **kw: seen.update(kw) or object())

    class FakeIndexer:
        def __init__(self, catalog, provider, path, **kw):
            assert isinstance(catalog, Catalog)
            assert provider is not None
            assert path == str(configured.chroma_path)
            assert kw["template_version"] == configured.text_template_version

        def run(self):
            return SimpleNamespace(fingerprint="abcdef0123456789", indexed=3, skipped=2, deleted=1)

    monkeypatch.setattr(cli, "Indexer", FakeIndexer)
    result = runner.invoke(cli.app, [command])
    assert result.exit_code == 0, result.output
    assert "indexed=3" in result.output
    assert "galleries_abcdef0123456789" in result.output
    assert seen["model_id"] == configured.embedding_model
    assert seen["normalize"] is True
    assert seen["offline"] is False
    if command == "reindex":
        assert "changed fingerprint creates a separate collection" in result.output


def test_search_offline_display_and_pending_warning(configured, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "MetadataClient", forbidden)
    monkeypatch.setattr(cli, "LocalSentenceTransformer", lambda **kw: seen.update(provider=kw) or object())

    class FakeSearch:
        def __init__(self, catalog, provider, path, **kwargs):
            assert isinstance(catalog, Catalog)
            assert path == str(configured.chroma_path)

        def search(self, query, **kwargs):
            seen.update(query=query, **kwargs)
            return SimpleNamespace(indexed_count=8, missing_count=2, pending_warning=True, hits=(
                SimpleNamespace(gallery_id=7, num_pages=13, cosine_distance=0.25,
                                title_display="A sample title", tags=({"name": "quiet"},)),))

    monkeypatch.setattr(cli, "SearchService", FakeSearch)
    full = runner.invoke(cli.app, ["search", "gentle atmosphere", "--top-k", "2"])
    assert full.exit_code == 0, full.output
    assert "cosine_distance=0.250000" in full.output
    assert "A sample title" in full.output and "quiet" in full.output
    assert "pending indexing" in full.output
    assert "2 eligible local records" in full.output
    assert seen["provider"]["offline"] is True
    assert {k: seen[k] for k in ("query", "top_k", "display")} == {"query": "gentle atmosphere", "top_k": 2, "display": "full"}
    brief = runner.invoke(cli.app, ["search", "gentle atmosphere", "--display", "id-only"])
    assert brief.exit_code == 0, brief.output
    assert "A sample title" not in brief.output and "quiet" not in brief.output
    assert "7  pages=13" in brief.output
    assert runner.invoke(cli.app, ["search", " "]).exit_code == 2
    assert runner.invoke(cli.app, ["search", "query", "--display", "unknown"]).exit_code == 2


def test_status_is_local_no_model_and_no_secret(configured, monkeypatch):
    monkeypatch.setattr(cli, "MetadataClient", forbidden)
    monkeypatch.setattr(cli, "LocalSentenceTransformer", forbidden)
    empty = runner.invoke(cli.app, ["status"])
    assert empty.exit_code == 0 and "No local catalog" in empty.output
    assert not configured.catalog_path.exists()
    with Catalog(configured.catalog_path) as catalog:
        catalog.queue_candidate(9)
        catalog.record_error(9, "schema")
        run_id = catalog.start_run()
        catalog.db.execute("CREATE TABLE index_manifests (fingerprint TEXT PRIMARY KEY, manifest_json TEXT NOT NULL)")
        catalog.db.execute("INSERT INTO index_manifests VALUES (?, ?)",
                           ("abcdef0123456789", '{"model_id":"local-model","revision":"rev1","template_version":"gallery-text-v1"}'))
        catalog.db.commit()
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "queued=1" in result.output and "errors=1" in result.output
    assert run_id in result.output and "local-model" in result.output
    assert "private-test-token" not in result.output


@pytest.mark.parametrize(("exception", "code", "message"), [
    (APIStatusError(401), 3, "authentication"),
    (APIStatusError(503), 4, "unavailable"),
    (APIRateLimited(), 4, "rate limit"),
    (APISchemaError("sensitive-payload"), 5, "schema mismatch"),
    (APIError("sensitive-token"), 4, "unavailable"),
    (LookupError("private-path"), 6, "No compatible local index"),
    (RuntimeError("private-path"), 7, "Operation failed"),
])
def test_errors_are_actionable_redacted_and_nonzero(configured, monkeypatch, exception, code, message):
    def failing(*_, **__):
        raise exception

    monkeypatch.setattr(cli, "LocalSentenceTransformer", failing)
    result = runner.invoke(cli.app, ["index"])
    assert result.exit_code == code, result.output
    assert message.lower() in result.output.lower()
    assert "private-" not in result.output and "sensitive-" not in result.output
    assert "Traceback" not in result.output


def test_collect_index_search_reindex_real_local_pipeline(configured, monkeypatch):
    """Exercise the real collector, catalog, indexer, and search with fake boundaries."""
    class FakeClient:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def list_galleries(self, page, per_page):
            assert page == 1
            return GalleryListResponse.model_validate({"result": [
                {"id": 7, "media_id": "7", "english_title": "Quiet path", "num_pages": 8},
            ], "num_pages": 1, "per_page": per_page})

        async def get_detail(self, gallery_id):
            raw = {"id": gallery_id, "media_id": str(gallery_id),
                   "title": {"english": "Quiet path", "pretty": "Quiet path"},
                   "tags": [{"id": 12, "type": "tag", "name": "cozy"}],
                   "num_pages": 8, "upload_date": 100}
            return GalleryDetail.model_validate(raw), raw

    class FakeProvider:
        model_id = "local/test"
        dimension = 2
        normalize = True
        document_prefix = ""
        query_prefix = ""
        revision = "a" * 40

        def embed_documents(self, texts):
            assert "Quiet path" in texts[0]
            return [[1.0, 0.0] for _ in texts]

        def embed_query(self, text):
            return [1.0, 0.0]

    monkeypatch.setattr(cli, "MetadataClient", FakeClient)
    monkeypatch.setattr(cli, "LocalSentenceTransformer", lambda **_: FakeProvider())
    missing = runner.invoke(cli.app, ["search", "quiet"])
    assert missing.exit_code == 6 and "No compatible local index" in missing.output
    collected = runner.invoke(cli.app, ["collect", "--max-items", "1", "--max-pages", "1"])
    assert collected.exit_code == 0, collected.output
    assert "fetched=1" in collected.output
    indexed = runner.invoke(cli.app, ["index"])
    assert indexed.exit_code == 0, indexed.output
    assert "indexed=1" in indexed.output
    found = runner.invoke(cli.app, ["search", "quiet"])
    assert found.exit_code == 0, found.output
    assert "Quiet path" in found.output and "cozy" in found.output
    assert "cosine_distance=" in found.output
    status = runner.invoke(cli.app, ["status"])
    assert status.exit_code == 0 and "indexed=1" in status.output
    assert "private-test-token" not in status.output
    FakeProvider.revision = "b" * 40
    assert runner.invoke(cli.app, ["search", "quiet"]).exit_code == 6
    reindexed = runner.invoke(cli.app, ["reindex"])
    assert reindexed.exit_code == 0 and "indexed=1" in reindexed.output
    assert runner.invoke(cli.app, ["search", "quiet"]).exit_code == 0
