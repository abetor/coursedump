"""Item records, type detection, target paths, and manifest.jsonl."""

import hashlib
import json
from dataclasses import dataclass, asdict
from pathlib import Path

from . import normalize
from .util import atomic_write_text

VIDEO = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts", ".flv", ".wmv", ".mpg", ".mpeg"}
AUDIO = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
OFFICE = {".docx", ".pptx", ".xlsx", ".doc", ".ppt", ".xls", ".epub", ".rtf"}
TEXT = {".txt", ".md"}
SUBS = {".srt", ".vtt"}
WEB = {".html", ".htm", ".mht", ".mhtml"}
IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".svg"}


def kind_of(rel: str) -> str:
    ext = Path(rel).suffix.lower()
    if ext in VIDEO:
        return "video"
    if ext in AUDIO:
        return "audio"
    if ext == ".pdf":
        return "pdf"
    if ext in OFFICE:
        return "office"
    if ext in TEXT:
        return "text"
    if ext in SUBS:
        return "subs"
    if ext in WEB:
        return "html"
    if ext in IMAGE:
        return "image"
    return "other"


# File types that can actually become text.
EXTRACTABLE = {"video", "audio", "pdf", "office", "text", "subs", "html"}


@dataclass
class Item:
    rel: str                 # Path within the source; yt-dlp items omit the extension.
    kind: str
    size: int = 0
    remote: str = ""         # Adapter fetch key (URL or remote path); empty means local.
    skip: str = ""           # Exclusion reason: blacklist, image, or other.
    target: str = ""         # Relative output Markdown path inside text/.
    index: int = 0           # One-based playlist position for yt-dlp --playlist-items;
                             # zero means the item uses its own remote URL.
    vid: str = ""            # Stable source media ID, also embedded in rel so
                             # identity does not depend on playlist position.
    title: str = ""          # Raw source title. rel is filesystem-safe and includes
                             # index and ID; corpus naming uses the raw title.

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _media_stems(items: list[Item]) -> set[tuple[str, str]]:
    out = set()
    for it in items:
        if it.kind in ("video", "audio"):
            p = Path(it.rel)
            d = p.parent.as_posix()
            out.add((d, p.stem))
            out.add((d, p.name))
    return out


def _is_sibling_subs(it: Item, stems: set[tuple[str, str]]) -> bool:
    p = Path(it.rel)
    d = p.parent.as_posix()
    base = p.stem
    if (d, base) in stems:
        return True
    if "." in base:  # Optional language suffix, for example lesson.ru.vtt.
        return (d, base.rpartition(".")[0]) in stems
    return False


def finalize(items: list[Item]) -> list[Item]:
    """Assign skip/target values and resolve normalized-name collisions."""
    stems = _media_stems(items)
    taken: dict[str, str] = {}
    for it in items:
        if it.kind not in EXTRACTABLE:
            it.skip = it.kind  # Track images and other files without extracting them.
            continue
        if it.kind == "subs" and _is_sibling_subs(it, stems):
            it.skip = "sibling"  # The media extractor will consume this subtitle.
            continue
        if normalize.is_blacklisted(it.rel):
            it.skip = "blacklist"
            continue
        target = normalize.clean_rel(it.rel) + ".md"
        if target in taken and taken[target] != it.rel:
            # The suffix must be deterministic across runs. Otherwise resume
            # re-extracts the item and creates an orphan (hash() is salted).
            digest = hashlib.sha1(it.rel.encode("utf-8")).hexdigest()[:6]
            target = f"{target[:-len('.md')]} ({digest}).md"
        taken[target] = it.rel
        it.target = target
    return items


def save(items: list[Item], path: Path) -> None:
    atomic_write_text(path, "\n".join(it.to_json() for it in items) + "\n")


def load(path: Path) -> list[Item]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            items.append(Item(**json.loads(line)))
    return items
