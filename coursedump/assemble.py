"""Build a complete course INDEX.md with structure, status, and text volume.

Per-item word counts are operational data, not decoration. A consumer that
selects material by file extension cannot distinguish an empty scanned PDF
from a content-rich slide deck. The index therefore makes empty extraction and
substantial document text mechanically visible.
"""

import json
from collections import Counter
from pathlib import Path

from .manifest import Item
from .extractors.asr import QUALITY_HEADER_PREFIX
from .util import atomic_write_text, human_dur

# Below this threshold, the file is effectively empty: an image-only scan,
# cover, or title page. It is neither skipped nor failed; consumers decide.
EMPTY_TEXT_WORDS = 50


def _last_errors(course_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    ej = course_dir / "errors.jsonl"
    if ej.exists():
        for line in ej.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                out[e.get("rel", "")] = e.get("error", "")
            except json.JSONDecodeError:
                continue
    return out


def write_index(course_dir: Path, title: str, items: list[Item]) -> dict:
    errors = _last_errors(course_dir)
    text_dir = course_dir / "text"

    done = pending = 0
    skipped = Counter()
    lost: list[tuple[str, str]] = []   # (relative path, reason) not converted to text
    empty: list[tuple[str, int]] = []  # (relative path, words) extracted but empty
    quality: list[tuple[str, str]] = []  # ASR quality gate failed to recover speech
    by_dir: dict[str, list[str]] = {}

    for it in items:
        d = str(Path(it.rel).parent)
        d = "" if d == "." else d
        if it.skip:
            skipped[it.skip] += 1
            if it.skip != "sibling":  # Sibling subtitles were consumed by media extraction.
                lost.append((it.rel, it.skip))
            continue
        md = text_dir / it.target
        name = Path(it.target).name.removesuffix(".md")
        if md.exists():
            done += 1
            dur = ""
            words = 0
            quality_notice = ""
            if md.stat().st_size:
                contents = md.read_text(encoding="utf-8")
                parts = contents.split("---", 2)
                head = parts[1] if len(parts) > 2 else ""
                body = parts[2] if len(parts) > 2 else parts[0]
                words = len(body.split())
                quality_notice = next(
                    (
                        line
                        for line in contents.splitlines()[:40]
                        if line.startswith(QUALITY_HEADER_PREFIX)
                    ),
                    "",
                )
                for line in head.splitlines():
                    if line.startswith("duration:"):
                        dur = " (" + human_dur(int(line.split(":", 1)[1])) + ")"
            link = str(Path("text") / it.target).replace(" ", "%20")
            mark = " - NO TEXT" if words < EMPTY_TEXT_WORDS else ""
            if quality_notice:
                quality.append((it.rel, quality_notice))
                mark += " - QUALITY: ASR LOOP"
            if words < EMPTY_TEXT_WORDS:
                empty.append((it.rel, words))
            by_dir.setdefault(d, []).append(
                f"- [x] [{name}]({link}){dur} - {words} words{mark}")
        else:
            pending += 1
            err = errors.get(it.rel, "")
            mark = f" - ERROR: {err[:120]}" if err else ""
            by_dir.setdefault(d, []).append(f"- [ ] {name} ({it.kind}){mark}")

    lost_kinds = {k: v for k, v in skipped.items() if k != "sibling"}
    lines = [f"# {title}", ""]
    total = done + pending
    lines.append(f"Extracted {done}/{total}."
                 + (f" Not converted to text: {lost_kinds}." if lost_kinds else ""))
    lines.append("")
    for d in sorted(by_dir):
        if d:
            lines.append(f"## {d}")
        lines.extend(by_dir[d])
        lines.append("")

    if lost:  # Name every skipped archive, binary, image, or blacklist match.
        lines.append(f"## Not converted to text ({len(lost)})")
        lines.append("")
        lines.extend(f"- {rel} - {reason}" for rel, reason in lost)
        lines.append("")

    if empty:  # Extraction succeeded, but only a cover, title page, or image-only scan was found.
        lines.append(f"## Extracted with no usable text ({len(empty)})")
        lines.append("")
        lines.append(f"Fewer than {EMPTY_TEXT_WORDS} words. This is usually an image-only PDF scan. "
                     "The file remains available so downstream consumers can decide how to use it.")
        lines.append("")
        lines.extend(f"- {rel} - {n} words" for rel, n in empty)
        lines.append("")

    if quality:
        lines.append(f"## Requires another ASR pass ({len(quality)})")
        lines.append("")
        lines.append(
            "The quality gate exhausted its retries. Repeated text in these files is not "
            "recovered speech."
        )
        lines.append("")
        lines.extend(f"- {rel} - {notice}" for rel, notice in quality)
        lines.append("")

    atomic_write_text(course_dir / "INDEX.md", "\n".join(lines))
    return {"done": done, "total": total,
            "skipped": sum(lost_kinds.values()), "skipped_kinds": lost_kinds,
            "empty_text": len(empty), "quality_warnings": len(quality)}
