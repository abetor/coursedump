"""Atomic writes, subprocess helpers, and small utilities."""

import json
import os
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path


class ToolMissing(RuntimeError):
    pass


def require_tool(name: str, hint: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ToolMissing(f"{name} was not found. Install it with: {hint}")
    return path


def read_text_best(path: Path) -> str:
    """Read text with encoding detection.

    Legacy subtitles are often CP1251; UTF-8 with ignored errors would silently
    discard Cyrillic content.
    """
    data = path.read_bytes()
    for enc in ("utf-8", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def clean_text(text: str) -> str:
    """Remove control and formatting characters from snapshot bodies.

    Downstream validation rejects a whole document for any C* character except
    newline and tab, or for Zl/Zp. Normalize CRLF and Zl/Zp to newline, remove
    BOM, DEL, C0/C1, ZWSP, soft hyphen, surrogates, and private-use characters,
    and retain Zs characters such as NBSP because they are valid spaces.
    """
    text = (text.replace("\r\n", "\n").replace("\r", "\n")
            .replace("\u2028", "\n").replace("\u2029", "\n"))
    if all(line.isprintable() for line in text.replace("\t", " ").split("\n")):
        return text  # Fast path: do not rebuild already clean text.
    return "".join(
        ch for ch in text
        if ch in "\n\t" or ch.isprintable() or unicodedata.category(ch) == "Zs")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.rename(tmp, path)


def atomic_write_json(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=1))


def append_jsonl(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ffprobe_duration(path: Path) -> float | None:
    """Return media duration in seconds, or None when probing fails."""
    p = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    try:
        return float(p.stdout.strip())
    except ValueError:
        return None


def ffprobe_has_audio(path: Path) -> bool | None:
    """Return whether the file has audio, or None if ffprobe cannot decide.

    This distinguishes "nothing to transcribe" from transcription failure. A
    silent video can make ffmpeg return the same status as a corrupt file, but
    the pipeline must handle those cases differently.
    """
    p = run([
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    if p.returncode != 0:
        return None
    return bool(p.stdout.strip())


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def human_dur(sec: float) -> str:
    sec = int(sec)
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h}:{m:02d}:{sec % 60:02d}" if h else f"{m}:{sec % 60:02d}"


_SLUG_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def slugify(title: str) -> str:
    """Build a course directory name while preserving safe Unicode."""
    s = _SLUG_BAD.sub(" ", title)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:120] or "course"
