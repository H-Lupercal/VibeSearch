"""Deterministic source-only text construction checks."""

from vibesearch.api_models import GalleryDetail
from vibesearch.catalog import content_hash
from vibesearch.text_builder import TEMPLATE_VERSION, build_text, canonical_gallery


def detail(**changes):
    value = {"id": 42, "media_id": "m42", "title": {"english": "English", "pretty": "  Pretty  ",
             "japanese": "Japanese"}, "tags": [
             {"id": 5, "type": "language", "name": "French"},
             {"id": 3, "type": "tag", "name": "Calm"},
             {"id": 4, "type": "tag", "name": "calm"},
             {"id": 1, "type": "artist", "name": "  Mira  "},
             {"id": 2, "type": "parody", "name": "Voyage"}],
             "num_pages": 14, "upload_date": 1234}
    value.update(changes)
    return GalleryDetail.model_validate(value)


def test_typed_order_and_deduplication():
    record = canonical_gallery(detail())
    assert TEMPLATE_VERSION == "gallery-text-v1"
    assert record.rich_text == "Title: Pretty\nTags: Calm\nArtists: Mira\nSeries: Voyage\nLanguages: French"
    assert record.title_display == "Pretty"
    assert record.source_hash == content_hash(detail())
    assert record.tags[0].name == "French"


def test_fallback_hash_and_truncation():
    raw = detail(title={"pretty": "", "english": "", "japanese": ""})
    assert build_text(raw) == "Title: 42\nTags: Calm\nArtists: Mira\nSeries: Voyage\nLanguages: French"
    assert build_text(raw, max_chars=9) == "Title: 42"
    first = canonical_gallery(raw)
    changed = canonical_gallery(detail(title={"pretty": "", "english": "", "japanese": ""}, upload_date=9999))
    assert first.source_hash == changed.source_hash
    assert canonical_gallery(detail(title={"english": "Updated", "pretty": ""})).source_hash != first.source_hash
