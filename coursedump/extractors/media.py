"""Video/audio to text. Existing adjacent subtitles take precedence over ASR."""

import re
from pathlib import Path

from ..util import read_text_best

_TS_LINE = re.compile(r"-->")
_VTT_TAG = re.compile(r"<[^>]+>")
_SUB_EXTS = (".vtt", ".srt")


def find_sibling_subs(media: Path) -> Path | None:
    """Map lesson.mp4 to forms such as lesson.srt, lesson.ru.vtt, or lesson.mp4.ru.vtt."""
    stems = {media.stem, media.name}
    best: Path | None = None
    for p in media.parent.iterdir():
        if p.suffix.lower() not in _SUB_EXTS:
            continue
        base = p.name[: -len(p.suffix)]
        lang = ""
        if "." in base:  # An optional language suffix may be present: lesson.ru.
            base_no_lang, _, lang = base.rpartition(".")
            if base_no_lang in stems:
                base = base_no_lang
        if base in stems:
            if lang.startswith("ru") or best is None:
                best = p
            if lang.startswith("ru"):
                break
    return best


def parse_subs(text: str) -> str:
    """Flatten SRT/VTT by removing numbers, timestamps, tags, and duplicate lines."""
    out: list[str] = []
    for line in text.splitlines():
        line = _VTT_TAG.sub("", line).strip()
        if (not line or _TS_LINE.search(line) or line.isdigit()
                or line.startswith(("WEBVTT", "NOTE", "STYLE", "Kind:", "Language:"))):
            continue
        if out and out[-1] == line:  # Automatically generated subtitles repeat lines.
            continue
        out.append(line)
    return "\n".join(out)


def extract(media: Path, asr) -> tuple[str, dict]:
    subs = find_sibling_subs(media)
    if subs is not None:
        body = parse_subs(read_text_best(subs))
        if body.strip():
            return body, {"method": "subs", "subs_file": subs.name}
    res = asr.transcribe(media)
    extra = {
        "method": "asr",
        "asr": f"{res.backend or asr.name}/{res.model or asr.model}",
        "language": res.language,
    }
    if res.attempts > 1:
        extra["asr_attempts"] = res.attempts
    if res.quality_warning:
        # This internal field reaches extractors.extract but not YAML. A visible
        # header is required instead of a quiet metadata key.
        extra["_quality_warning"] = res.quality_warning
    return res.markdown_body(), extra
