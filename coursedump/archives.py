"""Safe, idempotent extraction of ZIP course sources."""

from __future__ import annotations

import json
import os
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .manifest import Item, kind_of
from .util import atomic_write_json


class UnsafeArchive(RuntimeError):
    pass


MARKER_SUFFIX = ".unpacked.json"


def is_zip_source(source: str | Path) -> bool:
    path = Path(source).expanduser()
    return path.is_file() and path.suffix.lower() == ".zip"


def extraction_dir(path: Path) -> Path:
    return path.with_suffix("")


def marker_path(path: Path) -> Path:
    return path.with_name(path.name + MARKER_SUFFIX)


def _identity(path: Path) -> dict[str, int | str]:
    st = path.stat()
    return {
        "archive": str(path.resolve()),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
    }


def _safe_rel(info: zipfile.ZipInfo, strip_root: str = "") -> Path | None:
    """Return a safe member path, or None for the directory root."""
    name = info.filename
    if not name or "\x00" in name or "\\" in name:
        raise UnsafeArchive(f"unsafe ZIP member name: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise UnsafeArchive(f"ZIP-slip path: {name!r}")
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise UnsafeArchive(f"special files are not allowed in ZIP archives: {name!r}")
    parts = list(pure.parts)
    if strip_root and parts and parts[0] == strip_root:
        parts.pop(0)
    return Path(*parts) if parts else None


def _members(zf: zipfile.ZipFile, dest: Path) -> list[tuple[zipfile.ZipInfo, Path | None]]:
    infos = zf.infolist()
    files = [PurePosixPath(i.filename) for i in infos if i.filename and not i.is_dir()]
    strip_root = ""
    if files and all(len(p.parts) > 1 and p.parts[0] == dest.name for p in files):
        strip_root = dest.name

    checked = [(info, _safe_rel(info, strip_root)) for info in infos]
    root = dest.resolve()
    for _, rel in checked:
        if rel is None:
            continue
        target = (dest / rel).resolve()
        if target != root and root not in target.parents:
            raise UnsafeArchive(f"ZIP-slip path: {rel!s}")
    return checked


def list_items(path: Path) -> list[Item]:
    """Build an archive manifest from the central directory without extracting it."""
    dest = extraction_dir(path)
    with zipfile.ZipFile(path) as zf:
        checked = _members(zf, dest)
        return [
            Item(rel=rel.as_posix(), kind=kind_of(rel.as_posix()), size=info.file_size)
            for info, rel in checked
            if rel is not None and not info.is_dir()
        ]


@dataclass
class ZipPlanSource:
    path: Path
    is_local: bool = True
    probe_media: bool = False  # The central directory is available, but contents are not.

    def title(self) -> str:
        return self.path.stem

    def list(self) -> list[Item]:
        return list_items(self.path)


def extract(path: Path) -> Path:
    """Extract beside the archive and write the marker only after every file."""
    path = path.expanduser().resolve()
    dest = extraction_dir(path)
    marker = marker_path(path)
    identity = _identity(path)
    if marker.is_file() and dest.is_dir():
        try:
            if json.loads(marker.read_text(encoding="utf-8")) == identity:
                return dest
        except (OSError, json.JSONDecodeError):
            pass

    with zipfile.ZipFile(path) as zf:
        checked = _members(zf, dest)  # Validate every member before the first write.
        dest.mkdir(parents=True, exist_ok=True)
        for info, rel in checked:
            if rel is None:
                continue
            target = dest / rel
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".coursedump.tmp")
            try:
                tmp.unlink(missing_ok=True)
                with zf.open(info) as src, tmp.open("xb") as out:
                    shutil.copyfileobj(src, out)
                os.replace(tmp, target)
            finally:
                if tmp.exists():
                    tmp.unlink()
    atomic_write_json(marker, identity)
    return dest


def purge(path: Path) -> None:
    """Delete the source archive and its marker after an explicit user request."""
    path.unlink()
    marker = marker_path(path)
    if marker.exists():
        marker.unlink()
