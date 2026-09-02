"""Versioned JSON surface for runtime connectors."""

import json

from typer.testing import CliRunner

from coursedump import executor, manifest, sources
from coursedump.cli import app, machine_capabilities
from coursedump.space import NoSpace


runner = CliRunner()


def _payload(result) -> dict[str, object]:
    assert result.exception is None or isinstance(result.exception, SystemExit)
    return json.loads(result.stdout)


def test_capabilities_are_exact_versioned_contract():
    result = runner.invoke(app, ["capabilities", "--json"])

    assert result.exit_code == 0
    assert _payload(result) == machine_capabilities()
    assert [row["name"] for row in _payload(result)["capabilities"]] == [
        "plan",
        "run",
        "status",
        "queue-status",
    ]


def test_queue_status_is_declared_as_read_only_machine_capability():
    capability = next(
        row for row in machine_capabilities()["capabilities"]
        if row["name"] == "queue-status"
    )

    assert capability["argv"] == [
        "coursedump",
        "queue",
        "status",
        "--data",
        "<data-home>",
        "--json",
    ]
    assert capability["idempotency"] == "read-only"
    assert capability["machine_output"]["required_fields"] == [
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
    ]


def test_plan_json_is_read_only_and_contains_full_estimate(tmp_path, monkeypatch):
    data = tmp_path / "missing-data"

    class _Source:
        is_local = False

        def title(self):
            return "Synthetic course"

        def list(self):
            return [
                manifest.Item(rel="lesson.mp4", kind="video", size=1024),
                manifest.Item(rel="notes.pdf", kind="pdf", size=512),
            ]

    monkeypatch.setattr(sources, "detect", lambda *args, **kwargs: _Source())
    result = runner.invoke(
        app,
        ["plan", "synthetic-source", "--data", str(data), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert set(payload) == {"schema_version", "source", "items", "estimate"}
    assert payload["schema_version"] == 1
    assert payload["source"] == {
        "input": "synthetic-source",
        "title": "Synthetic course",
        "adapter": "_Source",
        "is_local": False,
    }
    assert payload["items"] == [
        {"kind": "pdf", "count": 1, "size_bytes": 512, "work_items": 1},
        {"kind": "video", "count": 1, "size_bytes": 1024, "work_items": 1},
    ]
    assert payload["estimate"]["basis"] == "size"
    assert not data.exists()


def test_plan_json_failure_is_structured_and_does_not_echo_details(
    tmp_path, monkeypatch
):
    marker = "PRIVATE-DETAIL-MUST-NOT-APPEAR"

    def fail(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(sources, "detect", fail)
    result = runner.invoke(
        app,
        ["plan", "synthetic-source", "--data", str(tmp_path / "data"), "--json"],
    )

    assert result.exit_code == 1
    assert marker not in result.stdout
    assert _payload(result) == {
        "schema_version": 1,
        "source": None,
        "items": [],
        "estimate": None,
        "status": "failure",
        "error": "plan-failed",
    }


def test_plan_json_maps_interruption_to_transient(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sources,
        "detect",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    result = runner.invoke(
        app,
        ["plan", "synthetic-source", "--data", str(tmp_path / "data"), "--json"],
    )

    assert result.exit_code == 111
    assert _payload(result)["status"] == "transient"


def test_plan_json_has_bounded_output(tmp_path, monkeypatch):
    class _Source:
        is_local = False

        def title(self):
            return "x" * (70 * 1024)

        def list(self):
            return []

    monkeypatch.setattr(sources, "detect", lambda *args, **kwargs: _Source())
    result = runner.invoke(
        app,
        ["plan", "synthetic-source", "--data", str(tmp_path / "data"), "--json"],
    )

    assert result.exit_code == 1
    assert _payload(result) == {
        "schema_version": 1,
        "source": None,
        "items": [],
        "estimate": None,
        "status": "failure",
        "error": "machine-output-too-large",
    }


def test_status_json_reports_versioned_runs(tmp_path):
    out = tmp_path / "out"
    course = out / "course-a"
    (course / "text").mkdir(parents=True)
    (course / "source.json").write_text(
        json.dumps({"source": "synthetic-source"}), encoding="utf-8"
    )
    manifest.save(
        [
            manifest.Item(rel="lesson.txt", kind="text", target="lesson.txt.md"),
            manifest.Item(rel="image.png", kind="image", skip="image"),
        ],
        course / "manifest.jsonl",
    )
    (course / "text/lesson.txt.md").write_text("done\n", encoding="utf-8")
    (course / "errors.jsonl").write_text("{}\n{}\n", encoding="utf-8")

    result = runner.invoke(app, ["status", "--out", str(out), "--json"])

    assert result.exit_code == 0
    assert _payload(result) == {
        "schema_version": 1,
        "runs": [
            {
                "course": "course-a",
                "source": "synthetic-source",
                "artifact": str(course),
                "done": 1,
                "total": 1,
                "errors": 2,
                "quality_warnings": 0,
            }
        ],
    }


def test_status_human_and_json_report_quality_warnings(tmp_path):
    out = tmp_path / "out"
    course = out / "course-a"
    (course / "text").mkdir(parents=True)
    (course / "source.json").write_text(
        json.dumps({"source": "synthetic-source"}), encoding="utf-8"
    )
    manifest.save(
        [manifest.Item(rel="lesson.mp4", kind="video", target="lesson.mp4.md")],
        course / "manifest.jsonl",
    )
    (course / "text/lesson.mp4.md").write_text(
        "# QUALITY: ASR loop, speech not recovered\n\ntext\n",
        encoding="utf-8",
    )

    human = runner.invoke(app, ["status", "--out", str(out)])
    machine = runner.invoke(app, ["status", "--out", str(out), "--json"])

    assert human.exit_code == 0, human.output
    assert "QUALITY=1" in human.output
    assert machine.exit_code == 0, machine.output
    assert _payload(machine)["runs"][0]["quality_warnings"] == 1


def test_run_json_emits_one_summary_and_restores_console(tmp_path, monkeypatch):
    old_console = executor.console
    monkeypatch.setattr("coursedump.cli._caffeinate", lambda: None)
    monkeypatch.setattr(
        executor,
        "run_course",
        lambda source, opts: {
            "done": 2,
            "total": 2,
            "errors": 0,
            "course": opts.out_root / "course-a",
        },
    )

    result = runner.invoke(
        app,
        ["run", "synthetic-source", "--data", str(tmp_path / "data"), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert _payload(result) == {
        "schema_version": 1,
        "status": "succeeded",
        "artifact": {
            "source": "synthetic-source",
            "course_dir": str(tmp_path / "data/out/course-a"),
            "done": 2,
            "total": 2,
            "errors": 0,
            "quality_warnings": 0,
        },
    }
    assert executor.console is old_console


def test_run_json_maps_space_and_generic_failures_without_raw_details(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("coursedump.cli._caffeinate", lambda: None)
    marker = "PRIVATE-DETAIL-MUST-NOT-APPEAR"

    def no_space(*args, **kwargs):
        raise NoSpace(marker)

    monkeypatch.setattr(executor, "run_course", no_space)
    quota = runner.invoke(
        app,
        ["run", "synthetic-source", "--data", str(tmp_path / "quota"), "--json"],
    )
    assert quota.exit_code == 75
    assert marker not in quota.stdout
    assert _payload(quota)["status"] == "resource"

    monkeypatch.setattr(
        executor,
        "run_course",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(marker)),
    )
    failed = runner.invoke(
        app,
        ["run", "synthetic-source", "--data", str(tmp_path / "failed"), "--json"],
    )
    assert failed.exit_code == 1
    assert marker not in failed.stdout
    assert _payload(failed)["status"] == "failure"


def test_run_json_requires_one_source_before_data_mutation(tmp_path):
    data = tmp_path / "data"
    result = runner.invoke(app, ["run", "--data", str(data), "--json"])

    assert result.exit_code == 1
    assert _payload(result)["error"] == "machine-run-requires-exactly-one-source"
    assert not data.exists()


def test_run_json_normalizes_setup_failure(tmp_path, monkeypatch):
    marker = "PRIVATE-DETAIL-MUST-NOT-APPEAR"

    def fail(*args, **kwargs):
        raise OSError(marker)

    monkeypatch.setattr("coursedump.cli.ensure_data_dirs", fail)
    result = runner.invoke(
        app,
        ["run", "synthetic-source", "--data", str(tmp_path / "data"), "--json"],
    )

    assert result.exit_code == 1
    assert marker not in result.stdout
    assert _payload(result)["error"] == "run-setup-failed"


def test_run_json_maps_setup_interruption_to_transient(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coursedump.cli._caffeinate",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    result = runner.invoke(
        app,
        ["run", "synthetic-source", "--data", str(tmp_path / "data"), "--json"],
    )

    assert result.exit_code == 111
    assert _payload(result)["status"] == "transient"


def test_status_json_normalizes_malformed_manifest(tmp_path):
    out = tmp_path / "out"
    course = out / "course-a"
    course.mkdir(parents=True)
    (course / "source.json").write_text(
        json.dumps({"source": "synthetic-source"}), encoding="utf-8"
    )
    (course / "manifest.jsonl").write_text("not-json\n", encoding="utf-8")

    result = runner.invoke(app, ["status", "--out", str(out), "--json"])

    assert result.exit_code == 1
    assert _payload(result) == {
        "schema_version": 1,
        "runs": [],
        "status": "failure",
        "error": "status-read-failed",
    }
