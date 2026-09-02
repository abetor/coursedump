"""coursedump: course source -> text snapshot. run / plan / status / doctor.

Data lives outside the repository. The default store is
~/tools-data/coursedump-data/{in,out}, created by the first run, so removing the
tool does not remove course data. Override it with --data DIR or the
COURSEDUMP_DATA environment variable; override output alone with --out DIR.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__ as TOOL_VERSION
from . import archives, boosty_text, catalog, corpus, executor, manifest, normalize, planning, queueing, sources
from .space import NoSpace
from .util import human_size, human_dur, slugify

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Course source (folder/cloud/video platform/PDF) -> Markdown snapshot")
queue_app = typer.Typer(help="Sequential course queue from <data>/queue.txt")
app.add_typer(queue_app, name="queue")
console = Console()

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_DATA = "COURSEDUMP_DATA"

ASR_SPEED = planning.ASR_SPEED
MACHINE_SCHEMA_VERSION = 1
MAX_MACHINE_OUTPUT_BYTES = 64 * 1024

_MACHINE_EXITS = {"0": "success", "1": "failure"}
_REMOTE_MACHINE_EXITS = {
    **_MACHINE_EXITS,
    "75": "resource",
    "111": "transient",
}

_DATA_OPT = typer.Option(None, "--data",
                         help=f"Data directory (default: ~/tools-data/coursedump-data; env {ENV_DATA})")


def data_root(data: Path | None = None) -> Path:
    """Resolve data from --data, COURSEDUMP_DATA, or the default data store.

    Keeping the default outside the repository prevents a reinstall or checkout
    cleanup from silently deleting course data.
    """
    if data is not None:
        return data.expanduser().resolve()
    env = os.environ.get(ENV_DATA, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / "tools-data" / "coursedump-data"


def ensure_data_dirs(root: Path) -> None:
    """Create in/out and seed a working filters.toml on first use."""
    (root / "in").mkdir(parents=True, exist_ok=True)
    (root / "out").mkdir(parents=True, exist_ok=True)
    filters = root / "filters.toml"
    if not filters.exists():
        shutil.copyfile(normalize.DEFAULT_FILTERS, filters)


def _use_data(data: Path | None) -> Path:
    """Resolve the data directory and activate its filters without writing."""
    root = data_root(data)
    normalize.configure(root / "filters.toml")
    return root


def _caffeinate() -> None:
    """Keep an unattended macOS run awake."""
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass


def _write_machine(
    payload: dict[str, object],
    *,
    fallback: dict[str, object] | None = None,
) -> bool:
    """Write one canonical JSON object to stdout without Rich markup."""
    rendered = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    within_limit = len(rendered.encode("utf-8")) <= MAX_MACHINE_OUTPUT_BYTES
    if not within_limit:
        if fallback is None:
            raise ValueError("machine output exceeds its byte limit")
        rendered = json.dumps(
            fallback,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    sys.stdout.write(rendered + "\n")
    return within_limit


def _capability(
    name: str,
    argv: list[str],
    required_fields: list[str],
    exit_codes: dict[str, str],
    idempotency: str,
) -> dict[str, object]:
    return {
        "name": name,
        "argv": argv,
        "machine_output": {
            "format": "json",
            "schema_version": MACHINE_SCHEMA_VERSION,
            "required_fields": required_fields,
        },
        "exit_codes": exit_codes,
        "idempotency": idempotency,
    }


def machine_capabilities() -> dict[str, object]:
    """Describe public machine commands for runtime contract probes."""
    return {
        "schema_version": MACHINE_SCHEMA_VERSION,
        "tool": "coursedump",
        "version": TOOL_VERSION,
        "capabilities": [
            _capability(
                "plan",
                [
                    "coursedump",
                    "plan",
                    "<source>",
                    "--data",
                    "<data-home>",
                    "--json",
                ],
                ["schema_version", "source", "items", "estimate"],
                dict(_REMOTE_MACHINE_EXITS),
                "read-only",
            ),
            _capability(
                "run",
                [
                    "coursedump",
                    "run",
                    "<source>",
                    "--data",
                    "<data-home>",
                    "--json",
                ],
                ["schema_version", "status", "artifact"],
                dict(_REMOTE_MACHINE_EXITS),
                "idempotent",
            ),
            _capability(
                "status",
                [
                    "coursedump",
                    "status",
                    "--data",
                    "<data-home>",
                    "--json",
                ],
                ["schema_version", "runs"],
                dict(_MACHINE_EXITS),
                "read-only",
            ),
            _capability(
                "queue-status",
                [
                    "coursedump",
                    "queue",
                    "status",
                    "--data",
                    "<data-home>",
                    "--json",
                ],
                [
                    "schema_version",
                    "queue",
                    "queue_exists",
                    "queue_length",
                    "lock",
                    "state",
                    "progress",
                    "remaining_media_seconds",
                    "process_alive",
                    "queue_eta_seconds",
                    "speed_ratio",
                    "eta_known",
                    "next",
                    "snapshot",
                ],
                dict(_MACHINE_EXITS),
                "read-only",
            ),
        ],
    }


@app.command()
def capabilities(
    json_output: bool = typer.Option(False, "--json", help="Machine JSON contract"),
) -> None:
    """Print public machine capabilities and the contract version."""
    if not json_output:
        console.print("Usage: coursedump capabilities --json")
        raise typer.Exit(1)
    _write_machine(machine_capabilities())


@app.command()
def run(
    source: list[str] = typer.Argument(
        None, help="Paths/URLs. Empty means resume everything in out/"
    ),
    data: Path = _DATA_OPT,
    out: Path = typer.Option(None, help="Output directory (default: <data>/out)"),
    asr: str = typer.Option("mlx", help="ASR: mlx | fwhisper | dummy"),
    model: str = typer.Option("large-v3-turbo", help="Whisper model"),
    language: str = typer.Option("", help="Language (empty means auto-detect)"),
    purge_video: bool = typer.Option(
        False, "--purge-video", help="Delete downloaded video after text extraction"
    ),
    purge_zip: bool = typer.Option(
        False, "--purge-zip", help="Delete a ZIP only after a complete successful snapshot"
    ),
    cookies_from_browser: str = typer.Option(
        "", help="Pass a browser profile name to yt-dlp for authorized private sources"
    ),
    full_video: bool = typer.Option(
        False,
        "--full-video",
        help="Disable the source profile's audio-only mode",
    ),
    no_throttle: bool = typer.Option(
        False,
        "--no-throttle",
        help="Disable the source profile's rate limit and inter-item pacing",
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine JSON summary"),
):
    """Resolve, fetch, extract, and index. A repeated run resumes."""
    root = _use_data(data)
    out = out or root / "out"
    srcs = source or [s for s, _ in executor.known_courses(out)]
    if json_output and len(srcs) != 1:
        _write_machine(
            {
                "schema_version": MACHINE_SCHEMA_VERSION,
                "status": "failure",
                "artifact": None,
                "error": "machine-run-requires-exactly-one-source",
            }
        )
        raise typer.Exit(1)
    if not srcs:
        console.print(
            f"Nothing to do: no arguments and no started courses in {escape(str(out))}."
        )
        raise typer.Exit(1)

    try:
        ensure_data_dirs(root)
        opts = executor.Opts(
            out_root=out,
            asr_backend=asr,
            model=model,
            language=language,
            purge_video=purge_video,
            cookies_from_browser=cookies_from_browser,
            full_video=full_video,
            no_throttle=no_throttle,
        )
        _caffeinate()
    except KeyboardInterrupt:
        if not json_output:
            raise
        _write_machine(
            {
                "schema_version": MACHINE_SCHEMA_VERSION,
                "status": "transient",
                "artifact": None,
                "error": "interrupted",
            }
        )
        raise typer.Exit(111)
    except Exception:
        if not json_output:
            raise
        _write_machine(
            {
                "schema_version": MACHINE_SCHEMA_VERSION,
                "status": "failure",
                "artifact": None,
                "error": "run-setup-failed",
            }
        )
        raise typer.Exit(1)
    failed = 0
    machine_stats: dict[str, object] | None = None
    old_executor_console = executor.console
    if json_output:
        executor.console = Console(stderr=True, force_terminal=False, color_system=None)
    try:
        for s in srcs:
            try:
                original = s
                archive = Path(s).expanduser() if archives.is_zip_source(s) else None
                prepared = str(archives.extract(archive)) if archive is not None else s
                stats = executor.run_course(prepared, opts)
                complete = (
                    stats["total"] > 0
                    and stats["done"] == stats["total"]
                    and not stats["errors"]
                )
                if archive is not None and purge_zip and complete:
                    archives.purge(archive.resolve())
                if json_output:
                    if any(
                        type(stats.get(key)) is not int
                        for key in ("done", "total", "errors")
                    ):
                        raise ValueError("invalid run summary")
                    machine_stats = {
                        "source": original,
                        "course_dir": str(stats["course"]),
                        "done": stats["done"],
                        "total": stats["total"],
                        "errors": stats["errors"],
                        "quality_warnings": stats.get("quality_warnings", 0),
                    }
                else:
                    colour = (
                        "green"
                        if stats["done"] == stats["total"]
                        and not stats["errors"]
                        and not stats.get("quality_warnings")
                        else "yellow"
                    )
                    console.print(
                        f"[{colour}]{escape(s)}: {stats['done']}/{stats['total']} complete, "
                        f"{stats['errors']} errors, "
                        f"{stats.get('quality_warnings', 0)} quality warnings -> "
                        f"{escape(str(stats['course']))}[/{colour}]"
                    )
            except NoSpace as e:
                if json_output:
                    _write_machine(
                        {
                            "schema_version": MACHINE_SCHEMA_VERSION,
                            "status": "resource",
                            "artifact": None,
                            "error": "insufficient-space",
                        }
                    )
                    raise typer.Exit(75)
                console.print(f"[red]{escape(str(e))}[/red]")
                raise typer.Exit(2)
            except KeyboardInterrupt:
                if json_output:
                    _write_machine(
                        {
                            "schema_version": MACHINE_SCHEMA_VERSION,
                            "status": "transient",
                            "artifact": None,
                            "error": "interrupted",
                        }
                    )
                    raise typer.Exit(111)
                console.print(
                    "\n[yellow]Stopped. Resume with: coursedump run[/yellow]"
                )
                raise typer.Exit(130)
            except typer.Exit:
                raise
            except Exception as e:
                failed += 1
                if not json_output:
                    console.print(f"[red]{escape(s)}: {escape(str(e))}[/red]")
    finally:
        executor.console = old_executor_console

    if json_output:
        if failed or machine_stats is None:
            _write_machine(
                {
                    "schema_version": MACHINE_SCHEMA_VERSION,
                    "status": "failure",
                    "artifact": None,
                    "error": "run-failed",
                }
            )
            raise typer.Exit(1)
        status_name = (
            "succeeded" if not machine_stats["errors"] else "completed-with-errors"
        )
        if not _write_machine(
            {
                "schema_version": MACHINE_SCHEMA_VERSION,
                "status": status_name,
                "artifact": machine_stats,
            },
            fallback={
                "schema_version": MACHINE_SCHEMA_VERSION,
                "status": "failure",
                "artifact": None,
                "error": "machine-output-too-large",
            },
        ):
            raise typer.Exit(1)
    raise typer.Exit(1 if failed else 0)


@app.command("corpus")
def corpus_cmd(
    queue: Path = typer.Argument(..., help="Queue format: one '<url> <metadata>' per line; '#' starts a comment"),
    corpus_dir: Path = typer.Argument(..., help="Corpus directory (.txt files with a '# source:' header)"),
    data: Path = _DATA_OPT,
    out: Path = typer.Option(None, help="Working snapshots (default: <data>/staging/boosty-<blog>); only .txt enters the corpus"),
    asr: str = typer.Option("mlx", help="ASR: mlx | fwhisper | dummy"),
    model: str = typer.Option("large-v3-turbo", help="Whisper model"),
    language: str = typer.Option("", help="Language (empty means auto-detect)"),
    cookies_from_browser: str = typer.Option("", help="Browser profile name passed to yt-dlp"),
    full_video: bool = typer.Option(False, "--full-video", help="Disable the source profile's audio-only mode"),
    no_throttle: bool = typer.Option(False, "--no-throttle", help="Disable the source profile's rate limiting"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print only the preflight verdict; do not download or write"),
):
    """Turn a queue of authorized posts into a flat corpus of text files.

    Determine NEW/DONE from the post UUID in the '# source:' header before any
    source request. Metadata (title, tags, date, and level) comes from the queue.
    """
    if not queue.is_file():
        console.print(f"[red]queue not found: {escape(str(queue))}[/red]")
        raise typer.Exit(1)
    lines = corpus.read_queue(queue)
    ledger = corpus.Ledger.scan(corpus_dir)
    _ledger_head(corpus_dir, ledger)
    if ledger.unreadable:
        # An unreadable file can hide a queued UUID and produce a false NEW
        # verdict. Fail closed before building the work list.
        console.print(f"[red]unreadable corpus files ({len(ledger.unreadable)}): "
                      f"{escape('; '.join(ledger.unreadable[:5]))}; deduplication "
                      "is incomplete. Repair permissions or storage, then retry[/red]")
    if ledger.no_uuid:
        # Deduplication keys and filenames require a post UUID. A header without
        # one belongs to a different corpus contract.
        console.print(f"[red]headers without a post UUID ({len(ledger.no_uuid)}): "
                      f"{escape('; '.join(ledger.no_uuid[:5]))}; corpus "
                      "deduplication requires UUIDs[/red]")
    if ledger.unreadable or ledger.no_uuid:
        raise typer.Exit(1)
    counts = _preflight(lines, ledger)
    console.print(f"[bold]queue[/bold] {escape(str(queue))}: {len(lines)} lines -> "
                  + ", ".join(f"{k} {v}" for k, v in counts.items()))
    if counts["ERROR"]:
        # A queue is one request. Fail closed on any invalid line before data
        # directories are created or yt-dlp is invoked.
        raise typer.Exit(1)

    todo, seen = [], set()
    for ln in lines:
        if ln.problem or ledger.verdict(ln.url).done:
            continue
        if ln.key in seen:
            # The same post can appear under two URL forms. Skip the second line
            # so export does not create a suffixed duplicate.
            console.print(f"[yellow]DUPLICATE line {ln.lineno}: {ln.key} already "
                          f"appears earlier in the queue[/yellow]")
            continue
        seen.add(ln.key)
        todo.append(ln)
    if dry_run or not todo:
        raise typer.Exit(0)

    root = _use_data(data)
    ensure_data_dirs(root)
    opts = executor.Opts(out_root=out or _corpus_work_root(root, todo[0].url),
                         asr_backend=asr, model=model,
                         language=language, cookies_from_browser=cookies_from_browser,
                         full_video=full_video, no_throttle=no_throttle)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    _caffeinate()
    failed = counts["ERROR"]
    for ln in todo:
        # Select staging per line because one queue may contain multiple blogs.
        # An explicit --out always overrides this default.
        if out is None:
            opts.out_root = _corpus_work_root(root, ln.url)
        try:
            # Finish an interrupted publication directly from a complete local
            # snapshot instead of contacting the source again.
            done_dir = executor.completed_course(opts.out_root, ln.url)
            if done_dir is not None:
                console.print(f"[yellow]FINISHING[/yellow] line {ln.lineno}: snapshot "
                              f"{escape(str(done_dir))} is complete; exporting "
                              "without contacting the source")
                course_dir = done_dir
            else:
                course_dir = Path(executor.run_course(ln.url, opts)["course"])
            written, missing = corpus.export(course_dir, ln, corpus_dir)
        except NoSpace as e:
            console.print(f"[red]{escape(str(e))}[/red]")
            raise typer.Exit(2)
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped. Resume with the same command[/yellow]")
            raise typer.Exit(130)
        except Exception as e:
            if boosty_text.is_no_videos(e):
                try:
                    boosty_text.pause_after_no_videos()
                    post = boosty_text.fetch(ln.url, cookies_from_browser)
                    path = corpus.export_text_post(ln, post.title, post.body, corpus_dir)
                except Exception as text_error:
                    failed += 1
                    console.print(
                        f"[red]FAIL line {ln.lineno} {escape(ln.url)}: "
                        f"text fallback: {escape(str(text_error))}[/red]"
                    )
                else:
                    console.print(f"[green]DONE[/green] {escape(path.name)}")
                continue
            failed += 1
            console.print(f"[red]FAIL line {ln.lineno} {escape(ln.url)}: {escape(str(e))}[/red]")
            continue
        for p in written:
            console.print(f"[green]DONE[/green] {escape(p.name)}")
        if missing:
            # Post-level transaction: export nothing when any item is missing,
            # so the post stays NEW and a repeated run can finish it.
            failed += 1
            console.print(f"[red]INCOMPLETE line {ln.lineno}: {len(missing)} items "
                          f"not extracted ({escape(', '.join(missing[:3]))}); "
                          "nothing entered the corpus and the post remains NEW. "
                          "See the course errors.jsonl and rerun[/red]")
    raise typer.Exit(1 if failed else 0)


def _staging_dir(root: Path, url: str) -> Path:
    """Place corpus snapshots under `<data>/staging/boosty-<blog>/`.

    Blog posts are not courses. Keeping their working snapshots outside out/
    prevents argument-free course resume and status commands from treating every
    post as a course. Grouping by blog preserves the source boundary.
    """
    return root / "staging" / f"boosty-{slugify(corpus.post_blog(url))}"


def _corpus_work_root(root: Path, url: str) -> Path:
    """Resume a legacy snapshot in out/, otherwise start it in staging.

    Existing source.json state remains authoritative until that snapshot is
    complete, avoiding a duplicate directory after the layout migration.
    """
    legacy_out = root / "out"
    course, _ = executor.saved_state(legacy_out, url)
    return legacy_out if course is not None else _staging_dir(root, url)


def _ledger_head(corpus_dir: Path, ledger) -> None:
    console.print(f"[bold]corpus[/bold] {escape(str(corpus_dir))}: {ledger.files} files, "
                  f"{len(ledger.by_key)} UUIDs in '{corpus.HEAD_SRC.strip()}' headers")
    if ledger.headless:
        # Headerless files are invisible to deduplication.
        console.print(f"[yellow]without a header ({len(ledger.headless)}), invisible to deduplication: "
                      f"{escape(', '.join(ledger.headless[:5]))}[/yellow]")


def _preflight(lines: list, ledger) -> dict:
    """Print an evidence-backed verdict for each queue line before network use."""
    counts = {"DONE": 0, "NEW": 0, "ERROR": 0}
    for ln in lines:
        problem = ln.problem   # A missing UUID or date fails closed.
        if problem:
            counts["ERROR"] += 1
            console.print(f"[red]ERROR line {ln.lineno}: {escape(problem)}: "
                          f"{escape(ln.url)}[/red]")
            continue
        v = ledger.verdict(ln.url)
        counts[v.status] += 1
        if v.done:  # Evidence includes the matched UUID and files.
            console.print(f"[green]DONE[/green] {v.key} <- "
                          f"{escape(', '.join(ledger.by_key[v.key]))}")
        else:
            console.print(f"[yellow]NEW[/yellow]  {v.key} {escape(ln.meta[:60])}")
    return counts


@app.command()
def plan(
    source: str = typer.Argument(..., help="Path or URL"),
    data: Path = _DATA_OPT,
    cookies_from_browser: str = typer.Option(""),
    full_video: bool = typer.Option(
        False,
        "--full-video",
        help="Estimate full-video sizes instead of the profile's audio-only mode",
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine JSON plan"),
):
    """Dry-run discovery, sizes, media duration, and time estimate. Writes nothing."""
    _use_data(data)
    # Planning needs this flag because audio-only and full-video sizes differ greatly.
    try:
        model = planning.build(source, cookies_from_browser, full_video)
        source_info = model["source"]
        source_title = source_info["title"]
        source_adapter = source_info["adapter"]
        source_is_local = source_info["is_local"]
        item_groups = model["items"]
        estimate = model["estimate"]
        by_kind = model["_by_kind"]
        media = estimate["media_items"]
        media_size = estimate["media_bytes"]
    except KeyboardInterrupt:
        if not json_output:
            raise
        _write_machine(
            {
                "schema_version": MACHINE_SCHEMA_VERSION,
                "source": None,
                "items": [],
                "estimate": None,
                "status": "transient",
                "error": "interrupted",
            }
        )
        raise typer.Exit(111)
    except Exception:
        if not json_output:
            raise
        _write_machine(
            {
                "schema_version": MACHINE_SCHEMA_VERSION,
                "source": None,
                "items": [],
                "estimate": None,
                "status": "failure",
                "error": "plan-failed",
            }
        )
        raise typer.Exit(1)

    if json_output:
        if not _write_machine(
            {
                "schema_version": MACHINE_SCHEMA_VERSION,
                "source": {
                    "input": source,
                    "title": source_title,
                    "adapter": source_adapter,
                    "is_local": source_is_local,
                },
                "items": item_groups,
                "estimate": estimate,
            },
            fallback={
                "schema_version": MACHINE_SCHEMA_VERSION,
                "source": None,
                "items": [],
                "estimate": None,
                "status": "failure",
                "error": "machine-output-too-large",
            },
        ):
            raise typer.Exit(1)
        return

    t = Table(title=escape(source_title))
    for col in ("type", "files", "size", "to process"):
        t.add_column(col)
    for kind, its in sorted(by_kind.items()):
        work = sum(1 for i in its if i.target)
        t.add_row(kind, str(len(its)), human_size(sum(i.size for i in its)), str(work))
    console.print(t)

    if media and estimate["basis"] == "duration":
        console.print("Measuring durations with ffprobe...")
        secs = estimate["media_duration_seconds"]
        console.print(
            f"Video/audio: {human_dur(secs)}; ASR ~{ASR_SPEED:.0f}x realtime "
            f"-> approximately {human_dur(secs / ASR_SPEED)} of machine time."
        )
    elif media:
        secs = estimate["media_duration_seconds"]
        console.print(
            f"Video/audio ~{human_size(media_size)}; size suggests about {human_dur(secs)} "
            f"-> ASR approximately {human_dur(secs / ASR_SPEED)} (rough estimate)."
        )


def _queue_path(root: Path, value: Path | None) -> Path:
    return value.expanduser().resolve() if value is not None else root / "queue.txt"


@queue_app.command("plan")
def queue_plan(
    data: Path = _DATA_OPT,
    queue: Path = typer.Option(None, "--queue", help="Queue (default: <data>/queue.txt)"),
    cookies_from_browser: str = typer.Option("", help="Browser profile for authorized private URLs"),
    full_video: bool = typer.Option(False, "--full-video"),
):
    """Show queued sources, outputs, and size estimates without writing."""
    root = _use_data(data)
    path = _queue_path(root, queue)
    try:
        rows = queueing.plan_queue(root, path, cookies_from_browser, full_video)
    except queueing.QueueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1)
    table = Table(title=f"queue {escape(str(path))}")
    for column in ("#", "item", "type", "out", "size", "estimate"):
        table.add_column(column)
    failed = 0
    for row in rows:
        seconds = row["duration_seconds"]
        estimate = (
            human_dur(seconds) + " media"
            if seconds
            else human_size(row["size_bytes"])
            if row["estimate_known"]
            else "unknown"
        )
        size = human_size(row["size_bytes"]) if row["estimate_known"] else "unknown"
        if row["error"]:
            failed += 1
            estimate = "ERROR: " + str(row["error"])
        table.add_row(
            str(row["line"]), escape(str(row["entry"])), str(row["kind"]),
            str(row["out"]), size, escape(estimate),
        )
    console.print(table)
    console.print(f"Total: {len(rows)} items" + (f", errors {failed}" if failed else ""))
    raise typer.Exit(1 if failed else 0)


@queue_app.command("run")
def queue_run(
    data: Path = _DATA_OPT,
    queue: Path = typer.Option(None, "--queue", help="Queue (default: <data>/queue.txt)"),
    detach: bool = typer.Option(False, "--detach", help="Start with nohup and return the PID"),
    log: Path = typer.Option(None, "--log", hidden=True),
    asr: str = typer.Option("mlx", help="ASR: mlx | fwhisper | dummy"),
    model: str = typer.Option("large-v3-turbo", help="Whisper model"),
    language: str = typer.Option("", help="Language (empty means auto-detect)"),
    cookies_from_browser: str = typer.Option("", help="Browser profile for authorized private sources"),
    purge_video: bool = typer.Option(False, "--purge-video"),
    purge_zip: bool = typer.Option(False, "--purge-zip"),
    full_video: bool = typer.Option(False, "--full-video"),
    no_throttle: bool = typer.Option(False, "--no-throttle"),
    transient_wait: int = typer.Option(queueing.TRANSIENT_WAIT, min=0),
    transient_retries: int = typer.Option(queueing.TRANSIENT_RETRIES, min=1),
):
    """Run every queue line sequentially."""
    root = data_root(data)
    path = _queue_path(root, queue)
    log_path = log.expanduser().resolve() if log is not None else queueing.new_log_path(root)
    if detach:
        try:
            queueing.read_queue(path)
        except queueing.QueueError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(1)
        lock = queueing.lock_status(root / "queue.lock")
        if lock["alive"]:
            console.print(
                f"[red]queue is already running: pid {lock['pid']}, lock "
                f"{escape(str(root / 'queue.lock'))}[/red]"
            )
            raise typer.Exit(1)
        argv = [
            "--asr", asr, "--model", model,
            "--transient-wait", str(transient_wait),
            "--transient-retries", str(transient_retries),
        ]
        if language:
            argv += ["--language", language]
        if cookies_from_browser:
            argv += ["--cookies-from-browser", cookies_from_browser]
        for enabled, flag in (
            (purge_video, "--purge-video"), (purge_zip, "--purge-zip"),
            (full_video, "--full-video"), (no_throttle, "--no-throttle"),
        ):
            if enabled:
                argv.append(flag)
        try:
            pid, returncode = queueing.spawn_detached(
                root, path, log_path, REPO_ROOT, argv
            )
        except queueing.QueueError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(1)
        if returncode is None:
            console.print(f"pid {pid}; log {escape(str(log_path))}")
        else:
            console.print(f"process already exited: exit {returncode}; log {escape(str(log_path))}")
        return
    try:
        counts = queueing.run_queue(
            root, path, log_path=log_path,
            asr=asr, model=model, language=language,
            cookies_from_browser=cookies_from_browser,
            purge_video=purge_video, purge_zip=purge_zip,
            full_video=full_video, no_throttle=no_throttle,
            transient_wait=transient_wait, transient_retries=transient_retries,
        )
    except queueing.QueueLocked as exc:
        # A held lock means another process is doing the work. Periodic callers
        # treat that as a successful no-op rather than a terminal queue failure.
        console.print(f"[yellow]{escape(str(exc))}; nothing to do[/yellow]")
        raise typer.Exit(0)
    except queueing.QueueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("Stopped. A repeated queue run resumes from files on disk.")
        raise typer.Exit(130)
    # Keep FAIL in the tick report, but do not repeatedly fail a periodic job
    # when the same course files have not changed.
    raise typer.Exit(1 if counts.new_failures else 0)


@queue_app.command("status")
def queue_status(
    data: Path = _DATA_OPT,
    queue: Path = typer.Option(None, "--queue", help="Queue (default: <data>/queue.txt)"),
    json_output: bool = typer.Option(False, "--json"),
):
    """Show the current item, progress, ETA, and upcoming queue items."""
    root = data_root(data)
    payload = queueing.status_payload(root, _queue_path(root, queue))
    if json_output:
        state = payload.get("state") or {}
        _write_machine(
            payload,
            fallback={
                "schema_version": MACHINE_SCHEMA_VERSION,
                "queue": payload["queue"],
                "queue_exists": payload["queue_exists"],
                "queue_length": payload["queue_length"],
                "lock": payload["lock"],
                "state": {
                    "status": state.get("status"),
                    "pid": state.get("pid"),
                    "counts": state.get("counts") or {},
                    "done": state.get("done"),
                    "remaining": state.get("remaining"),
                    "results": [],
                    "results_total": state.get("results_total", 0),
                    "results_truncated": True,
                },
                "progress": None,
                "remaining_media_seconds": None,
                "queue_eta_seconds": None,
                "speed_ratio": payload.get("speed_ratio"),
                "eta_known": payload.get("eta_known"),
                "process_alive": payload.get("process_alive"),
                "next": [],
                "snapshot": payload.get("snapshot"),
                "truncated": True,
            },
        )
        return
    state = payload["state"]
    lock = payload["lock"]
    if not state:
        console.print(f"Queue has not run yet; lines: {payload['queue_length']}.")
    else:
        status_name = str(state.get("status", "?"))
        current = state.get("current") or {}
        interrupted = status_name == "running" and payload.get("process_alive") is False
        current_display = current.get("entry")
        if interrupted:
            entry = (
                queueing._entry_slug(current.get("entry"))
                or queueing._one_line(current.get("entry"))
                or "unknown"
            )
            current_display = entry
            stage = current.get("stage") or "unknown"
            console.print(
                f"queue: interrupted (process died on {escape(str(entry))}, "
                f"stage {escape(str(stage))})"
            )
            console.print("queue run resumes from files on disk")
        else:
            console.print(
                f"queue: {escape(status_name)}; "
                f"lock: {'live' if lock['alive'] else lock['problem'] or 'none'}"
            )
        if status_name == "failed" and state.get("last_error"):
            console.print(f"error: {escape(str(state['last_error']))}")
        if current:
            progress = payload["progress"] or {}
            eta = progress.get("eta_seconds")
            eta_text = human_dur(eta) if eta is not None else "unknown"
            console.print(
                f"current: {escape(str(current_display))}; stage "
                f"{escape(str(current.get('stage')))}; "
                f"{progress.get('done', 0)}/{progress.get('total', 0)} "
                f"({progress.get('percent', 0):.1f}%); ETA {eta_text}"
            )
        remaining_media = payload.get("remaining_media_seconds")
        queue_eta = payload.get("queue_eta_seconds")
        speed = payload.get("speed_ratio")
        if remaining_media is not None and speed is not None:
            unknown = int(state.get("remaining_unknown_estimates") or 0)
            media_text = f"~{float(remaining_media) / 3600:.1f} h media"
            if unknown:
                media_text += f" (+{unknown} courses without estimates)"
            eta_text = (
                f"~{human_dur(float(queue_eta))}"
                if queue_eta is not None and payload.get("eta_known")
                else "unknown"
            )
            console.print(
                f"queue: {state.get('remaining', 0)} courses remaining, "
                f"{media_text}, ETA {eta_text} (speed {float(speed):.1f}x)"
            )
        counts = state.get("counts") or {}
        quality = int(state.get("quality_warnings") or 0)
        quality_text = f" QUALITY={quality}" if quality else ""
        console.print(
            "current totals: "
            + " ".join(f"{k}={v}" for k, v in counts.items())
            + quality_text
        )
    if payload["next"]:
        console.print("next: " + ", ".join(escape(str(value)) for value in payload["next"]))


@app.command()
def status(
    data: Path = _DATA_OPT,
    out: Path = typer.Option(None, help="Course directory (default: <data>/out)"),
    json_output: bool = typer.Option(False, "--json", help="Machine JSON status"),
):
    """Show progress and errors for all courses."""
    out = out or data_root(data) / "out"
    rows = []
    machine_runs = []
    try:
        for src_str, d in executor.known_courses(out):
            mpath = d / "manifest.jsonl"
            if not mpath.exists():
                continue
            items = manifest.load(mpath)
            work = [it for it in items if it.target]
            done = sum(1 for it in work if (d / "text" / it.target).exists())
            ej = d / "errors.jsonl"
            nerr = (
                len(ej.read_text(encoding="utf-8").splitlines()) if ej.exists() else 0
            )
            quality_warnings = sum(
                1
                for it in work
                if queueing.md_has_quality_warning(d / "text" / it.target)
            )
            rows.append(
                (
                    escape(d.name),
                    f"{done}/{len(work)}",
                    str(nerr),
                    f"QUALITY={quality_warnings}" if quality_warnings else "0",
                    escape(src_str),
                )
            )
            machine_runs.append(
                {
                    "course": d.name,
                    "source": src_str,
                    "artifact": str(d),
                    "done": done,
                    "total": len(work),
                    "errors": nerr,
                    "quality_warnings": quality_warnings,
                }
            )
    except KeyboardInterrupt:
        if not json_output:
            raise
        _write_machine(
            {
                "schema_version": MACHINE_SCHEMA_VERSION,
                "runs": [],
                "status": "failure",
                "error": "interrupted",
            }
        )
        raise typer.Exit(1)
    except Exception:
        if not json_output:
            raise
        _write_machine(
            {
                "schema_version": MACHINE_SCHEMA_VERSION,
                "runs": [],
                "status": "failure",
                "error": "status-read-failed",
            }
        )
        raise typer.Exit(1)

    if json_output:
        if not _write_machine(
            {
                "schema_version": MACHINE_SCHEMA_VERSION,
                "runs": machine_runs,
            },
            fallback={
                "schema_version": MACHINE_SCHEMA_VERSION,
                "runs": [],
                "status": "failure",
                "error": "machine-output-too-large",
            },
        ):
            raise typer.Exit(1)
        return

    if not rows:
        console.print(f"{escape(str(out))} is empty.")
        return
    t = Table()
    for col in ("course", "complete", "errors (log)", "quality", "source"):
        t.add_column(col)
    for r in rows:
        t.add_row(*r)
    console.print(t)


@app.command("catalog")
def catalog_cmd(
    data: Path = _DATA_OPT,
    path: Path = typer.Option(None, "--file", help="Catalog file (default: <data>/catalog.tsv)"),
):
    """Rebuild <data>/catalog.tsv while preserving title, URL, and notes."""
    root = data_root(data)
    target, rows = catalog.refresh(root, path)
    gone = sum(1 for row in rows if row.get("in") != "present")
    blank = sum(1 for row in rows if row.get("url", "-") in ("", "-"))
    console.print(f"{escape(str(target))}: {len(rows)} courses, {gone} without in/, {blank} without URLs")


@app.command()
def doctor(data: Path = _DATA_OPT):
    """Diagnose dependencies and the data directory without writing."""
    root = data_root(data)
    problems: list[str] = []

    tools = {name: shutil.which(name) is not None
             for name in ("ffmpeg", "ffprobe", "rclone")}
    # Run yt-dlp as a module of this interpreter, not as a PATH executable:
    # the latter may be a different version.
    tools["yt_dlp (module)"] = importlib.util.find_spec("yt_dlp") is not None
    for name in ("ffmpeg", "ffprobe"):
        if not tools[name]:
            problems.append(f"missing {name} executable (brew install ffmpeg)")
    if not tools["yt_dlp (module)"]:
        problems.append(f"yt_dlp is not installed in {sys.executable}; "
                        "video-platform URLs will not work (run uv sync)")
    # rclone is optional and required only for cloud remotes.

    # yt-dlp needs a JavaScript runtime for complete YouTube format discovery.
    js = list(sources._js_runtimes()) if tools["yt_dlp (module)"] else []
    if tools["yt_dlp (module)"] and not js:
        problems.append("no JavaScript runtime for yt-dlp (deno / node >= 23.5 / "
                        "quickjs / bun); install deno")

    asr = {name: importlib.util.find_spec(mod) is not None
           for name, mod in (("mlx-whisper", "mlx_whisper"), ("faster-whisper", "faster_whisper"))}
    if not any(asr.values()):
        problems.append("no ASR backend is installed (mlx_whisper / faster_whisper); "
                        "videos without subtitles cannot be transcribed")

    data_state = "absent (created by the first run)"
    if root.is_dir():
        data_state = "present"
        if not os.access(root, os.W_OK):
            data_state = "present, NOT writable"
            problems.append(f"data directory is not writable: {root}")
    filters = root / "filters.toml"
    queue_path = root / "queue.txt"
    try:
        queue_len = len(queueing.read_queue(queue_path))
        queue_state = f"present, {queue_len} lines"
    except queueing.QueueError:
        queue_state = "absent"
    qlock = queueing.lock_status(root / "queue.lock")
    lock_state = (
        f"live, pid {qlock['pid']}" if qlock["alive"]
        else str(qlock["problem"] or "none")
    )

    console.print("coursedump doctor: " + ("[green]READY[/green]" if not problems else "[red]NOT READY[/red]"))
    console.print("  tools: " + ", ".join(
        f"{n}={'present' if ok else 'MISSING'}" for n, ok in tools.items())
        + " (rclone is needed only for supported cloud remotes)")
    console.print("  JavaScript runtime (yt-dlp/YouTube): " + (", ".join(js) if js else "MISSING"))
    console.print("  ASR: " + ", ".join(f"{n}={'present' if ok else 'missing'}" for n, ok in asr.items()))
    console.print(f"  data: {escape(str(root))} - {data_state}")
    console.print("  filters: " + escape(str(filters) if filters.exists()
                                        else f"package default (working copy will be seeded at {filters})"))
    console.print(f"  queue: {queue_state}; lock: {escape(lock_state)}")
    for pr in problems:
        console.print(f"  [red]problem: {escape(pr)}[/red]")
    raise typer.Exit(0 if not problems else 1)


if __name__ == "__main__":
    app()
