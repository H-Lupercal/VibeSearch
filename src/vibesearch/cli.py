"""Command-line entry points for bounded collection and local retrieval."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator

import typer
from pydantic import ValidationError

from .api_client import (
    APIError,
    APIRateLimited,
    APISchemaError,
    APIStatusError,
    MetadataClient,
)
from .catalog import Catalog
from .collector import Collector
from .config import Settings
from .embeddings import LocalSentenceTransformer
from .indexer import Indexer
from .search import SearchService
from .locking import DataBusy, data_lock
from .filter import FilterService

app = typer.Typer(
    help="Collect metadata and search a local gallery index.", no_args_is_help=True
)


@contextmanager
def _errors() -> Iterator[None]:
    """Print only controlled messages; chained exceptions may include credentials."""
    try:
        yield
    except (ValidationError, ValueError) as exc:
        # Validation errors can echo environment values; never display them.
        if isinstance(exc, ValidationError):
            message = "Invalid configuration; check VIBESEARCH_ settings."
        else:
            message = "Invalid configuration or arguments; check command options and VIBESEARCH_ settings."
        typer.echo(message, err=True)
        raise typer.Exit(2) from None
    except APISchemaError:
        typer.echo(
            "Metadata schema mismatch; collection stopped. Check the API contract.",
            err=True,
        )
        raise typer.Exit(5) from None
    except APIRateLimited:
        typer.echo(
            "Metadata API rate limit persisted; collection paused. Retry later.",
            err=True,
        )
        raise typer.Exit(4) from None
    except APIStatusError as exc:
        if exc.status_code in (401, 403):
            typer.echo(
                "Metadata API authentication or access failed; check API credentials and permissions.",
                err=True,
            )
            code = 3
        else:
            typer.echo(
                f"Metadata API unavailable (HTTP {exc.status_code}). Retry later.",
                err=True,
            )
            code = 4
        raise typer.Exit(code) from None
    except APIError:
        typer.echo("Metadata API unavailable; check connectivity and retry.", err=True)
        raise typer.Exit(4) from None
    except LookupError:
        typer.echo("No compatible local index; run 'vibesearch index' first.", err=True)
        raise typer.Exit(6) from None
    except DataBusy:
        typer.echo(
            "Data directory is busy; stop the server or other command first.", err=True
        )
        raise typer.Exit(8) from None
    except (OSError, sqlite3.Error):
        typer.echo(
            "Local data access failed; check the data directory and permissions.",
            err=True,
        )
        raise typer.Exit(7) from None
    except Exception:
        # Model and storage libraries may embed sensitive paths or arguments in errors.
        typer.echo(
            "Operation failed; check local model and index configuration.", err=True
        )
        raise typer.Exit(7) from None


def _provider(settings: Settings, *, offline: bool = False) -> LocalSentenceTransformer:
    return LocalSentenceTransformer(
        model_id=settings.embedding_model,
        revision=settings.embedding_revision,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
        normalize=settings.embedding_normalize,
        offline=offline,
    )


@app.callback()
def lock_command(ctx: typer.Context):
    """Serialize CLI operations; serve owns its lock through app lifespan."""
    if ctx.invoked_subcommand and ctx.invoked_subcommand != "serve":
        with _errors():
            guard = data_lock(Settings().data_dir)
            guard.__enter__()
            ctx.call_on_close(lambda: guard.__exit__(None, None, None))


@app.command()
def collect(
    max_items: int | None = typer.Option(
        None, min=1, max=100, help="Maximum newly fetched details (1–100)."
    ),
    max_pages: int | None = typer.Option(
        None, min=1, max=5, help="Maximum listing pages (1–5)."
    ),
    per_page: int | None = typer.Option(
        None, min=1, max=100, help="Listing page size (1–100)."
    ),
) -> None:
    """Collect a bounded sample of metadata; never load an embedding model."""
    with _errors():
        settings = Settings()
        items = max_items if max_items is not None else settings.collect_max_items
        pages = max_pages if max_pages is not None else settings.collect_max_pages
        size = per_page if per_page is not None else settings.per_page
        # Validate before constructing a network client or creating a catalog.
        if not 1 <= items <= 100 or not 1 <= pages <= 5 or not 1 <= size <= 100:
            raise ValueError("collection limits exceed the bounded pilot")
        user_agent = settings.user_agent
        authenticated = bool(settings.api_key)

        async def run():
            async with MetadataClient(
                origin=settings.api_origin,
                api_key=settings.api_key or None,
                user_agent=user_agent,
                list_per_minute=(
                    settings.list_rate_authenticated
                    if authenticated
                    else settings.list_rate_anonymous
                ),
                detail_per_minute=(
                    settings.detail_rate_authenticated
                    if authenticated
                    else settings.detail_rate_anonymous
                ),
                connect_timeout=settings.http_connect_timeout,
                read_timeout=settings.http_read_timeout,
                max_retries=settings.max_retries,
            ) as client:
                with Catalog(settings.catalog_path) as catalog:
                    return await Collector(
                        client, catalog, max_items=items, max_pages=pages, per_page=size
                    ).collect()

        result = asyncio.run(run())
        typer.echo(
            f"Run {result.run_id}: {result.status}; pages={result.pages_scanned}, "
            f"scanned={result.scanned}, deduplicated={result.deduplicated}, "
            f"fetched={result.fetched}, skipped={result.skipped}, "
            f"inaccessible={result.inaccessible}, pending={result.pending}"
        )


@app.command()
def index() -> None:
    """Index local details using the configured embedding contract; no API calls."""
    with _errors():
        settings = Settings()
        with Catalog(settings.catalog_path) as catalog:
            result = Indexer(
                catalog,
                _provider(settings),
                str(settings.chroma_path),
                template_version=settings.text_template_version,
            ).run()
        typer.echo(
            f"Collection galleries_{result.fingerprint[:16]}: indexed={result.indexed}, skipped={result.skipped}, deleted={result.deleted}"
        )


@app.command()
def reindex() -> None:
    """Explicitly build the collection for the configured embedding contract."""
    with _errors():
        settings = Settings()
        with Catalog(settings.catalog_path) as catalog:
            result = Indexer(
                catalog,
                _provider(settings),
                str(settings.chroma_path),
                template_version=settings.text_template_version,
            ).run()
        typer.echo(
            f"Contract galleries_{result.fingerprint[:16]}: indexed={result.indexed}, skipped={result.skipped}, deleted={result.deleted}"
        )
        typer.echo(
            "Reindex uses the configured contract; a changed fingerprint creates a separate collection."
        )


@app.command()
def search(
    query: str = typer.Argument(
        ..., help="Natural-language query for the local index."
    ),
    top_k: int = typer.Option(20, min=1, help="Maximum number of results."),
    display: str | None = typer.Option(None, help="full or id-only."),
    filters: str | None = typer.Option(
        None, help="Structured constraints as a JSON object."
    ),
) -> None:
    """Search the local index offline; report cosine distance (lower is better)."""
    with _errors():
        settings = Settings()
        mode = display if display is not None else settings.display_mode
        if mode not in ("full", "id-only") or not query.strip():
            raise ValueError("invalid display mode or empty query")
        with Catalog(settings.catalog_path) as catalog:
            extra = {}
            if filters is not None:
                constraints = json.loads(filters)
                if not isinstance(constraints, dict):
                    raise ValueError("filters must be an object")
                extra["eligible_ids"] = FilterService(catalog).eligible_ids(constraints)
            result = SearchService(
                catalog,
                _provider(settings, offline=True),
                str(settings.chroma_path),
                template_version=settings.text_template_version,
            ).search(query, top_k=top_k, display=mode, **extra)
        typer.echo(
            f"Local indexed corpus: {result.indexed_count} records; cosine_distance (lower is better)."
        )
        if result.pending_warning:
            typer.echo("Warning: pending indexing or deletions may make results stale.")
        if getattr(result, "missing_count", 0):
            typer.echo(
                f"Warning: {result.missing_count} eligible local records are missing from this contract; reindex for complete coverage."
            )
        if not result.hits:
            typer.echo("No matching local records.")
        for hit in result.hits:
            typer.echo(
                f"{hit.gallery_id}  pages={hit.num_pages}  cosine_distance={hit.cosine_distance:.6f}"
            )
            if mode == "full":
                typer.echo(f"  Title: {hit.title_display or hit.gallery_id}")
                if hit.tags:
                    typer.echo(
                        "  Tags: "
                        + ", ".join(
                            str(tag.get("name", ""))
                            if isinstance(tag, dict)
                            else str(tag)
                            for tag in hit.tags
                        )
                    )


@app.command()
def status() -> None:
    """Report local counts and stored contracts without a model or network request."""
    with _errors():
        settings = Settings()
        if not settings.catalog_path.exists():
            typer.echo("No local catalog yet. Run 'vibesearch collect' first.")
            return
        with Catalog(settings.catalog_path) as catalog:
            counts = catalog.counts()
            run = catalog.latest_run()
            errors = catalog.db.execute(
                "SELECT count(*) FROM galleries WHERE error_category IS NOT NULL"
            ).fetchone()[0]
            try:
                manifests = catalog.db.execute(
                    "SELECT fingerprint,manifest_json FROM index_manifests ORDER BY rowid DESC"
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
                manifests = []
        typer.echo(
            "Catalog: "
            + ", ".join(f"{key}={value}" for key, value in counts.items())
            + f", errors={errors}"
        )
        typer.echo(
            f"Last run: {run['run_id']} ({run['status']})" if run else "Last run: none"
        )
        if manifests:
            for fingerprint, raw in manifests:
                manifest = json.loads(raw)
                typer.echo(
                    f"Stored contract galleries_{fingerprint[:16]}: "
                    f"model={manifest['model_id']}, revision={manifest['revision']}, "
                    f"template={manifest['template_version']}"
                )
        else:
            typer.echo("Stored contracts: none; run 'vibesearch index' to build one.")
        matching = [
            fingerprint
            for fingerprint, raw in manifests
            if (manifest := json.loads(raw)).get("model_id") == settings.embedding_model
            and manifest.get("revision") == settings.embedding_revision
            and manifest.get("template_version") == settings.text_template_version
            and manifest.get("normalize") == settings.embedding_normalize
        ]
        if settings.embedding_revision and len(matching) == 1:
            typer.echo(
                f"Configured contract candidate: galleries_{matching[0][:16]} "
                "(model and full fingerprint not verified by status)."
            )
        else:
            typer.echo(
                "Active search contract unresolved without loading the model; run index/search to verify it."
            )


@app.command("filter")
def filter_catalog(
    filters: str = typer.Option(
        "{}", help="JSON constraints, e.g. artist/language/tags_all."
    ),
    limit: int = typer.Option(50, min=1, max=100),
    offset: int = typer.Option(0, min=0),
    display: str | None = typer.Option(None),
) -> None:
    """Exact structured search without model or network requests."""
    with _errors():
        settings = Settings()
        constraints = json.loads(filters)
        if not isinstance(constraints, dict):
            raise ValueError("filters must be an object")
        with Catalog(settings.catalog_path) as catalog:
            result = FilterService(catalog).list(
                constraints,
                limit=limit,
                offset=offset,
                display=display or settings.display_mode,
            )
        typer.echo(json.dumps(result, ensure_ascii=False))


@app.command()
def suggest(
    field: str, prefix: str = typer.Argument(""), limit: int = typer.Option(20, min=1, max=50)
) -> None:
    """Autocomplete values present in the eligible local catalog."""
    with _errors():
        settings = Settings()
        with Catalog(settings.catalog_path) as catalog:
            result = FilterService(catalog).suggest(field, prefix, limit)
        typer.echo(json.dumps(result, ensure_ascii=False))


@app.command("refresh-reader-metadata")
def refresh_reader_metadata(
    max_items: int | None = typer.Option(None, min=1, max=100),
) -> None:
    """Bounded descriptor refresh, only for existing non-readable records."""
    from .reader_refresh import refresh_metadata

    with _errors():
        settings = Settings()
        contact = settings.user_agent
        authenticated = bool(settings.api_key)

        async def run():
            async with MetadataClient(
                origin=settings.api_origin,
                api_key=settings.api_key or None,
                user_agent=contact,
                list_per_minute=settings.list_rate_authenticated
                if authenticated
                else settings.list_rate_anonymous,
                detail_per_minute=settings.detail_rate_authenticated
                if authenticated
                else settings.detail_rate_anonymous,
                connect_timeout=settings.http_connect_timeout,
                read_timeout=settings.http_read_timeout,
                max_retries=settings.max_retries,
            ) as client:
                with Catalog(settings.catalog_path) as catalog:
                    return await refresh_metadata(
                        catalog,
                        client,
                        max_items=max_items or settings.refresh_max_items_default,
                    )

        typer.echo(json.dumps(asyncio.run(run())))


@app.command()
def serve(
    host: str | None = typer.Option(None),
    port: int | None = typer.Option(None, min=1, max=65535),
) -> None:
    """Start the single-user local web UI and reader."""
    import ipaddress
    import uvicorn
    from .web.app import create_app

    with _errors():
        settings = Settings()
        if host is not None:
            settings.web_host = host
        if port is not None:
            settings.web_port = port
        try:
            local = ipaddress.ip_address(settings.web_host).is_loopback
        except ValueError:
            local = settings.web_host == "localhost"
        if not local and not settings.allow_non_loopback:
            raise ValueError("non-loopback binding requires explicit opt-in")
        if not local:
            typer.echo("Warning: non-loopback hosting is unsupported.", err=True)
        typer.echo(f"VibeSearch: http://{settings.web_host}:{settings.web_port}")
        uvicorn.run(
            create_app(settings=settings),
            host=settings.web_host,
            port=settings.web_port,
            workers=1,
            proxy_headers=False,
            access_log=False,
        )


if __name__ == "__main__":
    app()
