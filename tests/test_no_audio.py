"""Media with no audio track: a skip, not a loss (incident 2026-08-26)."""

import shutil
import subprocess

import pytest

from coursedump import executor, manifest
from coursedump.executor import Opts, run_course

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


def test_video_without_audio_is_a_skip_and_other_failures_stay_losses(tmp_path, monkeypatch):
    """A clip with no audio track is not a loss, it is "nothing to transcribe":
    it must land as skip=no_audio with an empty target. Otherwise the manifest
    drifts away from text/ and the consumer downstream refuses the WHOLE course -
    a single nine second promo clip once failed a course that way and took both
    overnight queue jobs with it.
    ANY OTHER extraction error stays a loss: the target is kept, the course is
    incomplete and the next run finishes it."""
    root = tmp_path / "course"
    root.mkdir()
    # Cyrillic media names on purpose: they travel through ffmpeg argv, the glob
    # and the manifest keys in one go.
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=black:s=32x32:d=1", "-pix_fmt", "yuv420p",
                    str(root / "креатив.mp4")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1",
                    str(root / "урок.mp3")], check=True)

    monkeypatch.setattr(executor, "extract",
                        lambda it, path, asr: (_ for _ in ()).throw(
                            RuntimeError("Failed to load audio")))

    out = tmp_path / "out"
    run_course(str(root), Opts(out_root=out, asr_backend="dummy"))
    items = {it.rel: it for it in manifest.load(out / "course" / "manifest.jsonl")}

    assert items["креатив.mp4"].skip == "no_audio"
    assert items["креатив.mp4"].target == ""
    assert items["урок.mp3"].skip == ""          # audio is there, so this is a real loss
    assert items["урок.mp3"].target             # target kept: the resume run finishes it
