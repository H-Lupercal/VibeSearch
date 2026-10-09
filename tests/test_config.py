from pathlib import Path

import pytest
from pydantic import ValidationError

from vibesearch.config import Settings


def test_default_paths_and_missing_contact(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    assert settings.catalog_path == tmp_path / "catalog.sqlite3"
    assert settings.chroma_path == tmp_path / "chroma"
    with pytest.raises(ValueError, match="USER_AGENT_CONTACT"):
        _ = settings.user_agent


def test_contact_and_invalid_origin():
    settings = Settings(user_agent_contact="project.example")
    assert settings.user_agent == "VibeSearch/0.1 (project.example)"
    with pytest.raises(ValidationError):
        Settings(api_origin="https://example.org/api/v2")


def test_bounded_limits():
    with pytest.raises(ValidationError):
        Settings(per_page=101)
    with pytest.raises(ValidationError):
        Settings(detail_rate_anonymous=21)
    with pytest.raises(ValidationError):
        Settings(detail_rate_anonymous=17)
    with pytest.raises(ValidationError):
        Settings(list_rate_anonymous=13)
    with pytest.raises(ValidationError):
        Settings(list_rate_authenticated=25)
    with pytest.raises(ValidationError):
        Settings(detail_rate_authenticated=37)
    with pytest.raises(ValidationError):
        Settings(collect_max_items=101)
    with pytest.raises(ValidationError):
        Settings(collect_max_pages=6)
