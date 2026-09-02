"""The regular course queue and the zip preprocessor."""

import json
import os
import re
import shutil
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from coursedump import archives, manifest, queueing
from coursedump.cli import MAX_MACHINE_OUTPUT_BYTES, app


runner = CliRunner()


def _zip(path: Path, files: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return path


def _complete(out: Path, source: str, name: str) -> None:
    course = out / name
    (course / "text").mkdir(parents=True, exist_ok=True)
    (course / "source.json").write_text(
        json.dumps({"source": source, "title": name}), encoding="utf-8"
    )
    manifest.save(
        [manifest.Item(rel="lesson.txt", kind="text", target="lesson.txt.md")],
        course / "manifest.jsonl",
    )
    (course / "text" / "lesson.txt.md").write_text(
        "---\nduration: 10\n---\n# lesson\n\ntext\n", encoding="utf-8"
    )


def _hook_config(root: Path, command: str = "notify") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.toml").write_text(
        f"on_event = {json.dumps(command)}\n", encoding="utf-8"
    )


def _capture_hooks(monkeypatch, returncode: int = 0):
    calls = []

    def run(cmd, **kwargs):
        calls.append({"cmd": cmd, "env": dict(kwargs["env"]), "kwargs": kwargs})
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(queueing.subprocess, "run", run)
    return calls


def test_plan_understands_zip_without_unpacking(tmp_path):
    archive = _zip(tmp_path / "in" / "course.zip", {"course/lesson.txt": "text"})
    result = runner.invoke(app, ["plan", str(archive), "--data", str(tmp_path / "data"), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["source"]["adapter"] == "ZipPlanSource"
    assert payload["source"]["is_local"] is True
    assert payload["items"] == [
        {"count": 1, "kind": "text", "size_bytes": len("text".encode()), "work_items": 1}
    ]
    assert not archives.extraction_dir(archive).exists()
    assert not archives.marker_path(archive).exists()


def test_run_unpacks_zip_idempotently_and_keeps_it_by_default(tmp_path):
    data = tmp_path / "data"
    archive = _zip(data / "in" / "course.zip", {"course/lesson.txt": "first version"})
    result = runner.invoke(app, ["run", str(archive), "--data", str(data), "--asr", "dummy"])

    assert result.exit_code == 0, result.output
    extracted = data / "in" / "course" / "lesson.txt"
    assert extracted.read_text(encoding="utf-8") == "first version"
    assert archive.exists() and archives.marker_path(archive).is_file()
    assert (data / "out" / "course" / "text" / "lesson.txt.md").is_file()

    extracted.write_text("do not overwrite while the marker is valid", encoding="utf-8")
    assert archives.extract(archive) == data / "in" / "course"
    assert extracted.read_text(encoding="utf-8") == "do not overwrite while the marker is valid"


def test_purge_zip_only_after_success_and_only_by_flag(tmp_path):
    data = tmp_path / "data"
    archive = _zip(data / "in" / "course.zip", {"lesson.txt": "text"})
    result = runner.invoke(
        app,
        ["run", str(archive), "--data", str(data), "--asr", "dummy", "--purge-zip"],
    )
    assert result.exit_code == 0, result.output
    assert not archive.exists() and not archives.marker_path(archive).exists()
    assert (data / "in" / "course" / "lesson.txt").exists()


def test_purge_zip_keeps_the_only_archive_when_extraction_has_item_errors(tmp_path):
    data = tmp_path / "data"
    archive = _zip(data / "in" / "broken.zip", {"broken.pdf": "not a pdf"})
    result = runner.invoke(
        app,
        ["run", str(archive), "--data", str(data), "--asr", "dummy", "--purge-zip"],
    )
    # The historical human contract of run: an item error does not change exit 0.
    # Deletion needs the stronger gate - a complete snapshot with no errors.
    assert result.exit_code == 0, result.output
    assert archive.exists() and archives.marker_path(archive).exists()


@pytest.mark.parametrize(
    ("member", "mode"),
    [
        ("../outside.txt", None),
        ("/absolute.txt", None),
        ("..\\outside.txt", None),
        ("link", stat.S_IFLNK | 0o777),
        ("pipe", stat.S_IFIFO | 0o644),
    ],
)
def test_zip_slip_is_rejected_before_the_first_write(tmp_path, member, mode):
    archive = tmp_path / "in" / "evil.zip"
    if mode is None:
        _zip(archive, {member: "owned", "safe.txt": "safe"})
    else:
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w") as zf:
            special = zipfile.ZipInfo(member)
            special.create_system = 3
            special.external_attr = mode << 16
            zf.writestr(special, "owned")
            zf.writestr("safe.txt", "safe")
    with pytest.raises(archives.UnsafeArchive, match="ZIP-slip|unsafe|special files"):
        archives.extract(archive)
    assert not (tmp_path / "in" / "evil").exists()
    assert not (tmp_path / "outside.txt").exists()


def test_live_lock_refuses_a_second_runner_and_stale_lock_is_recovered(tmp_path):
    lock = tmp_path / "queue.lock"
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    with pytest.raises(queueing.QueueLocked, match="already running"):
        with queueing.QueueLock(lock):
            pass

    lock.write_text("99999999\n", encoding="utf-8")
    with queueing.QueueLock(lock):
        assert int(lock.read_text()) == os.getpid()
    assert not lock.exists()


def test_queue_counts_finished_but_incomplete_as_fail_and_continues(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "in" / "broken").mkdir(parents=True)
    (root / "in" / "good").mkdir(parents=True)
    queue = root / "queue.txt"
    queue.write_text("broken\ngood\n", encoding="utf-8")
    calls = []

    def invoke(source, data, log, options):
        calls.append(source)
        if source.endswith("/good"):
            _complete(root / "out", source, "good")
        return 0

    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    counts = queueing.run_queue(root, queue, log_path=root / "logs" / "test.log")

    assert counts == {"DONE": 1, "FAIL": 1, "SKIP": 0}
    assert [Path(call).name for call in calls] == ["broken", "good"]
    state = json.loads((root / "logs" / "queue-state.json").read_text())
    assert state["status"] == "finished" and state["counts"] == counts
    assert "snapshot is incomplete" in state["last_error"]


def test_queue_retries_transient_code_with_bounded_waits(tmp_path, monkeypatch):
    root = tmp_path / "data"
    source_dir = root / "in" / "course"
    source_dir.mkdir(parents=True)
    queue = root / "queue.txt"
    queue.write_text("course\n", encoding="utf-8")
    returns = iter([111, 0])
    sleeps = []

    def invoke(source, data, log, options):
        rc = next(returns)
        if rc == 0:
            _complete(root / "out", source, "course")
        return rc

    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    monkeypatch.setattr(queueing, "_sleep", sleeps.append)
    counts = queueing.run_queue(
        root, queue, log_path=root / "logs" / "test.log",
        transient_wait=3, transient_retries=2,
    )
    assert counts == {"DONE": 1, "FAIL": 0, "SKIP": 0}
    assert sleeps == [3]


def test_exit_75_is_disk_space_failure_without_retry_and_queue_continues(
    tmp_path, monkeypatch
):
    root = tmp_path / "data"
    for name in ("quota", "next"):
        (root / "in" / name).mkdir(parents=True)
    queue = root / "queue.txt"
    queue.write_text("quota\nnext\n", encoding="utf-8")
    calls = []

    def invoke(source, data, log, options):
        calls.append(Path(source).name)
        if source.endswith("/next"):
            _complete(root / "out", source, "next")
            return 0
        return 75

    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    sleeps = []
    monkeypatch.setattr(queueing, "_sleep", sleeps.append)
    log = root / "logs" / "test.log"
    counts = queueing.run_queue(root, queue, log_path=log)
    assert calls == ["quota", "next"]
    assert sleeps == []
    assert counts == {"DONE": 1, "FAIL": 1, "SKIP": 0}
    assert "insufficient space (exit 75)" in log.read_text(encoding="utf-8")


def test_queue_refreshes_completed_local_course_before_skip(tmp_path):
    root = tmp_path / "data"
    source = root / "in" / "course"
    source.mkdir(parents=True)
    (source / "one.txt").write_text("one", encoding="utf-8")
    queue = root / "queue.txt"
    queue.write_text("course\n", encoding="utf-8")

    first = runner.invoke(
        app,
        ["queue", "run", "--data", str(root), "--asr", "dummy"],
    )
    assert first.exit_code == 0, first.output
    (source / "two.txt").write_text("two", encoding="utf-8")

    second = runner.invoke(
        app,
        ["queue", "run", "--data", str(root), "--asr", "dummy"],
    )
    assert second.exit_code == 0, second.output
    assert (root / "out" / "course" / "text" / "two.txt.md").is_file()
    state = json.loads((root / "logs" / "queue-state.json").read_text(encoding="utf-8"))
    assert state["results"][-1]["entry"] == "course"
    assert state["results"][-1]["status"] == "DONE"
    assert state["results"][-1]["elapsed"] >= 0


def test_deleted_source_of_a_complete_course_is_skipped_not_failed(tmp_path):
    # The owner deletes the video to free space and the queue line stays behind as
    # a record that the course existed. This used to FAIL (a job stop code) on
    # every tick.
    root = tmp_path / "data"
    source = root / "in" / "course"
    source.mkdir(parents=True)
    (source / "one.txt").write_text("one", encoding="utf-8")
    (root / "queue.txt").write_text("course\n", encoding="utf-8")

    first = runner.invoke(app, ["queue", "run", "--data", str(root), "--asr", "dummy"])
    assert first.exit_code == 0, first.output
    shutil.rmtree(source)

    second = runner.invoke(app, ["queue", "run", "--data", str(root), "--asr", "dummy"])
    assert second.exit_code == 0, second.output
    assert "FAIL=0 SKIP=1" in second.output
    assert (root / "out" / "course" / "text" / "one.txt.md").is_file()
    state = json.loads((root / "logs" / "queue-state.json").read_text(encoding="utf-8"))
    assert state["results"][-1] == {
        **state["results"][-1],
        "entry": "course",
        "status": "SKIP",
    }


def test_deleted_source_of_an_incomplete_course_still_fails(tmp_path):
    # A missing source for an unfinished course is a real failure, not cleanup.
    root = tmp_path / "data"
    source = root / "in" / "course"
    source.mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    shutil.rmtree(source)

    result = runner.invoke(app, ["queue", "run", "--data", str(root), "--asr", "dummy"])
    assert result.exit_code == 1, result.output
    state = json.loads((root / "logs" / "queue-state.json").read_text(encoding="utf-8"))
    assert state["results"][-1]["status"] == "FAIL"


def test_queue_plan_resolves_slug_folder_and_zip_and_reports_out(tmp_path):
    root = tmp_path / "data"
    folder = root / "in" / "done"
    folder.mkdir(parents=True)
    (folder / "lesson.txt").write_text("x", encoding="utf-8")
    archive = _zip(root / "in" / "packed.zip", {"lesson.txt": "y"})
    _complete(root / "out", str(folder.resolve()), "done")
    queue = root / "queue.txt"
    queue.write_text("# order\ndone\npacked\n", encoding="utf-8")

    rows = queueing.plan_queue(root, queue)
    assert [(row["entry"], row["kind"], row["out"]) for row in rows] == [
        ("done", "directory", "complete"),
        ("packed", "zip", "missing"),
    ]
    assert rows[1]["source"] == str(archive.resolve())


def test_bare_slug_is_resolved_only_inside_data_in(tmp_path, monkeypatch):
    root = tmp_path / "data"
    expected = root / "in" / "course"
    expected.mkdir(parents=True)
    cwd = tmp_path / "caller"
    (cwd / "course").mkdir(parents=True)
    queue = root / "queue.txt"
    queue.write_text("course\n", encoding="utf-8")
    monkeypatch.chdir(cwd)

    line = queueing.read_queue(queue)[0]
    assert queueing.resolve_source(line, root, queue) == str(expected.resolve())


def test_explicit_relative_path_is_resolved_from_queue_file(tmp_path, monkeypatch):
    queue_dir = tmp_path / "lists"
    source = tmp_path / "course"
    source.mkdir()
    queue_dir.mkdir()
    queue = queue_dir / "queue.txt"
    queue.write_text("../course\n", encoding="utf-8")
    unrelated = tmp_path / "caller"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    line = queueing.read_queue(queue)[0]
    assert queueing.resolve_source(line, tmp_path / "data", queue) == str(source.resolve())


def test_course_progress_rejects_foreign_basename_fallback(tmp_path):
    out = tmp_path / "out"
    foreign = str((tmp_path / "foreign" / "in" / "course").resolve())
    expected = str((tmp_path / "data" / "in" / "course").resolve())
    _complete(out, foreign, "course")

    assert queueing.course_progress(out, expected, "course")["exists"] is False


def test_course_progress_accepts_legacy_relative_source_with_absolute_root(tmp_path):
    """Snapshots written before 2026-08 keep a relative `source` (in/<slug>/) in
    source.json and the absolute path in `root`. Such an out directory is ours,
    not a stranger's (queue regression 2026-08-22)."""
    out = tmp_path / "out"
    expected = str((tmp_path / "data" / "in" / "course").resolve())
    _complete(out, "in/course/", "course")
    (out / "course" / "source.json").write_text(
        json.dumps({"source": "in/course/", "root": expected, "title": "course"}),
        encoding="utf-8",
    )

    progress = queueing.course_progress(out, expected, "course")
    assert progress["exists"] is True
    assert progress["complete"] is True


_QUALITY_MD = (
    "---\nduration: 10\n---\n# lesson\n\n"
    "# QUALITY: ASR loop, speech not recovered\n\ntext\n"
)


def test_queue_result_reports_quality_warning_of_work_it_did(tmp_path, monkeypatch):
    """A course finished by THIS tick (DONE) reports the quality marks it produced."""
    root = tmp_path / "data"
    source = root / "in" / "course"
    source.mkdir(parents=True)
    _complete(root / "out", str(source.resolve()), "course")
    md = root / "out" / "course" / "text" / "lesson.txt.md"
    md.unlink()  # incomplete snapshot -> the tick does work and closes it as DONE

    def run(*_args):
        md.write_text(_QUALITY_MD, encoding="utf-8")
        return 0

    queue = root / "queue.txt"
    queue.write_text("course\n", encoding="utf-8")
    monkeypatch.setattr(queueing, "_invoke_run", run)

    counts = queueing.run_queue(root, queue, log_path=root / "queue.log")
    state = json.loads((root / "logs" / "queue-state.json").read_text(encoding="utf-8"))

    assert counts == {"DONE": 1, "FAIL": 0, "SKIP": 0}
    assert state["quality_warnings"] == 1
    assert state["results"][0]["quality_warnings"] == 1
    assert state["results"][0]["quality_warning_files"] == ["lesson.txt.md"]
    assert "QUALITY: ASR loop in 1 files" in (root / "queue.log").read_text(
        encoding="utf-8"
    )
    status = runner.invoke(app, ["queue", "status", "--data", str(root)])
    assert status.exit_code == 0
    assert "QUALITY=1" in status.output


def test_queue_skip_does_not_reannounce_old_quality_warning(tmp_path, monkeypatch):
    """A finished course (SKIP) does not re-announce its old quality marks.

    A tick reports on ITS OWN work, and SKIP has none by definition: the snapshot
    was already complete before the tick and the manifest did not change.
    Recomputing every hour turned QUALITY into permanent background noise - in one
    live case two files stayed in the summary for a day after they had been dealt
    with. The mark itself stays in the md file and in the course INDEX.md.
    """
    root = tmp_path / "data"
    source = root / "in" / "course"
    source.mkdir(parents=True)
    _complete(root / "out", str(source.resolve()), "course")
    md = root / "out" / "course" / "text" / "lesson.txt.md"
    md.write_text(_QUALITY_MD, encoding="utf-8")
    queue = root / "queue.txt"
    queue.write_text("course\n", encoding="utf-8")
    monkeypatch.setattr(queueing, "_invoke_run", lambda *_args: 0)

    counts = queueing.run_queue(root, queue, log_path=root / "queue.log")
    state = json.loads((root / "logs" / "queue-state.json").read_text(encoding="utf-8"))

    assert counts == {"DONE": 0, "FAIL": 0, "SKIP": 1}
    assert state["quality_warnings"] == 0
    assert "quality_warnings" not in state["results"][0]
    assert "QUALITY" not in (root / "queue.log").read_text(encoding="utf-8")
    status = runner.invoke(app, ["queue", "status", "--data", str(root)])
    assert status.exit_code == 0
    assert "QUALITY" not in status.output  # There is no work to report.
    # the mark is not lost: it lives where it belongs, inside the snapshot
    assert "# QUALITY:" in md.read_text(encoding="utf-8")


def test_slug_with_dot_resolves_zip_by_appending_suffix(tmp_path):
    root = tmp_path / "data"
    archive = _zip(root / "in" / "course.v1.zip", {"lesson.txt": "text"})
    queue = root / "queue.txt"
    queue.write_text("course.v1\n", encoding="utf-8")

    line = queueing.read_queue(queue)[0]
    assert queueing.resolve_source(line, root, queue) == str(archive.resolve())


def test_queue_plan_cli_is_read_only_with_an_external_queue(tmp_path):
    source = tmp_path / "course"
    source.mkdir()
    (source / "lesson.txt").write_text("x", encoding="utf-8")
    queue = tmp_path / "queue.txt"
    queue.write_text(f"{source}\n", encoding="utf-8")
    missing_data = tmp_path / "missing-data"

    result = runner.invoke(
        app,
        ["queue", "plan", "--data", str(missing_data), "--queue", str(queue)],
    )
    assert result.exit_code == 0, result.output
    assert "Total: 1 items" in result.output
    assert not missing_data.exists()


def test_extracted_slug_keeps_resolving_to_zip_for_resumed_purge(tmp_path):
    root = tmp_path / "data"
    archive = _zip(root / "in" / "packed.zip", {"lesson.txt": "y"})
    archives.extract(archive)
    queue = root / "queue.txt"
    queue.write_text("packed\n", encoding="utf-8")

    line = queueing.read_queue(queue)[0]
    assert queueing.resolve_source(line, root, queue) == str(archive.resolve())


def test_completed_zip_is_refreshed_and_purged_on_later_queue_run(tmp_path, monkeypatch):
    root = tmp_path / "data"
    archive = _zip(root / "in" / "packed.zip", {"lesson.txt": "text"})
    extracted = archives.extract(archive)
    _complete(root / "out", str(extracted), "packed")
    queue = root / "queue.txt"
    queue.write_text("packed\n", encoding="utf-8")
    calls = []

    def invoke(source, data, log, options):
        calls.append(source)
        return 0

    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    counts = queueing.run_queue(
        root,
        queue,
        log_path=root / "logs" / "test.log",
        purge_zip=True,
    )

    assert counts == {"DONE": 0, "FAIL": 0, "SKIP": 1}
    assert calls == [str(extracted)]
    assert not archive.exists()
    assert not archives.marker_path(archive).exists()


def test_queue_status_reports_progress_eta_and_next(tmp_path, monkeypatch):
    root = tmp_path / "data"
    source = str((root / "in" / "current").resolve())
    _complete(root / "out", source, "current")
    queue = root / "queue.txt"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text("current\nnext\n", encoding="utf-8")
    state = {
        "status": "running",
        "current": {
            "index": 1, "entry": "current", "source": source, "stage": "run",
            "started_at": "2026-08-22T00:00:00+00:00", "baseline_done": 0,
            "baseline_done_seconds": 0, "total_seconds": 20,
        },
        "counts": {"DONE": 0, "FAIL": 0, "SKIP": 0},
    }
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "queue-state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(queueing.time, "time", lambda: 1770000000.0)

    payload = queueing.status_payload(root, queue)
    assert payload["progress"]["percent"] == 50.0
    assert payload["progress"]["eta_seconds"] is not None
    assert payload["next"] == ["next"]

    result = runner.invoke(
        app,
        ["queue", "status", "--data", str(root), "--queue", str(queue), "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["progress"]["percent"] == 50.0


def test_queue_status_json_bounds_long_result_history(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    queue = root / "queue.txt"
    queue.write_text("course\n", encoding="utf-8")
    state = {
        "status": "finished",
        "counts": {"DONE": 5000, "FAIL": 0, "SKIP": 0},
        "results": [
            {"entry": f"course-{index:04d}-" + "x" * 40, "status": "DONE"}
            for index in range(5000)
        ],
    }
    (root / "logs").mkdir()
    (root / "logs" / "queue-state.json").write_text(json.dumps(state), encoding="utf-8")

    result = runner.invoke(
        app,
        ["queue", "status", "--data", str(root), "--queue", str(queue), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert len(result.stdout.encode("utf-8")) <= MAX_MACHINE_OUTPUT_BYTES
    payload = json.loads(result.stdout)
    assert payload["state"]["results_total"] == 5000
    assert payload["state"]["results_truncated"] is True
    assert len(payload["state"]["results"]) < 5000


def test_queue_status_calls_running_state_with_dead_lock_interrupted(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "queue-state.json").write_text(
        json.dumps({"status": "running", "counts": {}, "results": []}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["queue", "status", "--data", str(root)])

    assert result.exit_code == 0, result.output
    assert "queue: interrupted" in result.output


def test_doctor_reports_queue_length_and_dead_lock(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "queue.txt").write_text("one\n# comment\ntwo\n", encoding="utf-8")
    (root / "queue.lock").write_text("99999999\n", encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--data", str(root)])
    flat = result.output.replace("\n", "")
    assert "queue: present, 2 lines" in flat
    assert "stale lock" in flat


def test_detach_uses_nohup_new_session_and_prints_pid(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    seen = {}

    class Proc:
        pid = 4321

        def wait(self, timeout):
            seen["timeout"] = timeout
            raise queueing.subprocess.TimeoutExpired("coursedump", timeout)

    def popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return Proc()

    monkeypatch.setattr(queueing.shutil, "which", lambda name: "/usr/bin/nohup")
    monkeypatch.setattr(queueing.subprocess, "Popen", popen)
    result = runner.invoke(app, ["queue", "run", "--data", str(root), "--detach"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith("pid 4321; log ")
    assert seen["cmd"][0] == "/usr/bin/nohup"
    assert seen["kwargs"]["start_new_session"] is True
    assert seen["kwargs"]["stderr"] is queueing.subprocess.STDOUT
    assert seen["kwargs"]["stdout"] is not queueing.subprocess.DEVNULL
    assert seen["timeout"] == queueing.DETACH_START_TIMEOUT


def test_detach_start_failure_is_nonzero_and_kept_in_log(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    (root / "queue.txt").write_text("course\n", encoding="utf-8")

    class Proc:
        pid = 4321

        def wait(self, timeout):
            return 2

    def popen(cmd, **kwargs):
        kwargs["stdout"].write("startup failed\n")
        kwargs["stdout"].flush()
        return Proc()

    monkeypatch.setattr(queueing.shutil, "which", lambda name: "/usr/bin/nohup")
    monkeypatch.setattr(queueing.subprocess, "Popen", popen)
    result = runner.invoke(app, ["queue", "run", "--data", str(root), "--detach"])

    assert result.exit_code == 1
    assert "exited during startup" in result.output
    logs = list((root / "logs").glob("queue-*.log"))
    assert len(logs) == 1
    assert logs[0].read_text(encoding="utf-8") == "startup failed\n"


def test_queue_plan_does_not_resolve_remote_entries(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    queue = root / "queue.txt"
    queue.write_text("https://example.test/course\n", encoding="utf-8")
    monkeypatch.setattr(
        queueing.planning,
        "build",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network resolve")),
    )

    rows = queueing.plan_queue(root, queue)

    assert rows[0]["kind"] == "URL"
    assert rows[0]["estimate_known"] is False
    assert rows[0]["error"] == ""

    result = runner.invoke(
        app,
        ["queue", "plan", "--data", str(root), "--queue", str(queue)],
    )
    assert result.exit_code == 0, result.output
    assert "URL" in result.output
    assert "unknown" in result.output


def test_queue_run_busy_lock_is_exit_zero_for_watchdog(tmp_path):
    """A live lock held by another process means "work in progress": exit 0, not a
    stop code for the scheduler. Regression: exit 1 disabled the periodic job, the
    orphaned queue died at night and nobody picked up the rest of it."""
    root = tmp_path / "data"
    (root / "in").mkdir(parents=True)
    (root / "queue.txt").write_text("slug\n", encoding="utf-8")
    (root / "queue.lock").write_text(f"{os.getpid()}\n", encoding="utf-8")  # our pid is alive
    res = runner.invoke(app, ["queue", "run", "--data", str(root), "--asr", "dummy"])
    assert res.exit_code == 0, res.output
    assert "already running" in res.output and "nothing to do" in res.output
    assert (root / "queue.lock").read_text(encoding="utf-8").strip() == str(os.getpid())


def test_queue_logger_survives_dead_stdout(tmp_path, monkeypatch, capsys):
    """stdout is dead (EPIPE after the reader died): the line still reaches the
    file, the queue keeps running and the echo switches off. Regression: printing
    before writing to the file killed the queue, and the DONE line never even made
    it into the log."""
    import io

    class DeadPipe(io.TextIOBase):
        def write(self, _):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    log = tmp_path / "q.log"
    logger = queueing.QueueLogger(log)
    monkeypatch.setattr("sys.stdout", DeadPipe())
    logger("DONE first")       # does not raise
    logger("DONE second")      # echo already off, stdout is left alone
    lines = log.read_text(encoding="utf-8").splitlines()
    assert [l.split("] ", 1)[1] for l in lines] == ["DONE first", "DONE second"]
    assert logger.echo is False


def test_event_hook_uses_shell_and_complete_environment(tmp_path, monkeypatch):
    calls = _capture_hooks(monkeypatch)
    log = tmp_path / "queue.log"

    (tmp_path / "catalog.tsv").write_text(
        "slug\ttitle\turl\ncourse\tНазвание курса\t-\nother\t-\t-\n", encoding="utf-8"
    )

    queueing._event(
        "notify $COURSEDUMP_EVENT",
        queueing.QueueLogger(log),
        tmp_path,
        "course_done",
        kind="progress",
        subject="course",
        title="asr done",
        lines=("first line", "second line"),
        entry="course",
    )

    assert calls[0]["cmd"] == ["/bin/sh", "-c", "notify $COURSEDUMP_EVENT"]
    assert calls[0]["kwargs"]["timeout"] == 30
    env = calls[0]["env"]
    # the old env contract stays exactly as it was
    assert env["COURSEDUMP_EVENT"] == "course_done"
    assert env["COURSEDUMP_KIND"] == "progress"
    assert env["COURSEDUMP_SUBJECT"] == "course"
    assert env["COURSEDUMP_TITLE"] == "asr done"
    assert env["COURSEDUMP_LINES"] == "first line\nsecond line"
    assert env["COURSEDUMP_TEXT"] == "course: asr done; first line; second line"
    assert env["COURSEDUMP_ENTRY"] == "course"
    assert env["COURSEDUMP_DATA"] == str(tmp_path)
    # the v2 envelope: from/about/name/class/action
    assert env["COURSEDUMP_FROM"] == "coursedump"
    assert env["COURSEDUMP_ABOUT"] == "course"
    assert env["COURSEDUMP_NAME"] == "Название курса"
    assert env["COURSEDUMP_CLASS"] == "event"
    assert env["COURSEDUMP_ACTION"] == ""
    assert "hook: course_done ok 0" in log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "kind, expected_class",
    [("progress", "event"), ("done", "event"), ("fail", "alert"), ("info", "ack"),
     ("unknown", "event")],
)
def test_event_hook_maps_kind_to_class_and_extracts_action(
    tmp_path, monkeypatch, kind, expected_class
):
    calls = _capture_hooks(monkeypatch)
    (tmp_path / "catalog.tsv").write_text(
        "slug\ttitle\nqueue\t-\n", encoding="utf-8"
    )

    queueing._event(
        "notify",
        queueing.QueueLogger(tmp_path / "q.log"),
        tmp_path,
        "queue_failed",
        kind=kind,
        subject="queue",
        title="queue failed on course",
        lines=("engine failed", "What to do: coursedump queue run"),
        entry="course",
    )
    env = calls[0]["env"]

    assert env["COURSEDUMP_CLASS"] == expected_class
    assert env["COURSEDUMP_ABOUT"] == "queue"
    assert env["COURSEDUMP_NAME"] == ""  # "-" in the registry means no title
    assert env["COURSEDUMP_ACTION"] == "coursedump queue run"
    assert env["COURSEDUMP_LINES"].splitlines()[1] == "What to do: coursedump queue run"


def test_hook_timeout_is_logged_and_does_not_break_queue(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)

    def invoke(source, data, log, options):
        _complete(root / "out", source, "course")
        return 0

    def timeout(cmd, **kwargs):
        raise queueing.subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    monkeypatch.setattr(queueing.subprocess, "run", timeout)
    log = root / "logs" / "test.log"

    counts = queueing.run_queue(root, root / "queue.txt", log_path=log)

    assert counts == {"DONE": 1, "FAIL": 0, "SKIP": 0}
    assert "hook: queue_started fail timeout" in log.read_text(encoding="utf-8")
    assert "hook: queue_finished fail timeout" in log.read_text(encoding="utf-8")


def test_hook_nonzero_is_logged_and_does_not_break_queue_guard(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)
    _capture_hooks(monkeypatch, returncode=17)

    def invoke(source, data, log, options):
        _complete(root / "out", source, "course")
        return 0

    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    log = root / "logs" / "test.log"

    counts = queueing.run_queue(root, root / "queue.txt", log_path=log)

    assert counts == {"DONE": 1, "FAIL": 0, "SKIP": 0}
    text = log.read_text(encoding="utf-8")
    assert "hook: queue_started fail 17" in text
    assert "result: DONE=1 FAIL=0 SKIP=0" in text


def test_config_rejects_unknown_fields_fail_closed(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    (root / "config.toml").write_text(
        'on_event = "notify"\nunexpected = true\n', encoding="utf-8"
    )

    with pytest.raises(queueing.QueueError, match="unknown fields.*unexpected"):
        queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")

    assert not (root / "logs" / "queue-state.json").exists()


def test_config_rejects_non_string_on_event_fail_closed(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    (root / "config.toml").write_text("on_event = 42\n", encoding="utf-8")

    with pytest.raises(queueing.QueueError, match="on_event.*must be a string"):
        queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")


def test_success_events_have_all_required_texts(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)
    monkeypatch.setattr(
        queueing,
        "_estimate",
        lambda source, options: {"media_duration_seconds": 3600, "work_items": 2},
    )

    def invoke(source, data, log, options):
        _complete(root / "out", source, "course")
        return 0

    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    counts = queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
    events = [(call["env"]["COURSEDUMP_EVENT"], call["env"]["COURSEDUMP_TEXT"])
              for call in calls]

    assert counts == {"DONE": 1, "FAIL": 0, "SKIP": 0}
    assert [event for event, _ in events] == [
        "queue_started", "course_started", "course_done", "queue_finished"
    ]
    assert calls[0]["env"]["COURSEDUMP_LINES"] == (
        "items 1/1 to do\nmedia ~1.0h · eta ~5m"
    )
    assert calls[1]["env"]["COURSEDUMP_LINES"] == (
        "lessons 2 · media ~1.0h · eta ~5m\n"
        "queue after: 0"
    )
    assert calls[2]["env"]["COURSEDUMP_LINES"].startswith("lessons 1/1 · ")
    assert "queue: 0 left · media ~0.0h · eta ~0m" in calls[2]["env"][
        "COURSEDUMP_LINES"
    ]
    assert calls[3]["env"]["COURSEDUMP_LINES"].startswith(
        "done 1 · fail 0 · skip 0 · "
    )
    assert calls[3]["env"]["COURSEDUMP_LINES"].endswith(f"out: {root / 'out'}")
    assert calls[1]["env"]["COURSEDUMP_ENTRY"] == "course"


def test_slow_estimate_refreshes_queue_start_only_after_five_seconds(
    tmp_path, monkeypatch
):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)
    sleeps = []

    class FiredTimer:
        daemon = False

        def __init__(self, _delay, callback, args=(), kwargs=None):
            self.callback = callback
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.callback(*self.args, **self.kwargs)

        def cancel(self):
            pass

        def join(self):
            pass

    ticks = iter([0.0, 0.0, 6.0, 6.0, 7.0, 7.0])
    monkeypatch.setattr(queueing.threading, "Timer", FiredTimer)
    monkeypatch.setattr(queueing.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(queueing, "_sleep", sleeps.append)
    monkeypatch.setattr(
        queueing,
        "_estimate",
        lambda *args: {"media_duration_seconds": 60, "work_items": 1},
    )

    def invoke(source, data, child_log, options):
        _complete(root / "out", source, "course")
        return 0

    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")

    starts = [
        call["env"] for call in calls
        if call["env"]["COURSEDUMP_EVENT"] == "queue_started"
    ]
    assert len(starts) == 2
    assert "media ? (estimating) · eta ?" in starts[0]["COURSEDUMP_LINES"]
    assert "media ~0.0h · eta ~0m" in starts[1]["COURSEDUMP_LINES"]
    assert sleeps == []


def test_done_and_next_start_are_separate_events_addressed_to_each_course(
    tmp_path, monkeypatch
):
    # In the v2 envelope about is the course slug, so "one is done" and "the next
    # one started" are two envelopes, not a single running "queue" message.
    root = tmp_path / "data"
    for name in ("one", "two"):
        (root / "in" / name).mkdir(parents=True)
    (root / "queue.txt").write_text("one\ntwo\n", encoding="utf-8")
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)

    def estimate(source, options):
        seconds = 3600 if Path(source).name == "one" else 7200
        return {"media_duration_seconds": seconds, "work_items": 1}

    def invoke(source, data, child_log, options):
        _complete(root / "out", source, Path(source).name)
        return 0

    ticks = iter([0.0, 0.0, 10.0, 10.0, 20.0, 20.0])
    monkeypatch.setattr(queueing.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(queueing, "_estimate", estimate)
    monkeypatch.setattr(queueing, "_invoke_run", invoke)

    queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
    events = [call["env"] for call in calls]

    assert [(event["COURSEDUMP_EVENT"], event["COURSEDUMP_ABOUT"]) for event in events] == [
        ("queue_started", "queue"),
        ("course_started", "one"),
        ("course_done", "one"),
        ("course_started", "two"),
        ("course_done", "two"),
        ("queue_finished", "queue"),
    ]
    done_one = events[2]
    assert done_one["COURSEDUMP_TITLE"] == "asr done"
    assert done_one["COURSEDUMP_LINES"].splitlines() == [
        "lessons 1/1 · 0m",
        "queue: 1 left · media ~2.0h · eta ~0m",
    ]
    start_two = events[3]
    assert start_two["COURSEDUMP_TITLE"] == "asr start"
    assert start_two["COURSEDUMP_LINES"].splitlines() == [
        "lessons 1 · media ~2.0h · eta ~0m",
        "queue after: 0",
    ]


def _one_course_structured_events(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)
    monkeypatch.setattr(
        queueing,
        "_estimate",
        lambda *args: {"media_duration_seconds": 3600, "work_items": 2},
    )

    def invoke(source, data, log, options):
        _complete(root / "out", source, "course")
        return 0

    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    counts = queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
    return root, counts, [call["env"] for call in calls]


def test_queue_started_event_has_canonical_progress_fields(tmp_path, monkeypatch):
    _, _, events = _one_course_structured_events(tmp_path, monkeypatch)
    started = [event for event in events if event["COURSEDUMP_EVENT"] == "queue_started"][-1]

    assert started["COURSEDUMP_KIND"] == "progress"
    assert started["COURSEDUMP_CLASS"] == "event"
    assert started["COURSEDUMP_SUBJECT"] == started["COURSEDUMP_ABOUT"] == "queue"
    assert started["COURSEDUMP_TITLE"] == "queue start"
    assert started["COURSEDUMP_LINES"] == "items 1/1 to do\nmedia ~1.0h · eta ~5m"


def test_course_started_event_is_addressed_to_the_course(tmp_path, monkeypatch):
    _, _, events = _one_course_structured_events(tmp_path, monkeypatch)
    started = next(event for event in events if event["COURSEDUMP_EVENT"] == "course_started")

    assert started["COURSEDUMP_KIND"] == "progress"
    assert started["COURSEDUMP_CLASS"] == "event"
    assert started["COURSEDUMP_SUBJECT"] == started["COURSEDUMP_ABOUT"] == "course"
    assert started["COURSEDUMP_TITLE"] == "asr start"
    assert started["COURSEDUMP_LINES"] == (
        "lessons 2 · media ~1.0h · eta ~5m\n"
        "queue after: 0"
    )


def test_course_done_success_is_addressed_to_the_course(tmp_path, monkeypatch):
    _, counts, events = _one_course_structured_events(tmp_path, monkeypatch)
    done = next(event for event in events if event["COURSEDUMP_EVENT"] == "course_done")

    assert counts == {"DONE": 1, "FAIL": 0, "SKIP": 0}
    assert done["COURSEDUMP_KIND"] == "progress"
    assert done["COURSEDUMP_CLASS"] == "event"
    assert done["COURSEDUMP_SUBJECT"] == done["COURSEDUMP_ABOUT"] == "course"
    assert done["COURSEDUMP_TITLE"] == "asr done"
    assert done["COURSEDUMP_LINES"].splitlines() == [
        "lessons 1/1 · 0m",
        "queue: 0 left · media ~0.0h · eta ~0m",
    ]


def test_queue_finished_event_has_canonical_done_fields(tmp_path, monkeypatch):
    root, _, events = _one_course_structured_events(tmp_path, monkeypatch)
    finished = next(
        event for event in events if event["COURSEDUMP_EVENT"] == "queue_finished"
    )

    assert finished["COURSEDUMP_KIND"] == "done"
    assert finished["COURSEDUMP_CLASS"] == "event"
    assert finished["COURSEDUMP_SUBJECT"] == finished["COURSEDUMP_ABOUT"] == "queue"
    assert finished["COURSEDUMP_TITLE"] == "queue done"
    assert finished["COURSEDUMP_LINES"].splitlines() == [
        "done 1 · fail 0 · skip 0 · 0m",
        f"out: {root / 'out'}",
    ]


def test_course_done_failure_has_reason_log_and_continuation(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)
    monkeypatch.setattr(queueing, "_invoke_run", lambda *args: 75)

    queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
    failed = next(
        call["env"] for call in calls if call["env"]["COURSEDUMP_EVENT"] == "course_done"
    )

    assert failed["COURSEDUMP_KIND"] == "fail"
    assert failed["COURSEDUMP_CLASS"] == "alert"
    assert failed["COURSEDUMP_SUBJECT"] == failed["COURSEDUMP_ABOUT"] == "course"
    assert failed["COURSEDUMP_TITLE"] == "fail"
    assert failed["COURSEDUMP_LINES"].splitlines() == [
        "insufficient space (exit 75)",
        f"log: {root / 'q.log'}",
        "queue: 0 left",
    ]
    assert failed["COURSEDUMP_ACTION"] == ""


def test_queue_failed_event_has_reason_and_restart_command(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)

    def explode(*args):
        raise RuntimeError("engine failed")

    monkeypatch.setattr(queueing, "_invoke_run", explode)
    with pytest.raises(RuntimeError, match="engine failed"):
        queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
    failed = next(
        call["env"] for call in calls if call["env"]["COURSEDUMP_EVENT"] == "queue_failed"
    )

    assert failed["COURSEDUMP_KIND"] == "fail"
    assert failed["COURSEDUMP_CLASS"] == "alert"
    assert failed["COURSEDUMP_SUBJECT"] == failed["COURSEDUMP_ABOUT"] == "queue"
    assert failed["COURSEDUMP_TITLE"] == "queue failed on course"
    assert failed["COURSEDUMP_LINES"] == (
        "engine failed\nWhat to do: coursedump queue run"
    )
    assert failed["COURSEDUMP_ACTION"] == "coursedump queue run"


def test_structured_event_bounds_subject_title_and_three_lines(tmp_path, monkeypatch):
    calls = _capture_hooks(monkeypatch)

    # Two-byte filler on purpose: truncation counted in bytes would cut a
    # character in half here.
    queueing._event(
        "notify",
        queueing.QueueLogger(tmp_path / "q.log"),
        tmp_path,
        "course_done",
        kind="fail",
        subject="с" * 80,
        title="fail\nwith tail",
        lines=("а" * 250, "two\nlines", "third", "extra"),
        entry="course",
    )
    env = calls[0]["env"]

    assert len(env["COURSEDUMP_SUBJECT"]) == queueing.HOOK_SUBJECT_LIMIT == 40
    assert env["COURSEDUMP_TITLE"] == "fail with tail"
    assert env["COURSEDUMP_LINES"].splitlines() == [
        "а" * queueing.HOOK_LINE_LIMIT,
        "two lines",
        "third",
    ]
    assert "\n" not in env["COURSEDUMP_TEXT"]


def test_course_fail_event_is_one_line_and_queue_continues(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)
    monkeypatch.setattr(queueing, "_invoke_run", lambda *args: 75)

    counts = queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
    done = next(call["env"] for call in calls
                if call["env"]["COURSEDUMP_EVENT"] == "course_done")
    state = json.loads((root / "logs" / "queue-state.json").read_text(encoding="utf-8"))

    assert counts == {"DONE": 0, "FAIL": 1, "SKIP": 0}
    assert done["COURSEDUMP_KIND"] == "fail"
    assert done["COURSEDUMP_SUBJECT"] == "course"
    assert done["COURSEDUMP_TITLE"] == "fail"
    assert done["COURSEDUMP_LINES"] == (
        f"insufficient space (exit 75)\nlog: {root / 'q.log'}\n"
        "queue: 0 left"
    )
    assert "\n" not in done["COURSEDUMP_TEXT"]
    assert state["results"][0]["elapsed"] >= 0


def test_repeated_failure_is_not_a_new_exit_or_alert_and_done_resets_it(
    tmp_path, monkeypatch
):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)
    hooks = _capture_hooks(monkeypatch)
    outcome = {"error": "table detection is broken", "done": False}

    def invoke(source, _data, child_log, _options):
        if outcome["done"]:
            _complete(root / "out", source, "course")
            return 0
        with child_log.open("a", encoding="utf-8") as output:
            output.write(str(outcome["error"]) + "\n")
        return 1

    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    argv = [
        "queue", "run", "--data", str(root), "--asr", "dummy",
        "--log", str(root / "q.log"),
    ]

    def tick():
        hook_start = len(hooks)
        result = runner.invoke(app, argv)
        state = json.loads(
            (root / "logs" / "queue-state.json").read_text(encoding="utf-8")
        )
        return result, state, hooks[hook_start:]

    first, state, first_hooks = tick()
    assert first.exit_code == 1, first.output
    assert state["new_failures"] == 1
    assert state["results"][-1]["status"] == "FAIL"
    assert any(call["env"]["COURSEDUMP_CLASS"] == "alert" for call in first_hooks)

    second, state, second_hooks = tick()
    assert second.exit_code == 0, second.output
    assert state["new_failures"] == 0
    assert state["results"][-1]["status"] == "FAIL"
    assert state["results"][-1]["repeat"] is True
    assert "(repeat)" in (root / "q.log").read_text(encoding="utf-8")
    assert all(call["env"]["COURSEDUMP_CLASS"] != "alert" for call in second_hooks)

    outcome["error"] = "a different error"
    third, state, third_hooks = tick()
    assert third.exit_code == 1, third.output
    assert state["new_failures"] == 1
    assert "repeat" not in state["results"][-1]
    assert any(call["env"]["COURSEDUMP_CLASS"] == "alert" for call in third_hooks)

    outcome["done"] = True
    done, state, _done_hooks = tick()
    assert done.exit_code == 0, done.output
    assert state["results"][-1]["status"] == "DONE"

    outcome["done"] = False
    after_done, state, after_done_hooks = tick()
    assert after_done.exit_code == 1, after_done.output
    assert state["new_failures"] == 1
    assert "repeat" not in state["results"][-1]
    assert any(
        call["env"]["COURSEDUMP_CLASS"] == "alert" for call in after_done_hooks
    )


def test_same_failure_after_snapshot_change_is_new(tmp_path, monkeypatch):
    root = tmp_path / "data"
    source = str((root / "in" / "course").resolve())
    (root / "in" / "course").mkdir(parents=True)
    _complete(root / "out", source, "course")
    (root / "queue.txt").write_text("course\n", encoding="utf-8")

    def invoke(_source, _data, child_log, _options):
        with child_log.open("a", encoding="utf-8") as output:
            output.write("the same error again\n")
        return 1

    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    argv = ["queue", "run", "--data", str(root), "--asr", "dummy"]
    first = runner.invoke(app, argv)
    assert first.exit_code == 1, first.output

    (root / "out" / "course" / "text" / "lesson.txt.md").unlink()
    second = runner.invoke(app, argv)
    state = json.loads(
        (root / "logs" / "queue-state.json").read_text(encoding="utf-8")
    )

    assert second.exit_code == 1, second.output
    assert state["new_failures"] == 1
    assert "repeat" not in state["results"][-1]


def test_skip_sends_no_per_course_events(tmp_path, monkeypatch):
    root = tmp_path / "data"
    source = str((root / "in" / "course").resolve())
    (root / "in" / "course").mkdir(parents=True)
    _complete(root / "out", source, "course")
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)
    monkeypatch.setattr(queueing, "_invoke_run", lambda *args: 0)

    counts = queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
    events = [call["env"]["COURSEDUMP_EVENT"] for call in calls]

    assert counts == {"DONE": 0, "FAIL": 0, "SKIP": 1}
    assert events == []


def test_restart_announces_previous_interruption_in_log_and_started_event(
    tmp_path, monkeypatch
):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "queue-state.json").write_text(
        json.dumps({
            "status": "running",
            "current": {"entry": "old-course", "stage": "verify"},
        }),
        encoding="utf-8",
    )
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)

    def invoke(source, data, log, options):
        _complete(root / "out", source, "course")
        return 0

    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    log = root / "logs" / "test.log"
    queueing.run_queue(root, root / "queue.txt", log_path=log)

    assert "RESUME continuing after interruption at old-course" in log.read_text(encoding="utf-8")
    assert "resumed after interruption on old-course" in calls[0]["env"]["COURSEDUMP_TEXT"]


def test_unhandled_exception_writes_failed_state_and_queue_failed_event(
    tmp_path, monkeypatch
):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)

    def explode(*args):
        raise RuntimeError("first line\nsecond line")

    monkeypatch.setattr(queueing, "_invoke_run", explode)
    with pytest.raises(RuntimeError, match="first line"):
        queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")

    state = json.loads((root / "logs" / "queue-state.json").read_text(encoding="utf-8"))
    failed = next(call["env"] for call in calls
                  if call["env"]["COURSEDUMP_EVENT"] == "queue_failed")
    assert state["status"] == "failed"
    assert state["current"]["entry"] == "course"
    assert failed["COURSEDUMP_ENTRY"] == "course"
    assert failed["COURSEDUMP_KIND"] == "fail"
    assert failed["COURSEDUMP_SUBJECT"] == "queue"
    assert failed["COURSEDUMP_TITLE"] == "queue failed on course"
    assert failed["COURSEDUMP_LINES"] == (
        "first line second line\nWhat to do: coursedump queue run"
    )
    assert not (root / "queue.lock").exists()


def test_state_starts_with_whole_queue_eta_and_default_mlx_speed(tmp_path, monkeypatch):
    root = tmp_path / "data"
    for name in ("one", "two"):
        (root / "in" / name).mkdir(parents=True)
    (root / "queue.txt").write_text("one\ntwo\n", encoding="utf-8")

    def estimate(source, options):
        seconds = 3600 if Path(source).name == "one" else 7200
        return {"media_duration_seconds": seconds, "work_items": 1}

    seen = []

    def invoke(source, data, log, options):
        seen.append(json.loads(
            (root / "logs" / "queue-state.json").read_text(encoding="utf-8")
        ))
        _complete(root / "out", source, Path(source).name)
        return 0

    monkeypatch.setattr(queueing, "_estimate", estimate)
    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")

    assert seen[0]["remaining_media_seconds"] == 10800
    assert seen[0]["speed_ratio"] == 12.0
    assert seen[0]["queue_eta_seconds"] == 900
    assert seen[1]["remaining_media_seconds"] == 7200


def test_done_result_has_elapsed_and_updates_measured_speed(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    monkeypatch.setattr(
        queueing,
        "_estimate",
        lambda source, options: {"media_duration_seconds": 240, "work_items": 1},
    )

    def invoke(source, data, log, options):
        _complete(root / "out", source, "course")
        return 0

    ticks = iter([0.0, 0.0, 10.0, 10.0])
    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    monkeypatch.setattr(queueing.time, "monotonic", lambda: next(ticks))
    queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
    state = json.loads((root / "logs" / "queue-state.json").read_text(encoding="utf-8"))

    assert state["results"] == [{"entry": "course", "status": "DONE", "elapsed": 10.0}]
    assert state["speed_ratio"] == 24.0
    assert state["speed_samples"] == [{"media_seconds": 240.0, "elapsed": 10.0}]


def test_speed_ratio_uses_only_last_course_window():
    state = {"remaining_media_seconds": 1000, "speed_ratio": 12.0, "speed_samples": []}
    queueing._record_speed(state, 30, 3)
    queueing._record_speed(state, 80, 4)
    queueing._record_speed(state, 90, 3)
    queueing._record_speed(state, 400, 10)

    assert len(state["speed_samples"]) == queueing.SPEED_WINDOW == 3
    assert state["speed_ratio"] == pytest.approx((80 + 90 + 400) / (4 + 3 + 10))


def test_queue_status_json_and_human_show_whole_eta_and_speed(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "queue.txt").write_text("one\ntwo\n", encoding="utf-8")
    (root / "queue.lock").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "queue-state.json").write_text(
        json.dumps({
            "status": "running",
            "pid": os.getpid(),
            "current": None,
            "counts": {"DONE": 0, "FAIL": 0, "SKIP": 0},
            "remaining": 2,
            "remaining_media_seconds": 7200,
            "speed_ratio": 8,
        }),
        encoding="utf-8",
    )

    machine = runner.invoke(app, ["queue", "status", "--data", str(root), "--json"])
    human = runner.invoke(app, ["queue", "status", "--data", str(root)])
    payload = json.loads(machine.stdout)

    assert machine.exit_code == 0 and human.exit_code == 0
    assert payload["queue_eta_seconds"] == 900
    assert payload["speed_ratio"] == 8
    assert (
        "queue: 2 courses remaining, ~2.0 h media, ETA ~15:00 (speed 8.0x)"
        in human.output
    )


def test_queue_status_dead_pid_names_course_stage_and_resume_hint(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    # Even an unrelated live lock does not revive a dead PID recorded in state.
    (root / "queue.lock").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "queue-state.json").write_text(
        json.dumps({
            "status": "running",
            "pid": 99999999,
            "current": {
                "index": 1,
                "entry": "course",
                "source": str(root / "in" / "course"),
                "stage": "verify",
            },
            "counts": {},
            "remaining": 1,
            "remaining_media_seconds": 600,
            "speed_ratio": 12,
        }),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["queue", "status", "--data", str(root)])

    assert result.exit_code == 0, result.output
    assert "interrupted (process died on course, stage verify)" in result.output
    assert "queue run resumes" in result.output


def test_queue_status_subtracts_current_course_progress_from_whole_eta(tmp_path):
    root = tmp_path / "data"
    source = str((root / "in" / "course").resolve())
    _complete(root / "out", source, "course")
    (root / "queue.txt").write_text("course\nnext\n", encoding="utf-8")
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / "queue-state.json").write_text(
        json.dumps({
            "status": "running",
            "current": {
                "index": 1,
                "entry": "course",
                "source": source,
                "stage": "run",
                "baseline_done_seconds": 0,
                "total_seconds": 20,
            },
            "remaining": 2,
            "remaining_media_seconds": 110,
            "speed_ratio": 10,
            "counts": {},
        }),
        encoding="utf-8",
    )

    payload = queueing.status_payload(root, root / "queue.txt")

    assert payload["progress"]["done_seconds"] == 10
    assert payload["remaining_media_seconds"] == 100
    assert payload["queue_eta_seconds"] == 10


def test_estimate_state_is_visible_and_fast_start_is_debounced_to_eta(
    tmp_path, monkeypatch
):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)
    log = root / "logs" / "test.log"
    observed = {}

    def estimate(source, options):
        observed["state"] = json.loads(
            (root / "logs" / "queue-state.json").read_text(encoding="utf-8")
        )
        observed["events"] = [
            call["env"]["COURSEDUMP_EVENT"] for call in calls
        ]
        observed["started_text"] = (
            calls[0]["env"]["COURSEDUMP_TEXT"] if calls else ""
        )
        observed["first_log"] = log.read_text(encoding="utf-8").splitlines()[0]
        return {"media_duration_seconds": 60, "work_items": 1}

    def invoke(source, data, child_log, options):
        _complete(root / "out", source, "course")
        return 0

    monkeypatch.setattr(queueing, "_estimate", estimate)
    monkeypatch.setattr(queueing, "_invoke_run", invoke)

    assert queueing.run_queue(root, root / "queue.txt", log_path=log)["DONE"] == 1
    assert observed["state"]["status"] == "running"
    assert observed["state"]["pid"] == os.getpid()
    assert observed["state"]["current"]["entry"] == "course"
    assert observed["state"]["current"]["stage"] == "estimate"
    assert observed["state"]["current"]["estimate_index"] == 1
    assert "index" not in observed["state"]["current"]
    assert observed["events"] == []
    assert observed["started_text"] == ""
    assert calls[0]["env"]["COURSEDUMP_EVENT"] == "queue_started"
    assert "media ~0.0h · eta ~0m" in calls[0]["env"]["COURSEDUMP_LINES"]
    assert "queue run: 1 items" in observed["first_log"]


def test_status_next_stays_at_queue_start_during_estimate(tmp_path, monkeypatch):
    root = tmp_path / "data"
    for name in ("one", "two"):
        (root / "in" / name).mkdir(parents=True)
    queue = root / "queue.txt"
    queue.write_text("one\ntwo\n", encoding="utf-8")
    observed = {}

    def estimate(source, options):
        if not observed:
            observed.update(queueing.status_payload(root, queue))
        return {"media_duration_seconds": 60, "work_items": 1}

    def invoke(source, data, child_log, options):
        _complete(root / "out", source, Path(source).name)
        return 0

    monkeypatch.setattr(queueing, "_estimate", estimate)
    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    queueing.run_queue(root, queue, log_path=root / "q.log")

    assert observed["state"]["current"]["stage"] == "estimate"
    assert observed["state"]["current"]["estimate_index"] == 1
    assert "index" not in observed["state"]["current"]
    assert observed["next"] == ["one", "two"]


def test_estimate_failure_is_unknown_logged_and_does_not_stop_next_course(
    tmp_path, monkeypatch
):
    root = tmp_path / "data"
    for name in ("bad-estimate", "good"):
        (root / "in" / name).mkdir(parents=True)
    (root / "queue.txt").write_text("bad-estimate\ngood\n", encoding="utf-8")
    seen = []

    def estimate(source, options):
        if Path(source).name == "bad-estimate":
            raise RuntimeError("estimator crashed")
        return {"media_duration_seconds": 120, "work_items": 1}

    def invoke(source, data, child_log, options):
        seen.append(Path(source).name)
        if Path(source).name == "bad-estimate":
            state = json.loads(
                (root / "logs" / "queue-state.json").read_text(encoding="utf-8")
            )
            assert state["eta_known"] is False
            assert state["remaining_unknown_estimates"] == 1
        _complete(root / "out", source, Path(source).name)
        return 0

    monkeypatch.setattr(queueing, "_estimate", estimate)
    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    log = root / "logs" / "test.log"

    counts = queueing.run_queue(root, root / "queue.txt", log_path=log)

    assert counts == {"DONE": 2, "FAIL": 0, "SKIP": 0}
    assert seen == ["bad-estimate", "good"]
    assert "ESTIMATE bad-estimate: unknown - estimator crashed" in log.read_text(
        encoding="utf-8"
    )


def test_human_status_marks_unknown_estimate_instead_of_zero_eta(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "queue-state.json").write_text(
        json.dumps(
            {
                "status": "running",
                "current": {"stage": "preflight"},
                "counts": {},
                "remaining": 1,
                "remaining_media_seconds": 3600,
                "remaining_unknown_estimates": 1,
                "speed_ratio": 12,
                "eta_known": False,
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["queue", "status", "--data", str(root)])

    assert result.exit_code == 0, result.output
    assert "~1.0 h media (+1 courses without estimates)" in result.output
    assert "ETA unknown" in result.output


def test_completed_course_is_estimated_fresh_before_confirming_skip(
    tmp_path, monkeypatch
):
    root = tmp_path / "data"
    source = str((root / "in" / "course").resolve())
    (root / "in" / "course").mkdir(parents=True)
    _complete(root / "out", source, "course")
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    estimated = []

    def estimate(value, options):
        estimated.append(value)
        return {"media_duration_seconds": 10, "work_items": 1}

    monkeypatch.setattr(queueing, "_estimate", estimate)
    monkeypatch.setattr(queueing, "_invoke_run", lambda *args: 0)

    counts = queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")

    assert counts == {"DONE": 0, "FAIL": 0, "SKIP": 1}
    assert estimated == [source]


def test_predicted_skip_that_grows_announces_update_and_late_queue_start(
    tmp_path, monkeypatch
):
    root = tmp_path / "data"
    source = str((root / "in" / "course").resolve())
    (root / "in" / "course").mkdir(parents=True)
    _complete(root / "out", source, "course")
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)
    monkeypatch.setattr(
        queueing,
        "_estimate",
        lambda *args: {"media_duration_seconds": 20, "work_items": 2},
    )

    def invoke(value, data, child_log, options):
        assert [call["env"]["COURSEDUMP_EVENT"] for call in calls] == [
            "queue_started",
            "course_started",
        ]
        state = json.loads(
            (root / "logs" / "queue-state.json").read_text(encoding="utf-8")
        )
        assert state["remaining_media_seconds"] == 10
        course = root / "out" / "course"
        manifest.save(
            [
                manifest.Item(rel="lesson.txt", kind="text", target="lesson.txt.md"),
                manifest.Item(rel="new.txt", kind="text", target="new.txt.md"),
            ],
            course / "manifest.jsonl",
        )
        (course / "text" / "new.txt.md").write_text(
            "---\nduration: 10\n---\n# new\n", encoding="utf-8"
        )
        return 0

    monkeypatch.setattr(queueing, "_invoke_run", invoke)

    counts = queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
    events = [call["env"] for call in calls]

    assert counts == {"DONE": 1, "FAIL": 0, "SKIP": 0}
    assert [event["COURSEDUMP_EVENT"] for event in events] == [
        "queue_started",
        "course_started",
        "course_done",
        "queue_finished",
    ]
    assert events[0]["COURSEDUMP_TITLE"] == "queue start"
    assert "items 1/1 to do" in events[0]["COURSEDUMP_LINES"]
    assert events[1]["COURSEDUMP_TITLE"] == "asr start"
    assert "updated: +1 lessons" in events[2]["COURSEDUMP_LINES"]


def test_course_done_hook_sees_already_accounted_queue_eta(tmp_path, monkeypatch):
    root = tmp_path / "data"
    for name in ("one", "two"):
        (root / "in" / name).mkdir(parents=True)
    queue = root / "queue.txt"
    queue.write_text("one\ntwo\n", encoding="utf-8")
    _hook_config(root)
    during_hooks = []

    def hook(cmd, **kwargs):
        if kwargs["env"]["COURSEDUMP_EVENT"] == "course_done":
            during_hooks.append(queueing.status_payload(root, queue))
        return SimpleNamespace(returncode=0)

    def invoke(source, data, child_log, options):
        _complete(root / "out", source, Path(source).name)
        return 0

    monkeypatch.setattr(queueing.subprocess, "run", hook)
    monkeypatch.setattr(
        queueing,
        "_estimate",
        lambda *args: {"media_duration_seconds": 100, "work_items": 1},
    )
    monkeypatch.setattr(queueing, "_invoke_run", invoke)

    queueing.run_queue(root, queue, log_path=root / "q.log")

    assert during_hooks[0]["remaining_media_seconds"] == 100
    assert during_hooks[0]["state"]["remaining_media_seconds"] == 100
    assert during_hooks[1]["remaining_media_seconds"] == 0


def test_all_skip_multiple_courses_emits_no_hook_events(tmp_path, monkeypatch):
    root = tmp_path / "data"
    for name in ("one", "two"):
        source = str((root / "in" / name).resolve())
        (root / "in" / name).mkdir(parents=True)
        _complete(root / "out", source, name)
    (root / "queue.txt").write_text("one\ntwo\n", encoding="utf-8")
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)
    monkeypatch.setattr(queueing, "_invoke_run", lambda *args: 0)

    counts = queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")

    assert counts == {"DONE": 0, "FAIL": 0, "SKIP": 2}
    assert calls == []


@pytest.mark.parametrize("previous_status", ["running", "failed"])
def test_resume_all_skip_always_closes_previous_live_message(
    tmp_path, monkeypatch, previous_status
):
    root = tmp_path / "data"
    source = str((root / "in" / "course").resolve())
    (root / "in" / "course").mkdir(parents=True)
    _complete(root / "out", source, "course")
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "queue-state.json").write_text(
        json.dumps({
            "status": previous_status,
            "current": {"entry": "course", "stage": "run"},
        }),
        encoding="utf-8",
    )
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)
    monkeypatch.setattr(queueing, "_invoke_run", lambda *args: 0)

    counts = queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
    events = [call["env"] for call in calls]

    assert counts == {"DONE": 0, "FAIL": 0, "SKIP": 1}
    assert [event["COURSEDUMP_EVENT"] for event in events] == ["queue_finished"]
    assert events[0]["COURSEDUMP_KIND"] == "done"
    assert events[0]["COURSEDUMP_LINES"].splitlines() == [
        "resumed after interruption: nothing left to do",
        "done 0 · fail 0 · skip 1 · 0m",
        f"out: {root / 'out'}",
    ]


def test_predicted_skip_failure_still_emits_queue_and_course_events(
    tmp_path, monkeypatch
):
    root = tmp_path / "data"
    source = str((root / "in" / "course").resolve())
    (root / "in" / "course").mkdir(parents=True)
    _complete(root / "out", source, "course")
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)
    monkeypatch.setattr(queueing, "_invoke_run", lambda *args: 1)

    counts = queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
    events = [call["env"] for call in calls]

    assert counts == {"DONE": 0, "FAIL": 1, "SKIP": 0}
    assert [event["COURSEDUMP_EVENT"] for event in events] == [
        "queue_started",
        "course_done",
        "queue_finished",
    ]
    assert events[0]["COURSEDUMP_TITLE"] == "queue start"


def test_child_failure_uses_last_nonempty_run_log_line(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)

    def invoke(source, data, child_log, options):
        with child_log.open("a", encoding="utf-8") as output:
            output.write("not this line\n\nexact reason from the child run\n")
        return 1

    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
    state = json.loads((root / "logs" / "queue-state.json").read_text(encoding="utf-8"))
    failed = next(
        call["env"] for call in calls if call["env"]["COURSEDUMP_EVENT"] == "course_done"
    )

    assert state["results"][0]["error"] == "exact reason from the child run"
    assert state["last_error"] == "course: exact reason from the child run"
    assert failed["COURSEDUMP_LINES"].splitlines()[0] == "exact reason from the child run"


def test_child_failure_reason_and_last_error_are_bounded(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    reason = "error:" + "x" * 400

    def invoke(source, data, child_log, options):
        with child_log.open("a", encoding="utf-8") as output:
            output.write(reason + "\n")
        return 1

    monkeypatch.setattr(queueing, "_invoke_run", invoke)
    queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
    state = json.loads((root / "logs" / "queue-state.json").read_text(encoding="utf-8"))

    assert len(state["results"][0]["error"]) == queueing.FAILURE_REASON_LIMIT
    assert len(state["last_error"]) <= queueing.FAILURE_REASON_LIMIT
    assert state["results"][0]["error"] == reason[:queueing.FAILURE_REASON_LIMIT]


def test_event_text_is_bounded_to_one_thousand_characters(tmp_path, monkeypatch):
    calls = _capture_hooks(monkeypatch)

    queueing._event(
        "notify",
        queueing.QueueLogger(tmp_path / "q.log"),
        tmp_path,
        "queue_failed",
        kind="fail",
        subject="queue",
        title="queue failed on course",
        lines=("x" * 600 + "\n" + "y" * 600,),
    )

    text = calls[0]["env"]["COURSEDUMP_TEXT"]
    assert len(text) <= queueing.HOOK_TEXT_LIMIT == 1000
    assert "\n" not in text
    assert len(calls[0]["env"]["COURSEDUMP_LINES"]) == queueing.HOOK_LINE_LIMIT


def test_dead_pid_status_uses_url_slug_not_raw_entry(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    raw = "https://example.test/private/path/strange[course]"
    (root / "queue.txt").write_text(raw + "\n", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "queue-state.json").write_text(
        json.dumps(
            {
                "status": "running",
                "pid": 99999999,
                "current": {"index": 1, "entry": raw, "stage": "estimate"},
                "counts": {},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["queue", "status", "--data", str(root)])

    assert result.exit_code == 0, result.output
    assert "process died on strange[course], stage estimate" in result.output
    assert "https://example.test" not in result.output


def test_failed_human_status_prints_last_error(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "queue-state.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "current": None,
                "counts": {"DONE": 0, "FAIL": 0, "SKIP": 0},
                "last_error": "course: exact [reason]",
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["queue", "status", "--data", str(root)])

    assert result.exit_code == 0, result.output
    assert "error: course: exact [reason]" in result.output


def test_queue_status_json_exposes_eta_known_and_operational_fields(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "queue.txt").write_text("course\n", encoding="utf-8")
    (root / "queue.lock").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "queue-state.json").write_text(
        json.dumps(
            {
                "status": "running",
                "pid": os.getpid(),
                "current": {"stage": "preflight"},
                "counts": {},
                "remaining_media_seconds": 3600,
                "remaining_unknown_estimates": 1,
                "speed_ratio": 12,
                "eta_known": False,
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["queue", "status", "--data", str(root), "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0, result.output
    assert payload["remaining_media_seconds"] == 3600
    assert payload["process_alive"] is True
    assert payload["queue_eta_seconds"] is None
    assert payload["speed_ratio"] == 12
    assert payload["eta_known"] is False


def _partial_course(out: Path, source: str, name: str) -> None:
    """A 1/2 snapshot: one lesson is done (duration 10), the second is not."""
    course = out / name
    (course / "text").mkdir(parents=True, exist_ok=True)
    (course / "source.json").write_text(
        json.dumps({"source": source, "title": name}), encoding="utf-8"
    )
    manifest.save(
        [
            manifest.Item(rel="a.txt", kind="text", target="a.txt.md"),
            manifest.Item(rel="b.txt", kind="text", target="b.txt.md"),
        ],
        course / "manifest.jsonl",
    )
    (course / "text" / "a.txt.md").write_text(
        "---\nduration: 10\n---\n# a\n\ntext\n", encoding="utf-8"
    )


@pytest.mark.parametrize("with_catalog", [True, False])
def test_snapshot_reports_running_worker_and_waiting_queue(
    tmp_path, monkeypatch, with_catalog
):
    # The v2 notice schema: workers/queue, names taken from catalog.tsv (or null).
    root = tmp_path / "data"
    for name in ("current", "next", "done"):
        (root / "in" / name).mkdir(parents=True)
    current_source = str((root / "in" / "current").resolve())
    _partial_course(root / "out", current_source, "current")
    _complete(root / "out", str((root / "in" / "done").resolve()), "done")
    queue = root / "queue.txt"
    queue.write_text("current\nnext # snapshot below is complete\ndone\n", encoding="utf-8")
    if with_catalog:
        (root / "catalog.tsv").write_text(
            "slug\ttitle\turl\n"
            "current\tCurrent Title\t-\n"
            "next\tNext Title\t-\n"
            "done\t-\t-\n",
            encoding="utf-8",
        )
    state = {
        "status": "running",
        "pid": os.getpid(),
        "speed_ratio": 12.0,
        "current": {
            "index": 1, "entry": "current", "source": current_source, "stage": "run",
            "started_at": "2026-08-25T00:00:00+00:00", "baseline_done": 0,
            "baseline_done_seconds": 0, "total_seconds": 7200,
        },
        "counts": {"DONE": 0, "FAIL": 0, "SKIP": 0},
    }
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "queue-state.json").write_text(json.dumps(state), encoding="utf-8")
    (root / "queue.lock").write_text(f"{os.getpid()}\n", encoding="utf-8")
    monkeypatch.setattr(queueing.time, "time", lambda: 1770000000.0)

    payload = queueing.status_payload(root, queue)
    snapshot = payload["snapshot"]
    eta = payload["progress"]["eta_seconds"]

    assert eta is not None
    assert snapshot == {
        "flow": "coursedump",
        "workers": [
            {
                "item": "current",
                "name": "Current Title" if with_catalog else None,
                "pid": os.getpid(),
                "job": None,
                "stage": {"i": 1, "n": 1, "name": "asr"},
                "done": 1,
                "total": 2,
                "started_at": "2026-08-25T00:00:00+00:00",
                "eta_seconds": eta,
                "eta_total_seconds": eta,
                "state": "running",
                "note": None,
                "metrics": ["media 2.0h", "speed 12.0x"],
            }
        ],
        "queue": [
            {"item": "next", "name": "Next Title" if with_catalog else None,
             "state": "waiting"},
        ],
    }
    result = runner.invoke(
        app, ["queue", "status", "--data", str(root), "--queue", str(queue), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["snapshot"] == snapshot


@pytest.mark.parametrize("lock_pid", [None, "99999999"])
def test_snapshot_is_empty_when_queue_is_not_running(tmp_path, lock_pid):
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    queue = root / "queue.txt"
    queue.write_text("course\n", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "queue-state.json").write_text(
        json.dumps({
            "status": "running",
            "pid": 99999999,
            "current": {
                "index": 1, "entry": "course", "stage": "run",
                "started_at": "2026-08-25T00:00:00+00:00",
            },
            "counts": {},
        }),
        encoding="utf-8",
    )
    if lock_pid:
        (root / "queue.lock").write_text(f"{lock_pid}\n", encoding="utf-8")

    snapshot = queueing.status_payload(root, queue)["snapshot"]

    assert snapshot == {"flow": "coursedump", "workers": [], "queue": []}


def test_snapshot_during_estimate_has_no_worker_but_lists_waiting(tmp_path):
    root = tmp_path / "data"
    for name in ("one", "two"):
        (root / "in" / name).mkdir(parents=True)
    queue = root / "queue.txt"
    queue.write_text("one\ntwo\n", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "queue-state.json").write_text(
        json.dumps({
            "status": "running",
            "pid": os.getpid(),
            "current": {"estimate_index": 1, "entry": "one", "stage": "estimate"},
            "counts": {},
        }),
        encoding="utf-8",
    )
    (root / "queue.lock").write_text(f"{os.getpid()}\n", encoding="utf-8")

    snapshot = queueing.status_payload(root, queue)["snapshot"]

    assert snapshot["workers"] == []
    assert [row["item"] for row in snapshot["queue"]] == ["one", "two"]


def test_all_run_event_titles_are_lowercase_with_english_states(tmp_path, monkeypatch):
    """Regression guard for the "no caps, states in English" rule on EVERY `_event`
    call of a run: course started/done/failed, queue started/done/failed, resume."""
    titles = []

    def scenario(name, queue_text, invoke, previous=None):
        root = tmp_path / name
        for slug in [line.split(" #")[0].strip() for line in queue_text.splitlines()]:
            (root / "in" / slug).mkdir(parents=True)
        (root / "queue.txt").write_text(queue_text, encoding="utf-8")
        if previous is not None:
            (root / "logs").mkdir(parents=True)
            (root / "logs" / "queue-state.json").write_text(
                json.dumps(previous), encoding="utf-8"
            )
        _hook_config(root)
        calls = _capture_hooks(monkeypatch)
        monkeypatch.setattr(
            queueing, "_estimate",
            lambda *args: {"media_duration_seconds": 3600, "work_items": 1},
        )
        monkeypatch.setattr(queueing, "_invoke_run", invoke(root))
        try:
            queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
        except RuntimeError:
            pass
        titles.extend(
            (call["env"]["COURSEDUMP_ENTRY"], call["env"]["COURSEDUMP_TITLE"])
            for call in calls
        )

    def complete(root):
        def invoke(source, data, log, options):
            _complete(root / "out", source, Path(source).name)
            return 0
        return invoke

    def explode(root):
        def invoke(*args):
            raise RuntimeError("boom")
        return invoke

    scenario("ok", "one\ntwo\n", complete)
    scenario("fail", "course\n", lambda root: (lambda *args: 75))
    scenario("crash", "My-Course\n", explode)
    scenario(
        "resume", "course\n", complete,
        previous={"status": "running", "current": {"entry": "Old-Course"}},
    )

    assert {title for _, title in titles} == {
        "queue start", "asr start", "asr done", "fail", "queue done",
        "queue failed on My-Course",
    }
    for slug, title in titles:
        stripped = title.replace(slug, "") if slug else title
        assert stripped == stripped.lower(), title
        assert re.fullmatch(r"[a-z0-9 ?:./~()-]+", stripped), title


def test_inline_comment_in_queue_line_is_stripped_from_slug_and_events(
    tmp_path, monkeypatch
):
    # Incident: `slug # note` went into run and into the notice subject verbatim.
    root = tmp_path / "data"
    (root / "in" / "course").mkdir(parents=True)
    queue = root / "queue.txt"
    queue.write_text(
        "course # note about the course\n"
        "   # whole line is a comment\n"
        "https://example.com/playlist#frag\n",
        encoding="utf-8",
    )
    _hook_config(root)
    calls = _capture_hooks(monkeypatch)
    monkeypatch.setattr(queueing, "_invoke_run", lambda *args: 75)

    assert [line.raw for line in queueing.read_queue(queue)] == [
        "course", "https://example.com/playlist#frag",
    ]
    queueing.run_queue(root, root / "queue.txt", log_path=root / "q.log")
    failed = next(
        call["env"] for call in calls if call["env"]["COURSEDUMP_EVENT"] == "course_done"
    )
    state = json.loads((root / "logs" / "queue-state.json").read_text(encoding="utf-8"))

    assert failed["COURSEDUMP_SUBJECT"] == failed["COURSEDUMP_ABOUT"] == "course"
    assert failed["COURSEDUMP_ENTRY"] == "course"
    assert state["results"][0]["entry"] == "course"
