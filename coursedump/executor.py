"""Reconcile desired manifest items with files already present.

There is no separate completion state: an existing text/<target> means the item
is complete. Atomic writes make interruption and process termination safe.
"""

import hashlib
import json
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic

from rich.console import Console
from rich.markup import escape
from rich.progress import (Progress, SpinnerColumn, TextColumn, BarColumn,
                           TaskProgressColumn, TimeElapsedColumn)

from . import manifest, sources, assemble
from .extractors import extract
from .space import ensure_space, NoSpace
from .util import (atomic_write_text, atomic_write_json, append_jsonl,
                   human_size, human_dur, ffprobe_duration, ffprobe_has_audio,
                   slugify)
from .normalize import clean_component

console = Console()

_EST_SPEED = 10.0  # Initial real-time speed estimate; later ETA uses observed speed.


@dataclass
class Opts:
    out_root: Path
    asr_backend: str = "mlx"
    model: str = "large-v3-turbo"
    language: str = ""
    purge_video: bool = False
    cookies_from_browser: str = ""
    # Source profiles can be disabled only through explicit flags.
    full_video: bool = False
    no_throttle: bool = False
    # One pacer belongs to one invocation and all of its sources. Adapter-local
    # state would miss cross-source waits; module state would leak across runs.
    pacer: sources.RunPacer = field(default_factory=sources.RunPacer)


def resolve(src, course_dir: Path) -> list[manifest.Item]:
    """Build a fresh manifest, falling back to the cached one if unavailable."""
    mpath = course_dir / "manifest.jsonl"
    try:
        items = manifest.finalize(src.list())
        course_dir.mkdir(parents=True, exist_ok=True)
        manifest.save(items, mpath)
        return items
    except Exception as e:
        if mpath.exists():
            console.print(f"[yellow]resolve failed ({escape(str(e))}); "
                          "using the cached manifest[/yellow]")
            items = manifest.load(mpath)
            # Adopt the cached canonical address so a wrapper or short URL
            # cannot silently change source-profile behavior during resume.
            adopt = getattr(src, "adopt_cached", None)
            if adopt is not None:
                adopt(items)
            return items
        raise


def course_dir_for(out_root: Path, title: str, source_str: str) -> Path:
    """Choose a title-based directory, adding a hash if another source owns it."""
    base = slugify(title)
    d = out_root / base
    sj = d / "source.json"
    if sj.exists():
        try:
            existing = json.loads(sj.read_text(encoding="utf-8")).get("source")
        except Exception:
            existing = None
        if existing is not None and existing != source_str:
            d = out_root / f"{base}-{hashlib.sha1(source_str.encode('utf-8')).hexdigest()[:6]}"
    return d


def run_course(source_str: str, opts: Opts) -> dict:
    # Restore browser selection and title before the first source request so an
    # authorized source can resume without repeating flags.
    saved_dir, saved = saved_state(opts.out_root, source_str)
    src = sources.detect(source_str,
                         opts.cookies_from_browser or saved.get("cookies") or "",
                         full_video=opts.full_video, no_throttle=opts.no_throttle,
                         pacer=opts.pacer)
    if saved_dir is not None and saved.get("title"):
        title, course_dir = saved["title"], saved_dir
    else:
        title = clean_component(src.title())
        course_dir = course_dir_for(opts.out_root, title, source_str)
    raw_dir = course_dir / "raw"
    text_dir = course_dir / "text"

    items = resolve(src, course_dir)
    atomic_write_json(course_dir / "source.json", {
        "source": source_str, **src.describe(),
        "title": title, "resolved": datetime.now().isoformat(timespec="seconds"),
    })

    todo = [it for it in items if it.target and not (text_dir / it.target).exists()]
    total_size = sum(it.size for it in todo)
    console.print(f"[bold]{escape(title)}[/bold]: {len(items)} files, "
                  f"{len(todo)} left to extract (~{human_size(total_size)})")

    lost = [it for it in items if it.skip and it.skip != "sibling"]
    if lost:  # Show skipped archives, binaries, images, and blacklist matches.
        kinds = Counter(it.skip for it in lost)
        console.print(f"[yellow]not converted to text ({len(lost)}): "
                      + ", ".join(f"{k} {v}" for k, v in kinds.items())
                      + " - itemized in INDEX.md[/yellow]")

    # Remove temporary fragments left by earlier failures.
    for tmp in text_dir.rglob("*.tmp") if text_dir.exists() else []:
        tmp.unlink()

    counts: dict = {"done": 0, "errors": 0, "no_audio_rels": []}
    if todo:
        _process(src, opts, todo, course_dir, title, counts)

    # Media with no audio contains nothing to transcribe. Mark only that case so
    # a silent clip does not keep an otherwise complete snapshot incomplete.
    # Network, storage, and corrupt-file failures still leave targets pending.
    # Apply the mark here because resolve rebuilds the manifest on every run.
    if counts["no_audio_rels"]:
        silent = set(counts["no_audio_rels"])
        for it in items:
            if it.rel in silent and it.target:
                it.skip = "no_audio"
                it.target = ""
        manifest.save(items, course_dir / "manifest.jsonl")
        console.print(f"[yellow]no audio track ({len(silent)}): nothing to transcribe; "
                      "marked no_audio in the manifest and itemized in errors.jsonl[/yellow]")

    stats = assemble.write_index(course_dir, title, items)
    stats.update(errors=counts["errors"], course=str(course_dir))
    return stats


def _durations(src, todo, raw_dir) -> list[float]:
    """Precompute media durations for progress weights and ETA; zero is unknown."""
    out = []
    for it in todo:
        d = 0.0
        if it.kind in ("video", "audio"):
            p = src.fetched(it, raw_dir)  # Local paths exist; remote paths do not until fetched.
            if p is not None and p.exists():
                d = ffprobe_duration(p) or 0.0
        out.append(d)
    return out


def _process(src, opts, todo, course_dir: Path, title: str, counts: dict) -> None:
    """Render course progress with ETA and the current item.

    Errors print above the progress display and remain in logs; transient status
    stays in the live display.
    """
    raw_dir, text_dir = course_dir / "raw", course_dir / "text"
    durs = _durations(src, todo, raw_dir)
    weights = [max(d, 1.0) if d else 1.0 for d in durs]  # Media uses duration; others approximate zero.
    total_weight = sum(weights)
    n = len(todo)

    asr = None       # Lazy so a document-only course does not load a model.
    done_weight = media_done = 0.0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        overall = progress.add_task(escape(title), total=total_weight)
        start = monotonic()

        for i, it in enumerate(todo, 1):
            dur = durs[i - 1]
            elapsed = monotonic() - start
            speed = (media_done / elapsed) if media_done and elapsed else _EST_SPEED
            det = f" ({human_dur(dur)}, ~{human_dur(dur / speed)})" if dur else ""
            item = progress.add_task(escape(f"[{i}/{n}] {Path(it.rel).name}{det}"),
                                     total=None)
            if not console.is_terminal:
                # Rich progress becomes one final line in non-terminal logs, so
                # emit one line per item to keep long runs observable and update
                # the log mtime for external stall detection.
                progress.console.print(
                    f"[{datetime.now():%H:%M}] {i}/{n} {Path(it.rel).name}{det}",
                    markup=False, highlight=False, soft_wrap=True)  # Do not wrap long names.
            path = None  # Needed to inspect the file if extraction fails.
            try:
                path = src.fetched(it, raw_dir)
                if path is None:
                    ensure_space(opts.out_root, it.size)
                    path = src.fetch(it, raw_dir)

                if it.kind in ("video", "audio") and asr is None:
                    from .extractors.asr import get_backend
                    asr = get_backend(opts.asr_backend, opts.model, opts.language)
                md = extract(it, path, asr)
                atomic_write_text(text_dir / it.target, md)
                counts["done"] += 1
                if dur:
                    media_done += dur

                # Purge downloaded media only, never a user's original source.
                if (opts.purge_video and not src.is_local
                        and it.kind in ("video", "audio") and path.is_relative_to(raw_dir)):
                    path.unlink()

            except NoSpace:
                raise  # No later item can succeed safely; stop cleanly.
            except Exception as e:
                counts["errors"] += 1
                # Inspect the file rather than the error text because ffmpeg can
                # use the same exit status for no audio and corrupt containers.
                if (it.kind in ("video", "audio") and path is not None
                        and path.exists() and ffprobe_has_audio(path) is False):
                    counts.setdefault("no_audio_rels", []).append(it.rel)
                progress.console.print(
                    f"[red]error: {escape(it.rel)}: {escape(str(e))}[/red]")
                append_jsonl(course_dir / "errors.jsonl", {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "rel": it.rel, "error": str(e) or type(e).__name__,
                    "trace": traceback.format_exc(limit=3),
                })
            finally:
                progress.remove_task(item)
                done_weight += weights[i - 1]
                elapsed = monotonic() - start
                rate = done_weight / elapsed if elapsed else 0
                eta = (total_weight - done_weight) / rate if rate else 0
                progress.update(overall, completed=done_weight,
                                description=escape(f"{title} · about {human_dur(eta)} remaining"))


def _course_state(d: Path) -> dict | None:
    """Return this directory's readable source.json, or None."""
    sj = d / "source.json"
    if not sj.exists():
        return None
    try:
        return json.loads(sj.read_text(encoding="utf-8"))
    except Exception:
        return None  # A broken source.json must not abort all resume discovery.


def _course_states(out_root: Path):
    """Yield ``(course directory, source.json)`` for started courses.

    Scan exactly two levels. Normal snapshots are flat (`out/<slug>/`), while
    corpus staging groups posts below a source (`staging/boosty-<blog>/<post>/`).
    A flat scan would miss every staged snapshot, causing `completed_course` to
    return None and a resumed run to contact the source for data already on disk.
    Do not descend further or scan inside complete courses: their source.json is
    at the course root, while raw/ can contain thousands of nested files.
    """
    if not out_root.exists():
        return
    try:
        top = sorted(out_root.iterdir())
    except OSError:
        return
    for d in top:
        st = _course_state(d)
        if st is not None:
            yield d, st
            continue
        if not d.is_dir():
            continue
        try:
            nested = sorted(d.iterdir())
        except OSError:
            continue
        for sub in nested:
            st = _course_state(sub)
            if st is not None:
                yield sub, st


def known_courses(out_root: Path) -> list[tuple[str, Path]]:
    """Return courses with source.json that can resume without arguments."""
    return [(st["source"], d) for d, st in _course_states(out_root) if st.get("source")]


def completed_course(out_root: Path, source_str: str) -> Path | None:
    """Return a course directory only when all of its items are extracted.

    Corpus publication can then finish an interrupted export from its complete
    on-disk snapshot without contacting the source again.
    """
    d, _ = saved_state(out_root, source_str)
    mpath = d / "manifest.jsonl" if d is not None else None
    if mpath is None or not mpath.exists():
        return None
    items = [it for it in manifest.load(mpath) if it.target]
    if items and all((d / "text" / it.target).exists() for it in items):
        return d
    return None


def saved_state(out_root: Path, source_str: str) -> tuple[Path | None, dict]:
    """Return a matching started course and source.json, or ``(None, {})``.

    This lookup runs before contacting the source because the saved state can
    contain the browser profile reference and title needed for an authorized
    private source.
    """
    wanted = _local_path_key(source_str)
    for d, st in _course_states(out_root):
        if st.get("source") == source_str:
            return d, st
        # Legacy snapshots store a relative `source` (`in/<slug>/`) and an
        # absolute `root`. Match a local course by either normalized path so a
        # resume does not create a duplicate directory with a hash suffix.
        if wanted is not None and st.get("adapter", "local") == "local":
            for key in ("root", "source"):
                if _local_path_key(st.get(key)) == wanted:
                    return d, st
    return None, {}


def _local_path_key(value: object) -> str | None:
    """Return a normalized absolute path for a local source, or None."""
    if not isinstance(value, str) or not value or "://" in value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        return None
    try:
        return str(path.resolve())
    except OSError:
        return str(path)
