"""Model compatibility without real model downloads."""

import sys
from types import SimpleNamespace

from vibesearch.embeddings import LocalSentenceTransformer


def test_cached_revision_and_modern_dimension(monkeypatch):
    class Model:
        def get_embedding_dimension(self):
            return 3

        def __getitem__(self, index):
            return SimpleNamespace(
                auto_model=SimpleNamespace(config=SimpleNamespace(_commit_hash=None))
            )

    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: False), backends=SimpleNamespace()
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=lambda *a, **kw: Model()),
    )
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(
            try_to_load_from_cache=lambda *a, **kw: (
                "/cache/models/snapshots/" + "a" * 40 + "/config.json"
            )
        ),
    )
    provider = LocalSentenceTransformer("local/test", offline=True)
    assert provider.dimension == 3
    assert provider.revision == "a" * 40
