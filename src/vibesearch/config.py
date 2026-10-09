"""Validated local application settings."""

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Settings loaded from the environment and a local optional .env file."""

    model_config = SettingsConfigDict(
        env_prefix="VIBESEARCH_",
        env_file=str(PROJECT_ROOT / ".env"),
        extra="ignore",
        case_sensitive=False,
    )

    api_origin: str = "https://nhentai.net"
    api_key: str = Field(default="", repr=False)
    user_agent_contact: str = ""
    data_dir: Path = PROJECT_ROOT / "data"
    http_connect_timeout: float = Field(default=10.0, gt=0)
    http_read_timeout: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=4, ge=0, le=10)
    per_page: int = Field(default=25, ge=1, le=100)
    collect_max_items: int = Field(default=100, ge=1, le=100)
    collect_max_pages: int = Field(default=5, ge=1, le=5)
    list_rate_anonymous: float = Field(default=12.0, gt=0, le=12)
    detail_rate_anonymous: float = Field(default=16.0, gt=0, le=16)
    list_rate_authenticated: float = Field(default=24.0, gt=0, le=24)
    detail_rate_authenticated: float = Field(default=36.0, gt=0, le=36)
    embedding_model: str = "BAAI/bge-m3"
    embedding_revision: str | None = None
    embedding_device: Literal["auto", "cpu", "cuda", "mps"] = "auto"
    embedding_batch_size: int = Field(default=8, ge=1, le=256)
    embedding_normalize: bool = True
    text_template_version: str = "gallery-text-v1"
    display_mode: Literal["full", "id-only"] = "full"
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=8000, ge=1, le=65535)
    allow_non_loopback: bool = False
    embed_queue_size: int = Field(default=8, ge=1, le=64)
    embed_queue_timeout_s: float = Field(default=30.0, gt=0, le=300)
    image_cache_max_mb: int = Field(default=512, ge=15, le=4096)
    image_cache_ttl_hours: float = Field(default=24.0, gt=0, le=168)
    refresh_max_items_default: int = Field(default=50, ge=1, le=100)
    title_translator: Literal["none"] = "none"
    page_translator: Literal["none"] = "none"

    @field_validator("api_origin")
    @classmethod
    def validate_api_origin(cls, value: str) -> str:
        if not value.startswith("https://") or value.rstrip("/").endswith("/api/v2"):
            raise ValueError("api_origin must be an HTTPS origin, without /api/v2")
        return value.rstrip("/")

    @property
    def user_agent(self) -> str:
        """Return a descriptive user agent; collection requires operator contact."""
        if not self.user_agent_contact.strip():
            raise ValueError("Set VIBESEARCH_USER_AGENT_CONTACT before collecting")
        return f"VibeSearch/0.1 ({self.user_agent_contact.strip()})"

    @property
    def catalog_path(self) -> Path:
        return self.data_dir / "catalog.sqlite3"

    @property
    def chroma_path(self) -> Path:
        return self.data_dir / "chroma"
