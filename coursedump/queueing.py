"""File-backed queue for sequential coursedump runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import archives, catalog, executor, manifest, planning
from .extractors.asr import QUALITY_HEADER_PREFIX
from .util import atomic_write_json


TRANSIENT_WAIT = 60
TRANSIENT_RETRIES = 12
DETACH_START_TIMEOUT = 1.0
STATUS_RESULTS_LIMIT = 100
HOOK_TIMEOUT = 30
HOOK_TEXT_LIMIT = 1000
HOOK_LINE_LIMIT = 200
HOOK_LINES_LIMIT = 3
HOOK_SUBJECT_LIMIT = 40
FAILURE_REASON_LIMIT = 200
DEFAULT_SPEED_RATIO = 12.0
SPEED_WINDOW = 3
QUEUE_STARTED_DEBOUNCE = 5.0
# Notification contract v2: legacy event kind -> envelope class.
HOOK_CLASSES = {"progress": "event", "done": "event", "fail": "alert", "info": "ack"}
HOOK_ACTION_PREFIXES = ("what to do: ",)
SNAPSHOT_FLOW = "coursedump"
SNAPSHOT_STAGE = {"i": 1, "n": 1, "name": "asr"}

_sleep = time.sleep
_REMOTE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://|^[\w.-]+:")
_INLINE_COMMENT = re.compile(r"\s+#.*$")


class QueueError(RuntimeError):
    pass


class QueueLocked(QueueError):
    pass


class QueueCounts(dict[str, int]):
    def __init__(self, **values: int):
        super().__init__(values)
        self.new_failures = 0


@dataclass(frozen=True)
class QueueLine:
    lineno: int
    raw: str


def read_queue(path: Path) -> list[QueueLine]:
    """Read slug, path, or URL entries with shell-like inline comments.

    A leading ``#``, or one preceded by whitespace, starts a comment through
    the end of the line. A ``#`` without preceding whitespace, as in a URL
    fragment, remains part of the entry. This prevents an inline note from
    becoming part of a pseudo-slug and producing false failures.
    """
    if not path.is_file():
        raise QueueError(f"queue not found: {path}")
    lines = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = _INLINE_COMMENT.sub("", raw).strip()
        if not value or value.startswith("#"):
            continue
        lines.append(QueueLine(lineno, value))
    return lines


def resolve_source(line: QueueLine, root: Path, queue_path: Path) -> str:
    """Resolve a slug under ``in`` while preserving an explicit path or URL."""
    raw = line.raw
    if _REMOTE.match(raw):
        return raw
    path = Path(raw).expanduser()
    if path.is_absolute():
        return str(path.resolve())
    if "/" in raw or raw.startswith("."):
        return str((queue_path.parent / path).resolve())
    direct = root / "in" / raw
    zipped = (
        direct
        if direct.suffix.lower() == ".zip"
        else direct.with_name(direct.name + ".zip")
    )
    # After the first extraction, both the directory and archive exist. The
    # marker proves they represent one source. Keep addressing the archive so
    # a resumed run with --purge-zip can still delete it after success.
    if zipped.exists() and (not direct.exists() or archives.marker_path(zipped).is_file()):
        return str(zipped.resolve())
    if direct.exists():
        return str(direct.resolve())
    return str(direct.resolve())


def run_source(source: str) -> str:
    path = Path(source).expanduser()
    if archives.is_zip_source(path):
        return str(archives.extraction_dir(path.resolve()))
    return source


def local_source_gone(source: str) -> bool:
    """Return whether a local source was removed after its snapshot was made.

    Remote URL and rclone sources are not checked because that would require
    network access. The queue entry remains as evidence that the course existed;
    without this check every periodic run would fail on intentionally removed
    local media.
    """
    if _REMOTE.match(source):
        return False
    return not Path(source).expanduser().exists()


def _course_dir(out: Path, source: str, raw: str = "") -> Path | None:
    saved, _ = executor.saved_state(out, source)
    if saved is not None:
        return saved
    # Older queues addressed a local course by slug. The name fallback applies
    # only to local sources. A basename alone is insufficient because output
    # from a different source with the same name must not count as progress.
    source_path = Path(source)
    if source_path.is_absolute() and source_path.parent.name == "in":
        raw_name = Path(raw or source).name
        candidate_name = Path(raw_name).stem if raw_name.lower().endswith(".zip") else raw_name
        candidate = out / candidate_name
        if candidate.is_dir():
            try:
                saved_state = json.loads((candidate / "source.json").read_text(encoding="utf-8"))
            except (AttributeError, OSError, json.JSONDecodeError):
                return None
            # Older snapshots store a relative path in `source` and an absolute
            # root in `root`. Compare both forms.
            for key in ("source", "root"):
                saved_source = saved_state.get(key) if isinstance(saved_state, dict) else None
                if not saved_source:
                    continue
                saved_path = Path(str(saved_source)).expanduser()
                if saved_path.is_absolute() and saved_path.resolve() == source_path.resolve():
                    return candidate
    return None


def _md_duration(path: Path) -> float:
    try:
        with path.open(encoding="utf-8") as source:
            for _ in range(30):
                line = source.readline()
                if not line:
                    break
                if line.startswith("duration:"):
                    return float(line.split(":", 1)[1].strip())
    except (OSError, ValueError):
        return 0.0
    return 0.0


def md_has_quality_warning(path: Path) -> bool:
    """Return whether Markdown begins with an unresolved ASR warning."""
    try:
        with path.open(encoding="utf-8") as source:
            for _ in range(40):
                line = source.readline()
                if not line:
                    break
                if line.startswith(QUALITY_HEADER_PREFIX):
                    return True
    except OSError:
        return False
    return False


def course_progress(out: Path, source: str, raw: str = "") -> dict[str, object]:
    course = _course_dir(out, source, raw)
    base = {
        "exists": course is not None,
        "course": str(course) if course else None,
        "done": 0,
        "total": 0,
        "done_seconds": 0.0,
        "complete": False,
        "manifest_fingerprint": "",
        "text_fingerprint": "",
        "quality_warnings": 0,
        "quality_warning_files": [],
    }
    if course is None:
        return base
    mpath = course / "manifest.jsonl"
    if not mpath.is_file():
        return base
    try:
        work = [item for item in manifest.load(mpath) if item.target]
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return base
    paths = [course / "text" / item.target for item in work]
    done_paths = [path for path in paths if path.is_file()]
    quality_paths = [path for path in done_paths if md_has_quality_warning(path)]
    fingerprint_source = "\n".join(
        json.dumps(
            {
                "rel": item.rel,
                "kind": item.kind,
                "size": item.size,
                "target": item.target,
                "index": item.index,
                "vid": item.vid,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for item in work
    )
    base.update(
        done=len(done_paths),
        total=len(work),
        done_seconds=sum(_md_duration(path) for path in done_paths),
        complete=bool(work) and len(done_paths) == len(work),
        manifest_fingerprint=hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest(),
        text_fingerprint=hashlib.sha256(
            "\n".join(str(path.relative_to(course / "text")) for path in done_paths).encode(
                "utf-8"
            )
        ).hexdigest(),
        quality_warnings=len(quality_paths),
        quality_warning_files=[
            str(path.relative_to(course / "text"))
            for path in quality_paths
        ][:5],
    )
    return base


def plan_queue(
    root: Path,
    queue_path: Path,
    cookies_from_browser: str = "",
    full_video: bool = False,
) -> list[dict[str, object]]:
    out = root / "out"
    rows = []
    for line in read_queue(queue_path):
        source = resolve_source(line, root, queue_path)
        expected = run_source(source)
        progress = course_progress(out, expected, line.raw)
        path = Path(source).expanduser()
        remote = bool(_REMOTE.match(source))
        kind = (
            "URL"
            if "://" in source
            else "remote"
            if remote
            else "zip"
            if archives.is_zip_source(path)
            else "directory"
            if path.is_dir()
            else "source"
        )
        if remote:
            estimate, size, error, estimate_known = {}, 0, "", False
        else:
            try:
                model = planning.build(source, cookies_from_browser, full_video)
                estimate = model["estimate"]
                size = sum(group["size_bytes"] for group in model["items"])
                error = ""
                estimate_known = True
            except Exception as exc:
                estimate, size, error, estimate_known = {}, 0, str(exc), False
        rows.append(
            {
                "line": line.lineno,
                "entry": line.raw,
                "source": source,
                "kind": kind,
                "out": "complete" if progress["complete"] else "present" if progress["exists"] else "missing",
                "size_bytes": size,
                "duration_seconds": estimate.get("media_duration_seconds"),
                "estimate_basis": estimate.get("basis", "none"),
                "estimate_known": estimate_known,
                "error": error,
            }
        )
    return rows


def lock_status(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"exists": False, "pid": None, "alive": False, "problem": ""}
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        if pid <= 0:
            raise ValueError
    except (OSError, ValueError):
        return {"exists": True, "pid": None, "alive": False, "problem": "invalid lock"}
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = True
    return {"exists": True, "pid": pid, "alive": alive, "problem": "" if alive else "stale lock"}


def _pid_alive(value: object) -> bool:
    try:
        pid = int(value)
        if pid <= 0:
            return False
    except (TypeError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class QueueLock:
    def __init__(self, path: Path):
        self.path = path
        self.owned = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                try:
                    before = self.path.stat()
                except FileNotFoundError:
                    continue
                status = lock_status(self.path)
                if status["alive"]:
                    raise QueueLocked(f"queue is already running: pid {status['pid']}, lock {self.path}")
                try:
                    after = self.path.stat()
                    if (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns):
                        self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"{os.getpid()}\n")
            self.owned = True
            return self
        raise QueueLocked(f"could not acquire lock: {self.path}")

    def __exit__(self, *_):
        if self.owned:
            try:
                if self.path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    self.path.unlink()
            except (FileNotFoundError, OSError):
                pass


class QueueLogger:
    """Persist each message before echoing it to stdout.

    A queue can outlive the scheduler worker whose pipe owns stdout. If that
    pipe closes, printing may raise ``BrokenPipeError``. Writing the file first
    preserves the record, and disabling echo after the first EPIPE lets the
    queue continue without a live consumer.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.echo = True

    def __call__(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if not self.echo:
            return
        try:
            print(line, flush=True)
        except (BrokenPipeError, OSError):
            self.echo = False
            try:
                # Prevent interpreter shutdown from flushing the dead pipe again.
                sys.stdout = open(os.devnull, "w", encoding="utf-8")
            except OSError:
                pass


def load_on_event(root: Path) -> str:
    """Load fail-closed queue configuration; a missing file means no hook."""
    path = root / "config.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise QueueError(f"could not read {path}: {exc}") from exc
    unknown = sorted(set(data) - {"on_event"})
    if unknown:
        raise QueueError(f"unknown fields in {path}: {', '.join(unknown)}")
    command = data.get("on_event", "")
    if not isinstance(command, str):
        raise QueueError(f"on_event in {path} must be a string")
    return command.strip()


def _one_line(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _entry_slug(raw: object) -> str:
    value = _one_line(raw)
    if not value:
        return ""
    if _REMOTE.match(value):
        if "://" not in value:
            return ""
        value = Path(urlsplit(value).path.rstrip("/")).name
    else:
        value = Path(value).name
    return value[:-4] if value.lower().endswith(".zip") else value


def _catalog_titles(root: Path) -> dict[str, str]:
    """Read slug-to-title mappings from ``<data>/catalog.tsv``.

    Do not rebuild the catalog here. A missing catalog or an empty title simply
    leaves that slug out of the returned mapping.
    """
    try:
        rows = catalog.read(root / "catalog.tsv")
    except (OSError, UnicodeError):
        return {}
    titles = {}
    for slug, row in rows.items():
        title = _one_line(row.get("title"))
        if title and title != catalog.MISSING:
            titles[slug] = title
    return titles


def _short_dur(seconds: object) -> str:
    """Format a status duration as 12m, 0h51, or 1h22."""
    hours, minutes = divmod(int(_seconds(seconds)) // 60, 60)
    return f"{hours}h{minutes:02d}" if hours else f"{minutes}m"


def _media_hours(seconds: object) -> str:
    return f"~{_seconds(seconds) / 3600:.1f}h"


def _hook_action(lines: tuple[str, ...]) -> str:
    for line in lines:
        lowered = line.lower()
        for prefix in HOOK_ACTION_PREFIXES:
            if lowered.startswith(prefix):
                return line[len(prefix):].strip()
    return ""


def _event(
    command: str,
    logger: QueueLogger,
    root: Path,
    name: str,
    *,
    kind: str,
    subject: str,
    title: str,
    lines: tuple[str, ...],
    entry: str = "",
) -> None:
    """Run an isolated observability hook that cannot change queue success.

    The legacy environment contract remains EVENT/KIND/SUBJECT/TITLE/LINES/
    TEXT/ENTRY/DATA. The v2 envelope adds FROM/ABOUT/NAME/CLASS/ACTION. ``about``
    is the course slug, or ``queue`` for queue-wide events; ``name`` is its
    catalog title; ``class`` maps through HOOK_CLASSES; and ``action`` comes
    from a ``What to do:`` line. Event titles are lower case and states are
    English. Tests cover every event emitted during a run.
    """
    if not command:
        return
    clean_subject = _one_line(subject)[:HOOK_SUBJECT_LIMIT]
    clean_title = _one_line(title)[:HOOK_LINE_LIMIT]
    clean_lines = tuple(
        value
        for value in (_one_line(line)[:HOOK_LINE_LIMIT] for line in lines)
        if value
    )[:HOOK_LINES_LIMIT]
    fallback = "; ".join((f"{clean_subject}: {clean_title}", *clean_lines))
    env = os.environ.copy()
    env.update(
        COURSEDUMP_EVENT=name,
        COURSEDUMP_KIND=kind,
        COURSEDUMP_SUBJECT=clean_subject,
        COURSEDUMP_TITLE=clean_title,
        COURSEDUMP_LINES="\n".join(clean_lines),
        COURSEDUMP_TEXT=_one_line(fallback)[:HOOK_TEXT_LIMIT],
        COURSEDUMP_ENTRY=entry,
        COURSEDUMP_DATA=str(root),
        COURSEDUMP_FROM=SNAPSHOT_FLOW,
        COURSEDUMP_ABOUT=clean_subject,
        COURSEDUMP_NAME=_catalog_titles(root).get(clean_subject, ""),
        COURSEDUMP_CLASS=HOOK_CLASSES.get(kind, "event"),
        COURSEDUMP_ACTION=_hook_action(clean_lines),
    )
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            env=env,
            timeout=HOOK_TIMEOUT,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        logger(f"hook: {name} fail timeout")
    except Exception as exc:
        logger(f"hook: {name} fail {_one_line(exc) or type(exc).__name__}")
    else:
        outcome = "ok" if result.returncode == 0 else "fail"
        logger(f"hook: {name} {outcome} {result.returncode}")


def new_log_path(root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return root / "logs" / f"queue-{stamp}.log"


def _invoke_run(source: str, root: Path, log_path: Path, options: dict[str, object]) -> int:
    cmd = [sys.executable, "-m", "coursedump", "run", source, "--data", str(root), "--json"]
    for name in ("asr", "model", "language", "cookies-from-browser"):
        value = options.get(name.replace("-", "_"), "")
        if value:
            cmd += [f"--{name}", str(value)]
    for name in ("purge-video", "full-video", "no-throttle"):
        if options.get(name.replace("-", "_")):
            cmd.append(f"--{name}")
    with log_path.open("a", encoding="utf-8") as log:
        result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
    return result.returncode


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_state(path: Path, state: dict[str, object]) -> None:
    state["updated_at"] = _now()
    atomic_write_json(path, state)


def _estimate(source: str, options: dict[str, object]) -> dict[str, object]:
    model = planning.build(
        source,
        str(options.get("cookies_from_browser") or ""),
        bool(options.get("full_video")),
    )
    estimate = dict(model["estimate"])
    estimate["work_items"] = sum(
        int(group.get("work_items") or 0) for group in model["items"]
    )
    return estimate


def _seconds(value: object) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _remaining_media(estimate: dict[str, object], progress: dict[str, object]) -> float:
    return max(
        0.0,
        _seconds(estimate.get("media_duration_seconds"))
        - _seconds(progress.get("done_seconds")),
    )


def _update_queue_eta(state: dict[str, object]) -> None:
    remaining = _seconds(state.get("remaining_media_seconds"))
    speed = _seconds(state.get("speed_ratio")) or DEFAULT_SPEED_RATIO
    estimate_unknown = int(state.get("remaining_unknown_estimates") or 0)
    state["remaining_media_seconds"] = remaining
    state["speed_ratio"] = speed
    state["eta_known"] = estimate_unknown == 0
    state["queue_eta_seconds"] = remaining / speed if state["eta_known"] else None


def _last_nonempty_log_line(path: Path, start: int) -> str:
    """Return the child run's last line, excluding earlier queue log content."""
    try:
        with path.open("rb") as source:
            source.seek(start)
            chunk = source.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = [line for line in chunk.splitlines() if line.strip()]
    return _one_line(lines[-1])[:FAILURE_REASON_LIMIT] if lines else ""


def _log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _record_speed(state: dict[str, object], media_seconds: float, elapsed: float) -> None:
    if media_seconds <= 0 or elapsed <= 0:
        return
    samples = list(state.get("speed_samples") or [])
    samples.append({"media_seconds": media_seconds, "elapsed": elapsed})
    samples = samples[-SPEED_WINDOW:]
    state["speed_samples"] = samples
    state["speed_ratio"] = sum(
        _seconds(sample.get("media_seconds")) for sample in samples
    ) / sum(_seconds(sample.get("elapsed")) for sample in samples)
    _update_queue_eta(state)


def _read_state(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _failure_snapshot(progress: dict[str, object]) -> dict[str, object]:
    """Capture enough filesystem state to compare adjacent queue ticks."""
    return {
        "exists": bool(progress.get("exists")),
        "manifest": str(progress.get("manifest_fingerprint") or ""),
        "text": str(progress.get("text_fingerprint") or ""),
        "done": int(progress.get("done") or 0),
        "total": int(progress.get("total") or 0),
    }


def _is_repeat_failure(
    previous: dict[str, object],
    *,
    entry: str,
    source: str,
    error: str,
    snapshot: dict[str, object],
) -> bool:
    """Return whether the previous tick saw the same course, error, and files."""
    previous_results = previous.get("results")
    if not isinstance(previous_results, list):
        return False
    clean_entry = _one_line(entry)
    clean_source = _one_line(source)
    for result in reversed(previous_results):
        if not isinstance(result, dict):
            continue
        result_source = _one_line(result.get("source"))
        same_course = (
            result_source == clean_source
            if result_source
            else _one_line(result.get("entry")) == clean_entry
        )
        if not same_course:
            continue
        return (
            result.get("status") == "FAIL"
            and _one_line(result.get("error")) == _one_line(error)
            and result.get("snapshot") == snapshot
        )
    return False


def run_queue(
    root: Path,
    queue_path: Path,
    *,
    log_path: Path,
    transient_wait: int = TRANSIENT_WAIT,
    transient_retries: int = TRANSIENT_RETRIES,
    purge_zip: bool = False,
    **options,
) -> QueueCounts:
    lines = read_queue(queue_path)
    if not lines:
        raise QueueError(f"queue is empty: {queue_path}")
    on_event = load_on_event(root)
    lock_path = root / "queue.lock"
    state_path = root / "logs" / "queue-state.json"
    logger = QueueLogger(log_path)
    counts = QueueCounts(DONE=0, FAIL=0, SKIP=0)
    with QueueLock(lock_path):
        previous = _read_state(state_path)
        speed = (
            DEFAULT_SPEED_RATIO
            if str(options.get("asr") or "mlx") == "mlx"
            else planning.ASR_SPEED
        )
        state: dict[str, object] = {
            "schema_version": 1,
            "status": "running",
            "pid": os.getpid(),
            "queue": str(queue_path),
            "log": str(log_path),
            "started_at": _now(),
            "current": {"stage": "preflight"},
            "counts": counts,
            "done": 0,
            "remaining": len(lines),
            "remaining_media_seconds": 0.0,
            "remaining_unknown_estimates": len(lines),
            "speed_ratio": speed,
            "queue_eta_seconds": None,
            "eta_known": False,
            "speed_samples": [],
            "last_error": "",
            "new_failures": 0,
            "quality_warnings": 0,
            "results": [],
            "results_total": 0,
        }
        queue_started_at = time.monotonic()
        _write_state(state_path, state)
        logger(f"queue run: {len(lines)} items, log={log_path}")
        notifications_started = False
        queue_started_sent_at: float | None = None
        queue_started_timer: threading.Timer | None = None
        ready = 0

        resume = ""
        resumed_after_interruption = previous.get("status") in {"running", "failed"}
        if resumed_after_interruption:
            old_current = previous.get("current") or {}
            old_entry = _entry_slug(old_current.get("entry")) or "unknown"
            resume = f"resumed after interruption on {old_entry}"
            logger(f"RESUME continuing after interruption at {old_entry}")

        def announce_started(work: int, *, estimate_ready: bool, refresh: bool = False) -> None:
            nonlocal notifications_started, queue_started_sent_at
            if notifications_started and not refresh:
                return
            if estimate_ready:
                media = _media_hours(state["remaining_media_seconds"])
                unknown = int(state.get("remaining_unknown_estimates") or 0)
                if unknown:
                    media += f" (+{unknown} unestimated)"
                eta = (
                    f"~{_short_dur(state['queue_eta_seconds'])}"
                    if state["eta_known"]
                    else "?"
                )
            else:
                media = "? (estimating)"
                eta = "?"
            _event(
                on_event,
                logger,
                root,
                "queue_started",
                kind="progress",
                subject="queue",
                title="queue start",
                lines=(
                    f"items {work}/{len(lines)} to do",
                    f"media {media} · eta {eta}",
                    resume,
                ),
            )
            notifications_started = True
            if not estimate_ready:
                queue_started_sent_at = time.monotonic()

        def finish_start_estimate(work: int) -> None:
            """Emit one ETA event for a quick estimate, two for a slow one."""
            nonlocal queue_started_timer
            if queue_started_timer is not None:
                queue_started_timer.cancel()
                queue_started_timer.join()
                queue_started_timer = None
            if queue_started_sent_at is None:
                announce_started(work, estimate_ready=True)
                return
            gap = time.monotonic() - queue_started_sent_at
            if gap < QUEUE_STARTED_DEBOUNCE:
                _sleep(QUEUE_STARTED_DEBOUNCE - gap)
            announce_started(work, estimate_ready=True, refresh=True)

        def announce_course_fail(slug: str, label: str, error: str) -> None:
            _event(
                on_event,
                logger,
                root,
                "course_done",
                kind="fail",
                subject=slug or label,
                title="fail",
                lines=(
                    error,
                    f"log: {log_path}",
                    f"queue: {state['remaining']} left",
                ),
                entry=slug,
            )

        try:
            entries = []
            for line in lines:
                state["current"] = {
                    "entry": line.raw,
                    "stage": "preflight",
                }
                _write_state(state_path, state)
                source = resolve_source(line, root, queue_path)
                expected = run_source(source)
                before = course_progress(root / "out", expected, line.raw)
                entries.append(
                    {
                        "line": line,
                        "source": source,
                        "expected": expected,
                        "before": before,
                        "needs_work": not before["complete"],
                        "start_announced": False,
                    }
                )
            ready = sum(1 for entry in entries if entry["before"]["complete"])
            predicted_work = len(lines) - ready
            if predicted_work:
                # A fast preflight should not emit adjacent progress events. If
                # estimation takes longer, the timer still reports it underway.
                queue_started_timer = threading.Timer(
                    QUEUE_STARTED_DEBOUNCE,
                    announce_started,
                    args=(predicted_work,),
                    kwargs={"estimate_ready": False},
                )
                queue_started_timer.daemon = True
                queue_started_timer.start()

            for position, entry in enumerate(entries, 1):
                line = entry["line"]
                state["current"] = {
                    "estimate_index": position,
                    "entry": line.raw,
                    "source": entry["expected"],
                    "stage": "estimate",
                }
                _write_state(state_path, state)
                if entry["before"]["complete"] and local_source_gone(
                    str(entry["source"])
                ):
                    # There is no work and no source from which to estimate it.
                    entry["estimate"] = {}
                    entry["estimate_known"] = False
                    entry["remaining_media_seconds"] = 0.0
                    continue
                try:
                    estimate = _estimate(str(entry["source"]), options)
                except Exception as exc:
                    estimate = {}
                    logger(
                        f"ESTIMATE {line.raw}: unknown - "
                        f"{_one_line(exc) or type(exc).__name__}"
                    )
                estimate_known = bool(estimate)
                entry["estimate"] = estimate
                entry["estimate_known"] = estimate_known
                if estimate_known and int(estimate.get("work_items") or 0) > int(
                    entry["before"]["total"]
                ):
                    entry["needs_work"] = True
                entry["remaining_media_seconds"] = _remaining_media(
                    estimate, entry["before"]
                )

            state["remaining_media_seconds"] = sum(
                float(entry["remaining_media_seconds"]) for entry in entries
            )
            state["remaining_unknown_estimates"] = sum(
                1 for entry in entries if not entry["estimate_known"]
            )
            _update_queue_eta(state)
            state["current"] = {"stage": "preflight"}
            _write_state(state_path, state)
            predicted_work = sum(bool(entry["needs_work"]) for entry in entries)
            if predicted_work:
                finish_start_estimate(predicted_work)
            elif queue_started_timer is not None:
                queue_started_timer.cancel()
                queue_started_timer.join()
                queue_started_timer = None

            for position, entry in enumerate(entries, 1):
                line = entry["line"]
                source = str(entry["source"])
                expected = str(entry["expected"])
                before = entry["before"]
                estimate = entry["estimate"]
                estimate_known = bool(entry["estimate_known"])
                planned_media = float(entry["remaining_media_seconds"])
                slug = _entry_slug(line.raw)
                label = slug or _one_line(line.raw)
                lessons = int(estimate.get("work_items") or before["total"] or 0)
                course_started_at = time.monotonic()
                current = {
                    "index": position,
                    "entry": line.raw,
                    "source": expected,
                    "stage": "unpack" if archives.is_zip_source(Path(source)) else "run",
                    "started_at": _now(),
                    "attempt": 0,
                    "baseline_done": before["done"],
                    "baseline_done_seconds": before["done_seconds"],
                    "total_seconds": estimate.get("media_duration_seconds"),
                    "estimate_known": estimate_known,
                    "accounted": False,
                }
                state["current"] = current
                _write_state(state_path, state)

                def announce_course_started() -> None:
                    if entry["start_announced"]:
                        return
                    if estimate_known:
                        estimate_text = _media_hours(planned_media)
                        course_eta = planned_media / _seconds(state["speed_ratio"])
                        course_eta_text = f"~{_short_dur(course_eta)}"
                    else:
                        estimate_text = course_eta_text = "?"
                    _event(
                        on_event,
                        logger,
                        root,
                        "course_started",
                        kind="progress",
                        subject=slug or label,
                        title="asr start",
                        lines=(
                            f"lessons {lessons} · media {estimate_text} · "
                            f"eta {course_eta_text}",
                            f"queue after: {len(lines) - position}",
                        ),
                        entry=slug,
                    )
                    entry["start_announced"] = True

                if entry["needs_work"]:
                    announce_course_started()

                archive = Path(source) if archives.is_zip_source(Path(source)) else None
                try:
                    if archive is not None:
                        expected = str(archives.extract(archive))
                        current["source"] = expected
                        try:
                            exact = _estimate(expected, options)
                        except Exception as exc:
                            exact = {}
                            logger(
                                f"ESTIMATE {line.raw} after extraction: unknown - "
                                f"{_one_line(exc) or type(exc).__name__}"
                            )
                        exact_known = bool(exact)
                        if exact_known != estimate_known:
                            state["remaining_unknown_estimates"] = max(
                                0,
                                int(state["remaining_unknown_estimates"])
                                + (-1 if exact_known else 1),
                            )
                        estimate_known = exact_known
                        estimate = exact
                        entry["estimate"] = exact
                        entry["estimate_known"] = exact_known
                        current["estimate_known"] = exact_known
                        exact_media = _remaining_media(exact, before)
                        state["remaining_media_seconds"] = max(
                            0.0,
                            _seconds(state["remaining_media_seconds"])
                            + exact_media
                            - planned_media,
                        )
                        planned_media = exact_media
                        entry["remaining_media_seconds"] = exact_media
                        current["total_seconds"] = exact.get("media_duration_seconds")
                        _update_queue_eta(state)
                        if exact_known and int(exact.get("work_items") or 0) > int(
                            before["total"]
                        ):
                            entry["needs_work"] = True
                            lessons = int(exact.get("work_items") or lessons)
                            if not notifications_started:
                                announce_started(
                                    counts["DONE"] + counts["FAIL"] + 1,
                                    estimate_ready=True,
                                )
                            announce_course_started()
                    current["stage"] = "run"
                    _write_state(state_path, state)
                except Exception as exc:
                    elapsed = max(0.0, time.monotonic() - course_started_at)
                    error = f"archive extraction: {_one_line(exc) or type(exc).__name__}"
                    snapshot = _failure_snapshot(
                        course_progress(root / "out", expected, line.raw)
                    )
                    repeat = _is_repeat_failure(
                        previous,
                        entry=line.raw,
                        source=expected,
                        error=error,
                        snapshot=snapshot,
                    )
                    counts["FAIL"] += 1
                    if not repeat:
                        counts.new_failures += 1
                        state["new_failures"] = counts.new_failures
                    state["last_error"] = _one_line(f"{line.raw}: {error}")[
                        :FAILURE_REASON_LIMIT
                    ]
                    result = {
                        "entry": line.raw,
                        "source": expected,
                        "status": "FAIL",
                        "error": error,
                        "snapshot": snapshot,
                        "elapsed": elapsed,
                    }
                    if repeat:
                        result["repeat"] = True
                    _record_result(
                        state,
                        result,
                    )
                    state["done"] = sum(counts.values())
                    state["remaining"] = len(lines) - position
                    state["remaining_media_seconds"] = max(
                        0.0, _seconds(state["remaining_media_seconds"]) - planned_media
                    )
                    state["remaining_unknown_estimates"] = max(
                        0,
                        int(state["remaining_unknown_estimates"])
                        - int(not estimate_known),
                    )
                    current["accounted"] = True
                    _update_queue_eta(state)
                    _write_state(state_path, state)
                    logger(
                        f"FAIL {line.raw}: {error} (repeat)"
                        if repeat
                        else f"FAIL {line.raw}: {error}; continuing"
                    )
                    if not notifications_started:
                        announce_started(
                            counts["DONE"] + counts["FAIL"], estimate_ready=True
                        )
                    if not repeat:
                        announce_course_fail(
                            slug,
                            label,
                            error,
                        )
                    continue

                rc = 1
                transient_attempt = 0
                child_error = ""
                gone = before["complete"] and local_source_gone(source)
                if gone:
                    rc = 0
                    logger(f"GONE {line.raw}: source removed, snapshot complete - SKIP")
                while not gone:
                    current["attempt"] += 1
                    _write_state(state_path, state)
                    logger(f"RUN  {line.raw}: attempt {current['attempt']}")
                    child_log_start = _log_size(log_path)
                    rc = _invoke_run(expected, root, log_path, options)
                    child_error = _last_nonempty_log_line(log_path, child_log_start)
                    if rc == 111:
                        transient_attempt += 1
                        if transient_attempt >= transient_retries:
                            break
                        current["stage"] = "transient-wait"
                        _write_state(state_path, state)
                        logger(f"WAIT {line.raw}: exit 111, retry in {transient_wait} s")
                        _sleep(transient_wait)
                        current["stage"] = "run"
                        continue
                    break

                current["stage"] = "verify"
                _write_state(state_path, state)
                after = course_progress(root / "out", expected, line.raw)
                error = ""
                if rc == 0 and after["complete"]:
                    status = (
                        "SKIP"
                        if before["complete"]
                        and before["manifest_fingerprint"] == after["manifest_fingerprint"]
                        else "DONE"
                    )
                    if archive is not None and purge_zip:
                        try:
                            archives.purge(archive)
                        except OSError as exc:
                            status = "FAIL"
                            error = f"snapshot complete, but --purge-zip failed: {exc}"
                        else:
                            logger(f"PURGE {archive}: archive removed by --purge-zip")
                else:
                    status = "FAIL"
                    error = (
                        "insufficient space (exit 75)"
                        if rc == 75 and not child_error
                        else child_error
                        if child_error
                        else f"exit {rc}"
                        if rc
                        else "process finished, but the snapshot is incomplete"
                    )

                if status != "SKIP" and not notifications_started:
                    # Work discovered late, such as a grown manifest or failed
                    # child run, receives a start event before its final event.
                    announce_started(
                        counts["DONE"] + counts["FAIL"] + 1, estimate_ready=True
                    )

                elapsed = max(0.0, time.monotonic() - course_started_at)
                counts[status] += 1
                result = {
                    "entry": line.raw,
                    "status": status,
                    "elapsed": elapsed,
                }
                # A tick report describes work done by this run. SKIP means the
                # course was already complete and its manifest did not change,
                # so there was no work and no new quality finding. Repeating old
                # warnings every hour would train readers to ignore them. The
                # warnings remain in Markdown, INDEX.md, and per-course status.
                quality_warnings = (
                    int(after.get("quality_warnings") or 0) if status != "SKIP" else 0
                )
                if quality_warnings:
                    result["quality_warnings"] = quality_warnings
                    result["quality_warning_files"] = after.get(
                        "quality_warning_files", []
                    )
                    state["quality_warnings"] = (
                        int(state.get("quality_warnings") or 0) + quality_warnings
                    )
                if error:
                    clean_error = _one_line(error)
                    snapshot = _failure_snapshot(after)
                    repeat = _is_repeat_failure(
                        previous,
                        entry=line.raw,
                        source=expected,
                        error=clean_error,
                        snapshot=snapshot,
                    )
                    result["source"] = expected
                    result["error"] = clean_error
                    result["snapshot"] = snapshot
                    if repeat:
                        result["repeat"] = True
                    else:
                        counts.new_failures += 1
                        state["new_failures"] = counts.new_failures
                    state["last_error"] = _one_line(f"{line.raw}: {error}")[
                        :FAILURE_REASON_LIMIT
                    ]
                    logger(
                        f"FAIL {line.raw}: {error} (repeat)"
                        if repeat
                        else f"FAIL {line.raw}: {error}; continuing"
                    )
                else:
                    quality_log = (
                        f"; QUALITY: ASR loop in {quality_warnings} files"
                        if quality_warnings
                        else ""
                    )
                    logger(
                        f"{status} {line.raw}: {after['done']}/{after['total']}"
                        f"{quality_log}"
                    )
                _record_result(state, result)
                state["remaining"] = len(lines) - position
                state["done"] = sum(counts.values())
                state["remaining_media_seconds"] = max(
                    0.0, _seconds(state["remaining_media_seconds"]) - planned_media
                )
                state["remaining_unknown_estimates"] = max(
                    0,
                    int(state["remaining_unknown_estimates"])
                    - int(not estimate_known),
                )
                current["accounted"] = True
                if status == "DONE":
                    _record_speed(state, planned_media, elapsed)
                _update_queue_eta(state)
                _write_state(state_path, state)

                if status == "DONE":
                    # In v2 each event addresses a course slug. Completion and
                    # the next start therefore use separate envelopes with
                    # different subjects instead of one mutable queue message.
                    added = max(0, int(after["total"]) - int(before["total"]))
                    queue_eta = (
                        f"~{_short_dur(state['queue_eta_seconds'])}"
                        if state["eta_known"]
                        else "?"
                    )
                    remaining_media = _media_hours(state["remaining_media_seconds"])
                    unknown = int(state.get("remaining_unknown_estimates") or 0)
                    if unknown:
                        remaining_media += f" (+{unknown} unestimated)"
                    _event(
                        on_event,
                        logger,
                        root,
                        "course_done",
                        kind="progress",
                        subject=slug or label,
                        title="asr done",
                        lines=(
                            f"lessons {after['done']}/{after['total']} · "
                            f"{_short_dur(elapsed)}"
                            + (
                                f" · quality: asr loop in {quality_warnings} files"
                                if quality_warnings
                                else ""
                            ),
                            f"queue: {state['remaining']} left · "
                            f"media {remaining_media} · eta {queue_eta}",
                            f"updated: +{added} lessons" if before["complete"] else "",
                        ),
                        entry=slug,
                    )
                elif status == "FAIL":
                    if not result.get("repeat"):
                        announce_course_fail(slug, label, _one_line(error))

            state["status"] = "finished"
            state["current"] = None
            state["remaining_media_seconds"] = 0.0
            state["remaining_unknown_estimates"] = 0
            _update_queue_eta(state)
            _write_state(state_path, state)
            logger("result: " + " ".join(f"{key}={value}" for key, value in counts.items()))
            queue_elapsed = max(0.0, time.monotonic() - queue_started_at)
            if notifications_started or resumed_after_interruption:
                finished_lines = (
                    f"done {counts['DONE']} · fail {counts['FAIL']} · "
                    f"skip {counts['SKIP']}"
                    + (
                        f" · quality {state['quality_warnings']}"
                        if state.get("quality_warnings")
                        else ""
                    )
                    + f" · {_short_dur(queue_elapsed)}",
                    f"out: {root / 'out'}",
                )
                if resumed_after_interruption and not notifications_started:
                    finished_lines = (
                        "resumed after interruption: nothing left to do",
                        *finished_lines,
                    )
                _event(
                    on_event,
                    logger,
                    root,
                    "queue_finished",
                    kind="done",
                    subject="queue",
                    title="queue done",
                    lines=finished_lines,
                )
        except Exception as exc:
            if queue_started_timer is not None:
                queue_started_timer.cancel()
                queue_started_timer.join()
                queue_started_timer = None
            current = state.get("current") or {}
            slug = _entry_slug(current.get("entry"))
            label = slug or _one_line(current.get("entry")) or "unknown"
            error = _one_line(exc) or type(exc).__name__
            state["status"] = "failed"
            state["last_error"] = f"{label}: {error}"
            _write_state(state_path, state)
            logger(f"ERROR queue failed at {label}: {error}")
            if not notifications_started:
                announce_started(
                    max(1, counts["DONE"] + counts["FAIL"]), estimate_ready=False
                )
            _event(
                on_event,
                logger,
                root,
                "queue_failed",
                kind="fail",
                subject="queue",
                title=f"queue failed on {label}",
                lines=(
                    error,
                    "What to do: coursedump queue run",
                ),
                entry=slug,
            )
            raise
        finally:
            if queue_started_timer is not None:
                queue_started_timer.cancel()
                queue_started_timer.join()
    return counts


def _parse_time(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _record_result(state: dict[str, object], result: dict[str, object]) -> None:
    results = list(state.get("results") or [])
    results.append(result)
    state["results"] = results[-STATUS_RESULTS_LIMIT:]
    state["results_total"] = int(state.get("results_total") or 0) + 1


def snapshot_payload(
    root: Path,
    queue_path: Path,
    lines: list[QueueLine],
    state: dict[str, object],
    lock: dict[str, object],
    current: dict[str, object],
    progress: dict[str, object] | None,
) -> dict[str, object]:
    """Build the coursedump fragment for a gateway status snapshot.

    {"flow": "coursedump",
     "workers": [{"item": slug, "name": catalog title | null,
                  "pid": state.pid, "job": null,
                  "stage": {"i": 1, "n": 1, "name": "asr"},
                  "done": completed lessons, "total": all lessons,
                  "started_at": current.started_at,
                  "eta_seconds": stage ETA | null,
                  "eta_total_seconds": same value because there is one stage,
                  "state": "running", "note": null,
                  "metrics": ["media 1.2h", "speed 12.0x"]}],
     "queue": [{"item": slug, "name": title | null, "state": "waiting"}, ...]}

    A worker exists only while the lock is live and a current course has entered
    the main loop, indicated by ``started_at``. Preflight and estimation do not
    create a worker. ``queue`` contains later queue.txt entries in order, except
    already complete snapshots. Both lists are empty when the queue is idle.
    """
    if not lock.get("alive"):
        return {"flow": SNAPSHOT_FLOW, "workers": [], "queue": []}
    titles = _catalog_titles(root)
    workers = []
    if current.get("entry") and current.get("started_at"):
        slug = _entry_slug(current.get("entry")) or _one_line(current.get("entry"))
        snap = progress or {}
        metrics = []
        media_seconds = _seconds(current.get("total_seconds"))
        if media_seconds > 0:
            metrics.append(f"media {media_seconds / 3600:.1f}h")
        speed = _seconds(state.get("speed_ratio"))
        if speed > 0:
            metrics.append(f"speed {speed:.1f}x")
        eta = snap.get("eta_seconds")
        workers.append(
            {
                "item": slug,
                "name": titles.get(slug),
                "pid": state.get("pid") or lock.get("pid"),
                "job": None,
                "stage": dict(SNAPSHOT_STAGE),
                "done": int(snap.get("done") or 0),
                "total": int(snap.get("total") or 0),
                "started_at": current.get("started_at"),
                "eta_seconds": eta,
                "eta_total_seconds": eta,
                "state": "running",
                "note": None,
                "metrics": metrics,
            }
        )
    waiting = []
    index = int(current.get("index") or 0)
    for line in lines[index:]:
        source = resolve_source(line, root, queue_path)
        if course_progress(root / "out", run_source(source), line.raw)["complete"]:
            continue
        slug = _entry_slug(line.raw) or _one_line(line.raw)
        waiting.append({"item": slug, "name": titles.get(slug), "state": "waiting"})
    return {"flow": SNAPSHOT_FLOW, "workers": workers, "queue": waiting}


def status_payload(root: Path, queue_path: Path) -> dict[str, object]:
    state_path = root / "logs" / "queue-state.json"
    state = _read_state(state_path)
    results = state.get("results")
    if isinstance(results, list):
        total = max(int(state.get("results_total") or 0), len(results))
        state["results"] = results[-STATUS_RESULTS_LIMIT:]
        state["results_total"] = total
        state["results_truncated"] = total > len(state["results"])
    lock = lock_status(root / "queue.lock")
    try:
        lines = read_queue(queue_path)
    except QueueError:
        lines = []
    current = dict(state.get("current") or {})
    progress = None
    eta_seconds = None
    processed_media = 0.0
    if current.get("source"):
        snap = course_progress(root / "out", str(current["source"]), str(current.get("entry", "")))
        total_seconds = float(current.get("total_seconds") or 0)
        if total_seconds > 0:
            done_value = float(snap["done_seconds"])
            total_value = total_seconds
            baseline = float(current.get("baseline_done_seconds") or 0)
        else:
            done_value = float(snap["done"])
            total_value = float(snap["total"])
            baseline = float(current.get("baseline_done") or 0)
        percent = min(100.0, 100.0 * done_value / total_value) if total_value else 0.0
        started = _parse_time(current.get("started_at"))
        delta = done_value - baseline
        if started and delta > 0 and total_value > done_value:
            rate = delta / max(time.time() - started, 1)
            eta_seconds = (total_value - done_value) / rate if rate else None
        progress = {**snap, "percent": percent, "eta_seconds": eta_seconds}
        if not current.get("accounted"):
            processed_media = max(
                0.0,
                _seconds(snap.get("done_seconds"))
                - _seconds(current.get("baseline_done_seconds")),
            )
    speed_ratio = (
        _seconds(state.get("speed_ratio")) or DEFAULT_SPEED_RATIO
        if state
        else None
    )
    remaining_media_seconds = None
    queue_eta_seconds = None
    eta_known = None
    if "remaining_media_seconds" in state and speed_ratio:
        eta_known = bool(state.get("eta_known", True))
        remaining_media_seconds = max(
            0.0,
            _seconds(state.get("remaining_media_seconds")) - processed_media,
        )
        if eta_known:
            queue_eta_seconds = remaining_media_seconds / speed_ratio
    process_alive = None
    if state.get("status") == "running":
        process_alive = (
            _pid_alive(state.get("pid"))
            if state.get("pid") is not None
            else bool(lock["alive"])
        )
    index = int(current.get("index") or 0)
    if state.get("status") == "finished":
        next_entries = []
    else:
        next_entries = ([line.raw for line in lines[index:index + 5]]
                        if index else [line.raw for line in lines[:5]])
    return {
        "schema_version": 1,
        "queue": str(queue_path),
        "queue_exists": queue_path.is_file(),
        "queue_length": len(lines),
        "lock": lock,
        "state": state,
        "progress": progress,
        "remaining_media_seconds": remaining_media_seconds,
        "queue_eta_seconds": queue_eta_seconds,
        "speed_ratio": speed_ratio,
        "eta_known": eta_known,
        "process_alive": process_alive,
        "next": next_entries,
        "snapshot": snapshot_payload(
            root, queue_path, lines, state, lock, current, progress
        ),
    }


def spawn_detached(
    root: Path,
    queue_path: Path,
    log_path: Path,
    repo_root: Path,
    argv: list[str],
) -> tuple[int, int | None]:
    nohup = shutil.which("nohup")
    if nohup is None:
        raise QueueError("nohup not found; detached execution is unavailable")
    cmd = [nohup, sys.executable, "-m", "coursedump", "queue", "run", *argv,
           "--data", str(root), "--queue", str(queue_path), "--log", str(log_path)]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("a", encoding="utf-8") as log:
            proc = subprocess.Popen(
                cmd,
                cwd=repo_root,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # shell-disown semantics without a live shell
                close_fds=True,
            )
            try:
                returncode = proc.wait(timeout=DETACH_START_TIMEOUT)
            except subprocess.TimeoutExpired:
                return proc.pid, None
    except OSError as exc:
        raise QueueError(f"could not start detached queue: {exc}") from exc
    if returncode:
        raise QueueError(
            f"detached process exited during startup: exit {returncode}; log {log_path}"
        )
    return proc.pid, returncode
