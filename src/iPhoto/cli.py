"""Typer-based CLI entry point."""

from __future__ import annotations

from pathlib import Path
import sys

import typer
from rich import print

if __package__ in (None, ""):
    package_root = Path(__file__).resolve().parent.parent
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    from iPhoto import app as app_facade  # type: ignore  # pragma: no cover
else:
    from . import app as app_facade
from .cache.index_store import IndexStore
from .config import WORK_DIR_NAME
from .errors import AlbumNotFoundError, IPhotoError, LockTimeoutError, ManifestInvalidError
from .models.album import Album

app = typer.Typer(help="Folder-native photo manager with Live Photo support")
cover_app = typer.Typer(help="Manage album covers")
feature_app = typer.Typer(help="Manage featured assets")
app.add_typer(cover_app, name="cover")
app.add_typer(feature_app, name="feature")


def _handle_errors(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (AlbumNotFoundError, ManifestInvalidError, LockTimeoutError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        except IPhotoError as exc:
            typer.echo(f"Unexpected error: {exc}", err=True)
            raise typer.Exit(1) from exc

    return wrapper


@app.command()
@_handle_errors
def init(album_dir: Path = typer.Argument(Path.cwd(), exists=False)) -> None:
    """Initialise an album manifest if it does not exist."""

    album_dir.mkdir(parents=True, exist_ok=True)
    album = Album.open(album_dir)
    album.save()
    print(f"[green]Initialised album at {album_dir}")


@app.command()
@_handle_errors
def scan(album_dir: Path = typer.Argument(Path.cwd(), exists=True)) -> None:
    """Scan files and update the index cache."""

    rows = app_facade.rescan(album_dir)
    print(f"[green]Indexed {len(rows)} assets")


@app.command()
@_handle_errors
def pair(album_dir: Path = typer.Argument(Path.cwd(), exists=True)) -> None:
    """Rebuild Live Photo pairings."""

    groups = app_facade.pair(album_dir)
    print(f"[green]Paired {len(groups)} Live Photos")


@cover_app.command("set")
@_handle_errors
def cover_set(album_dir: Path, rel: str) -> None:
    """Set the album cover to the provided relative path."""

    album = app_facade.open_album(album_dir)
    album.set_cover(rel)
    album.save()
    print(f"[green]Set cover to {rel}")


@feature_app.command("add")
@_handle_errors
def feature_add(album_dir: Path, ref: str) -> None:
    """Add an item to the featured list."""

    album = app_facade.open_album(album_dir)
    album.add_featured(ref)
    album.save()
    print(f"[green]Added featured {ref}")


@feature_app.command("rm")
@_handle_errors
def feature_rm(album_dir: Path, ref: str) -> None:
    """Remove an item from the featured list."""

    album = app_facade.open_album(album_dir)
    album.remove_featured(ref)
    album.save()
    print(f"[green]Removed featured {ref}")


@app.command()
@_handle_errors
def report(album_dir: Path = typer.Argument(Path.cwd(), exists=True)) -> None:
    """Print a simple album report."""

    album = app_facade.open_album(album_dir)
    rows = list(IndexStore(album_dir).read_all())
    work_dir = album_dir / WORK_DIR_NAME
    links_path = work_dir / "links.json"
    if links_path.exists():
        import json

        with links_path.open("r", encoding="utf-8") as handle:
            groups = json.load(handle).get("live_groups", [])
    else:
        groups = [group.__dict__ for group in app_facade.pair(album_dir)]
    print(
        f"Album: {album.manifest.get('title')}\n"
        f"Assets: {len(rows)}\n"
        f"Live pairs: {len(groups)}"
    )


if __name__ == "__main__":  # pragma: no cover
    app()
