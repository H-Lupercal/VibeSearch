"""Deterministic, source-only text for gallery-text-v1."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

TEMPLATE_VERSION = "gallery-text-v1"


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _title(titles: Any, name: str) -> str:
    return str(_field(titles, name, "") or "").strip()


@dataclass(frozen=True)
class CanonicalTag:
    id: int
    type: str
    name: str


@dataclass(frozen=True)
class CanonicalGallery:
    id: int
    media_id: str
    english: str
    japanese: str
    pretty: str
    tags: tuple[CanonicalTag, ...]
    num_pages: int
    upload_date: int
    source_hash: str
    rich_text: str
    title_display: str


def build_text(record: Any, *, max_chars: int = 4096) -> str:
    """Build fixed-order lines; truncate at a character boundary after assembly.

    max_chars limits the stored text (not model tokens). SentenceTransformer applies
    its model tokenizer's max sequence length separately during encoding.
    """
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    titles = _field(record, "title", record)
    gallery_id = int(_field(record, "id"))
    title = next((value for value in (_title(titles, "pretty"), _title(titles, "english"),
                                     _title(titles, "japanese")) if value), str(gallery_id))
    lines = [f"Title: {title}"]
    tags = sorted((_tag(tag) for tag in (_field(record, "tags", ()) or ())),
                  key=lambda tag: (tag.type.casefold(), tag.name.casefold(), tag.id))
    for types, heading in (({"tag", "general", "unclassified"}, "Tags"),
                           ({"artist"}, "Artists"), ({"character"}, "Characters"),
                           ({"parody", "series"}, "Series"), ({"group"}, "Groups"),
                           ({"language"}, "Languages")):
        names = []
        seen = set()
        for tag in tags:
            if tag.type.casefold() in types and tag.name and tag.name.casefold() not in seen:
                seen.add(tag.name.casefold())
                names.append(tag.name)
        if names:
            lines.append(f"{heading}: {', '.join(names)}")
    return "\n".join(lines)[:max_chars]


def _tag(tag: Any) -> CanonicalTag:
    return CanonicalTag(id=int(_field(tag, "id")),
                        type=str(_field(tag, "type", "") or "").strip(),
                        name=str(_field(tag, "name", "") or "").strip())


def canonical_gallery(detail: Any, *, max_chars: int = 4096) -> CanonicalGallery:
    """Map a typed detail or mapping; hash only text-affecting source fields."""
    gallery_id = int(_field(detail, "id"))
    titles = _field(detail, "title", detail)
    title_fields = {key: _title(titles, key) for key in ("english", "japanese", "pretty")}
    tags = tuple(_tag(tag) for tag in (_field(detail, "tags", ()) or ()))
    # Match the catalog's canonical hash of source title/tag fields. IDs are
    # immutable keys; non-text metadata does not invalidate an embedding.
    payload = {"title": {key: _field(titles, key, "") for key in ("english", "japanese", "pretty")},
               "tags": [{"id": _field(tag, "id"), "type": _field(tag, "type"),
                         "name": _field(tag, "name")}
                        for tag in (_field(detail, "tags", ()) or ())]}
    source_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    title_display = next((title_fields[key] for key in ("pretty", "english", "japanese")
                          if title_fields[key]), str(gallery_id))
    return CanonicalGallery(id=gallery_id, media_id=str(_field(detail, "media_id", "")),
                            **title_fields, tags=tags, num_pages=int(_field(detail, "num_pages", 0)),
                            upload_date=int(_field(detail, "upload_date", 0)),
                            source_hash=source_hash, rich_text=build_text(detail, max_chars=max_chars),
                            title_display=title_display)
