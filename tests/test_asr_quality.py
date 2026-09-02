"""Anti-loop decoding settings and the ASR quality gate."""

import sys
from pathlib import Path
from types import SimpleNamespace

from coursedump import manifest
from coursedump.extractors import asr
from coursedump.extractors import extract


def _loop_result(n: int = 20, text: str = "Okay.") -> asr.Result:
    return asr.Result(
        segments=[asr.Segment(i, i + 1, text) for i in range(n)],
        language="en",
        backend="fake",
        model="fake-model",
    )


def _good_result(n: int = 20) -> asr.Result:
    return asr.Result(
        segments=[asr.Segment(i, i + 1, f"unique sentence number {i}") for i in range(n)],
        language="en",
        backend="fake",
        model="fake-model",
    )


def test_quality_gate_catches_synthetic_segment_loop():
    metrics = asr.quality_metrics(_loop_result(10, "…"), duration_seconds=60)

    assert metrics.loop_detected
    assert metrics.max_identical_run == 10
    assert metrics.repeated_fraction == 1.0
    assert metrics.uniqueness == 0.1
    assert "repeated-fraction" in metrics.reasons


def test_mlx_default_kwargs_are_explicitly_anti_loop(monkeypatch):
    calls = []

    def transcribe(
        audio,
        *,
        path_or_hf_repo,
        verbose,
        temperature,
        compression_ratio_threshold,
        logprob_threshold,
        no_speech_threshold,
        condition_on_previous_text,
        language=None,
    ):
        calls.append(locals())
        return {"segments": [], "language": "en"}

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=transcribe))
    backend = asr.MlxWhisperASR(language="")
    backend.transcribe(Path("lesson.mp4"))

    kw = calls[0]
    assert kw["condition_on_previous_text"] is False
    assert kw["temperature"] == asr.DEFAULT_TEMPERATURES
    assert kw["compression_ratio_threshold"] == 2.4
    assert kw["logprob_threshold"] == -1.0
    assert kw["no_speech_threshold"] == 0.6
    assert kw["language"] is None  # empty CLI flag means autodetect


def test_faster_whisper_default_kwargs_include_vad_and_thresholds(monkeypatch):
    calls = []

    class WhisperModel:
        def __init__(self, model, compute_type):
            assert model == "large-v3-turbo"
            assert compute_type == "int8"

        def transcribe(
            self,
            audio,
            *,
            language,
            vad_filter,
            temperature,
            compression_ratio_threshold,
            log_prob_threshold,
            no_speech_threshold,
            condition_on_previous_text,
        ):
            calls.append(locals())
            return [], SimpleNamespace(language="en")

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        SimpleNamespace(WhisperModel=WhisperModel),
    )
    asr.FasterWhisperASR(language="").transcribe(Path("lesson.mp4"))

    kw = calls[0]
    assert kw["vad_filter"] is True
    assert kw["condition_on_previous_text"] is False
    assert kw["temperature"] == asr.DEFAULT_TEMPERATURES
    assert kw["compression_ratio_threshold"] == 2.4
    assert kw["log_prob_threshold"] == -1.0
    assert kw["no_speech_threshold"] == 0.6
    assert kw["language"] is None


def test_red_quality_gate_retranscribes_with_higher_start_temperature(
    tmp_path, monkeypatch
):
    class Primary:
        name = "mlx-whisper"
        model = "mlx-community/whisper-large-v3-turbo"
        requested_model = "large-v3-turbo"
        language = ""

        def __init__(self):
            self.calls = []

        def transcribe(self, media, *, temperatures, language):
            self.calls.append((temperatures, language))
            return _loop_result() if len(self.calls) == 1 else _good_result()

    primary = Primary()
    monkeypatch.setattr(asr, "_faster_available", lambda: False)
    monkeypatch.setattr(asr, "ffprobe_duration", lambda _path: 60.0)

    result = asr.QualityGatedASR(primary).transcribe(tmp_path / "lesson.mp4")

    assert len(primary.calls) == 2
    assert primary.calls[0][0] == asr.DEFAULT_TEMPERATURES
    assert primary.calls[1][0] == asr.RETRY_TEMPERATURES
    assert result.attempts == 2
    assert not result.quality.loop_detected
    assert not result.quality_warning


def test_mlx_gate_switches_to_available_faster_whisper(tmp_path, monkeypatch):
    class Backend:
        def __init__(self, name, result):
            self.name = name
            self.model = "model"
            self.requested_model = "model"
            self.language = ""
            self.result = result
            self.calls = 0

        def transcribe(self, media, *, temperatures, language):
            self.calls += 1
            return self.result

    primary = Backend("mlx-whisper", _loop_result())
    fallback = Backend("faster-whisper", _good_result())
    monkeypatch.setattr(asr, "_faster_available", lambda: True)
    monkeypatch.setattr(asr, "FasterWhisperASR", lambda *_args: fallback)
    monkeypatch.setattr(asr, "ffprobe_duration", lambda _path: 60.0)

    result = asr.QualityGatedASR(primary).transcribe(tmp_path / "lesson.mp4")

    assert primary.calls == 1
    assert fallback.calls == 1
    assert result.attempts == 2
    assert not result.quality.loop_detected


def test_exhausted_gate_writes_loud_quality_header_and_autodetect_retry(
    tmp_path, monkeypatch
):
    class AlwaysLoop:
        name = "mlx-whisper"
        model = "mlx-community/whisper-large-v3-turbo"
        requested_model = "large-v3-turbo"
        language = "ru"

        def __init__(self):
            self.languages = []

        def transcribe(self, media, *, temperatures, language):
            self.languages.append(language)
            return _loop_result()

    primary = AlwaysLoop()
    monkeypatch.setattr(asr, "_faster_available", lambda: False)
    monkeypatch.setattr(asr, "ffprobe_duration", lambda _path: 60.0)
    monkeypatch.setattr("coursedump.extractors.ffprobe_duration", lambda _path: 60.0)
    item = manifest.Item(
        rel="lesson.mp4",
        kind="video",
        target="lesson.mp4.md",
    )

    md = extract(item, tmp_path / "lesson.mp4", asr.QualityGatedASR(primary))

    assert len(primary.languages) == asr.MAX_ASR_ATTEMPTS
    assert primary.languages[-1] == ""  # the last attempt drops the explicit --language
    assert "asr_attempts: 3" in md
    assert asr.QUALITY_HEADER_PREFIX in md
    assert "speech in these sections was not recovered" in md
    assert "100% lines repeated" in md
