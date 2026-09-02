"""Course catalog recording what was acquired, from where, and when.

The catalog survives removal of local source media. User-maintained title,
URL, mirror, and note columns are preserved during rebuilds. Acquired time is
derived from the oldest source mtime while the input exists and then retained.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import manifest
from .util import atomic_write_text, human_size


COLUMNS = ("slug", "title", "url", "mirror", "acquired", "lessons", "media", "in", "note")
KEPT = ("title", "url", "mirror", "note")
MISSING = "-"


def _course_dirs(data: Path) -> list[str]:
    """Return local course slugs from snapshots and unprocessed inbox entries.

    The local-adapter filter identifies course records rather than individual
    remote posts. Catalog traversal remains intentionally flat and does not
    inspect staging.
    """
    slugs = set()
    out = data / "out"
    if out.is_dir():
        for d in sorted(out.iterdir()):
            source = d / "source.json"
            if not (d.is_dir() and source.is_file()):
                continue
            try:
                meta = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if meta.get("adapter") == "local":
                slugs.add(d.name)
    inbox = data / "in"
    if inbox.is_dir():
        slugs.update(d.name for d in inbox.iterdir() if d.is_dir())
    return sorted(slugs)


def _lessons(course: Path) -> str:
    mpath = course / "manifest.jsonl"
    if not mpath.is_file():
        return MISSING
    try:
        work = [item for item in manifest.load(mpath) if item.target]
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return MISSING
    done = sum(1 for item in work if (course / "text" / item.target).is_file())
    return f"{done}/{len(work)}"


def _media(course: Path) -> str:
    """Return manifest source size, which survives deletion of raw files."""
    mpath = course / "manifest.jsonl"
    if not mpath.is_file():
        return MISSING
    try:
        total = sum(item.size or 0 for item in manifest.load(mpath))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return MISSING
    return human_size(total) if total else MISSING


def _acquired(inbox: Path) -> str:
    """Return the oldest mtime among files in an inbox directory."""
    if not inbox.is_dir():
        return MISSING
    stamps = [p.stat().st_mtime for p in inbox.rglob("*") if p.is_file()]
    if not stamps:
        return MISSING
    return time.strftime("%Y-%m-%d", time.localtime(min(stamps)))


def read(path: Path) -> dict[str, dict[str, str]]:
    """Read columns from the file header so older catalogs do not shift."""
    if not path.is_file():
        return {}
    header = None
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        cells = line.split("\t")
        if header is None:
            header = cells if cells[0] == "slug" else list(COLUMNS)
            if cells[0] == "slug":
                continue
        row = dict(zip(header, cells + [""] * (len(header) - len(cells))))
        if row.get("slug"):
            rows[row["slug"]] = row
    return rows


def build(data: Path, previous: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    rows = []
    for slug in _course_dirs(data):
        old = previous.get(slug, {})
        inbox = data / "in" / slug
        course = data / "out" / slug
        acquired = old.get("acquired", "").strip() or _acquired(inbox)
        rows.append(
            {
                "slug": slug,
                "title": old.get("title", "").strip() or MISSING,
                "url": old.get("url", "").strip() or MISSING,
                "mirror": old.get("mirror", "").strip() or MISSING,
                "acquired": acquired or MISSING,
                "lessons": _lessons(course),
                "media": _media(course),
                "in": "yes" if inbox.is_dir() else "no",
                "note": old.get("note", "").strip(),
            }
        )
    for slug, old in sorted(previous.items()):
        # Keep a course record even after both its snapshot and inbox directory
        # are removed; recording prior acquisition is the catalog's purpose.
        if any(row["slug"] == slug for row in rows):
            continue
        row = {column: old.get(column, "") for column in COLUMNS}
        row["in"] = "no"
        rows.append(row)
    return sorted(rows, key=lambda row: row["slug"])


def render(rows: list[dict[str, str]]) -> str:
    head = (
        "# coursedump course catalog: one TAB-separated row per course.\n"
        "# Users maintain title/url/mirror/note; coursedump catalog preserves them\n"
        "# and recalculates other columns. acquired is recorded once while in/\n"
        "# exists. Individual remote posts are excluded because their working\n"
        "# snapshots live under staging/<source>/<post>/ and source.json retains\n"
        "# each post URL.\n"
    )
    lines = ["\t".join(COLUMNS)]
    lines += ["\t".join(row.get(column, "") for column in COLUMNS) for row in rows]
    return head + "\n".join(lines) + "\n"


def refresh(data: Path, path: Path | None = None) -> tuple[Path, list[dict[str, str]]]:
    path = path or data / "catalog.tsv"
    rows = build(data, read(path))
    atomic_write_text(path, render(rows))
    return path, rows
