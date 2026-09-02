"""CLI smoke: doctor and data paths. Data lives in the tools-data store, never
inside the repository.

Live runs (network, real platforms) do not belong here.
"""
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from coursedump import executor, manifest, sources
from coursedump.cli import REPO_ROOT, app, data_root, ensure_data_dirs

runner = CliRunner()

BOOSTY_POST = "https://boosty.to/demo-creator/posts/f897a4cc"


def test_doctor_runs_and_reports(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    res = runner.invoke(app, ["doctor", "--data", str(d)])
    flat = res.output.replace("\n", "")  # rich wraps long paths
    assert "doctor" in flat and str(d) in flat
    if shutil.which("ffmpeg"):  # with the environment in place doctor must be green
        assert res.exit_code == 0


def test_doctor_missing_data_does_not_create(tmp_path):
    d = tmp_path / "no-such-data"
    runner.invoke(app, ["doctor", "--data", str(d)])
    assert not d.exists()  # doctor diagnoses, it never mutates


def test_data_root_default_is_tools_data(monkeypatch):
    """The default is the single tools-data store, NOT a sibling of the repo: the
    old fallback quietly started a second store whenever the tool ran without the
    env variable (incident 2026-08-19)."""
    monkeypatch.delenv("COURSEDUMP_DATA", raising=False)
    assert data_root(None) == Path.home() / "tools-data" / "coursedump-data"
    assert data_root(None) != REPO_ROOT.parent / "coursedump-data"


def test_data_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("COURSEDUMP_DATA", str(tmp_path / "elsewhere"))
    assert data_root(None) == (tmp_path / "elsewhere").resolve()


def test_run_flags_that_switch_off_the_source_profile_reach_opts(tmp_path, monkeypatch):
    """`--full-video` / `--no-throttle` are the only way to relax a source profile
    (audio only plus throttling). If they never reach Opts the flag lies silently
    and the user believes they are getting video, or running without pauses."""
    seen: list[executor.Opts] = []

    def fake_run_course(source_str, opts):
        seen.append(opts)
        return {"done": 0, "total": 0, "errors": 0, "course": "x"}

    monkeypatch.setattr(executor, "run_course", fake_run_course)
    base = ["run", BOOSTY_POST, "--data", str(tmp_path / "d")]

    runner.invoke(app, base)
    assert not seen[-1].full_video and not seen[-1].no_throttle  # default: profile in force
    runner.invoke(app, base + ["--no-throttle"])
    assert seen[-1].no_throttle and not seen[-1].full_video
    runner.invoke(app, base + ["--full-video"])
    assert seen[-1].full_video and not seen[-1].no_throttle


def test_plan_passes_full_video_to_detect(tmp_path, monkeypatch):
    """`plan` reports sizes, so it has to know about the profile audio-only mode:
    otherwise its estimate differs from what `run` fetches by an order of
    magnitude."""
    seen: list[bool] = []

    class _Src:
        is_local = False

        def title(self):
            return "post"

        def list(self):
            return [manifest.Item(rel="001 - Lesson", kind="video", size=10)]

    def fake_detect(source, cookies_from_browser="", full_video=False, **kw):
        seen.append(full_video)
        return _Src()

    monkeypatch.setattr(sources, "detect", fake_detect)
    runner.invoke(app, ["plan", BOOSTY_POST, "--data", str(tmp_path / "d")])
    runner.invoke(app, ["plan", BOOSTY_POST, "--data", str(tmp_path / "d"), "--full-video"])
    assert seen == [False, True]


def test_cli_does_not_choke_on_rich_markup_from_outside(tmp_path, monkeypatch):
    """A user string and an exception message are foreign strings in the rich
    markup language: '[/]' breaks rendering with a MarkupError (so instead of a
    diagnosis the user gets a traceback) and '[dim]' is swallowed silently.
    Checked against real rich through CliRunner."""
    def boom(source_str, opts):
        raise RuntimeError("failed [/] at the [dim] step")

    monkeypatch.setattr(executor, "run_course", boom)
    res = runner.invoke(app, ["run", "[SW] source [dim]",
                              "--data", str(tmp_path / "d")])

    assert res.exit_code == 1, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), res.output
    flat = res.output.replace("\n", "")   # rich wraps long lines
    assert "[SW] source [dim]" in flat
    assert "failed [/] at the [dim] step" in flat


def test_ensure_data_dirs_creates_in_out_and_seeds_filters(tmp_path):
    root = tmp_path / "cd-data"
    ensure_data_dirs(root)
    assert (root / "in").is_dir() and (root / "out").is_dir()
    assert "strip_prefixes" in (root / "filters.toml").read_text(encoding="utf-8")
    # a repeat call is idempotent and does not overwrite the working copy
    (root / "filters.toml").write_text("blacklist = []\n", encoding="utf-8")
    ensure_data_dirs(root)
    assert (root / "filters.toml").read_text(encoding="utf-8") == "blacklist = []\n"
