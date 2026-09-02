"""Dispatch an item and local file to Markdown with front matter."""

from datetime import datetime
from pathlib import Path

from ..manifest import Item, VIDEO, AUDIO, OFFICE, TEXT, SUBS, WEB
from ..util import clean_text, ffprobe_duration, read_text_best
from . import media, docs

_KNOWN_EXT = VIDEO | AUDIO | OFFICE | TEXT | SUBS | WEB | {".pdf"}


def _title(target: str) -> str:
    """Convert '01/Lesson 1.mp4.md' to 'Lesson 1' by removing known suffixes."""
    name = Path(target).name.removesuffix(".md")
    if Path(name).suffix.lower() in _KNOWN_EXT:
        name = name[: -len(Path(name).suffix)]
    return name


def _frontmatter(item: Item, extra: dict) -> str:
    lines = ["---", f"source: {item.rel}"]
    for k, v in extra.items():
        if v not in ("", None):
            lines.append(f"{k}: {v}")
    lines.append(f"created: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("---")
    return "\n".join(lines)


def extract(item: Item, path: Path, asr) -> str:
    """Return complete Markdown. An exception is isolated to this item and logged."""
    title = _title(item.target)
    extra: dict = {}

    if item.kind in ("video", "audio"):
        body, extra = media.extract(path, asr)
        extra["duration"] = int(ffprobe_duration(path) or 0) or None
    elif item.kind == "subs":
        body = media.parse_subs(read_text_best(path))
        extra = {"method": "subs"}
    elif item.kind == "pdf":
        body = docs.pdf_markdown(path)
    elif item.kind == "office":
        body = docs.office_markdown(path)
    elif item.kind == "html":
        body = docs.html_markdown(path)
    elif item.kind == "text":
        body = read_text_best(path)
    else:
        raise ValueError(f"no extractor for kind={item.kind}")

    quality_warning = extra.pop("_quality_warning", "")
    warning = f"\n{quality_warning}\n" if quality_warning else ""
    # One cleanup path covers every extractor because a BOM in text, DEL from
    # EPUB, or ZWSP from HTML can break the same downstream consumer.
    return f"{_frontmatter(item, extra)}\n# {title}\n{warning}\n{clean_text(body).strip()}\n"
