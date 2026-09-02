"""ASR backends behind one transcribe(media) -> Result interface.

Only local backends are supported: mlx-whisper by default on Apple Silicon,
faster-whisper as a portable alternative, and dummy for tests. Real backends
read media directly through ffmpeg or PyAV, so no intermediate WAV is needed.
"""

import importlib.util
import inspect
import re
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..util import ffprobe_duration

PARAGRAPH_GAP = 1.75    # A pause this long starts a new paragraph.
PARAGRAPH_MAX = 1200    # Force a split after this many characters.

# Decoder settings are the first defense against a self-sustaining Whisper
# loop. They match verified mlx-whisper 0.4.3 defaults except for the deliberate
# condition_on_previous_text=False. Retries start above zero temperature because
# the gate already rejected the initial configuration.
DEFAULT_TEMPERATURES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
RETRY_TEMPERATURES = (0.4, 0.6, 0.8, 1.0)
FINAL_TEMPERATURES = (0.8, 1.0)
COMPRESSION_RATIO_THRESHOLD = 2.4
LOGPROB_THRESHOLD = -1.0
NO_SPEECH_THRESHOLD = 0.6
CONDITION_ON_PREVIOUS_TEXT = False
MAX_ASR_ATTEMPTS = 3

# The quality gate examines raw segments before collapse_repeats so cosmetic
# deduplication cannot hide lost speech. Thresholds detect a long local loop or
# repeated runs occupying a material fraction of a file. Extreme WPM values
# complement segment signals but do not prove a loop by themselves.
QUALITY_REPEAT_RUN_LENGTH = 10
QUALITY_MAX_IDENTICAL_RUN = 20
QUALITY_MAX_REPEATED_FRACTION = 0.20
QUALITY_MIN_UNIQUENESS = 0.15
QUALITY_MIN_DISTRIBUTION_LINES = 10
QUALITY_MIN_MEDIA_SECONDS_FOR_WPM = 60.0
QUALITY_MIN_WPM = 1.0
QUALITY_MAX_WPM = 250.0
QUALITY_HEADER_PREFIX = "# QUALITY:"

_WORD = re.compile(r"\w+", re.UNICODE)
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_AUTO_LANGUAGE = object()


def collapse_repeats(line: str, keep: int = 3, trigger: int = 4) -> str:
    """Collapse pathological repeats such as a Whisper loop on music or silence.

    A one-to-four-word unit repeated at least `trigger` times is reduced to
    `keep` copies. Shorter natural repetitions remain untouched.
    """
    def norm(ws):
        out = [w.lower().strip(".,!?;:—-") for w in ws]
        # A punctuation-only unit normalizes to empty. Compare its raw tokens so
        # a wall of punctuation remains visible to both collapse stages.
        return out if any(out) else [w.lower() for w in ws]

    words = line.split()
    n, i, out = len(words), 0, []
    while i < n:
        best_span = best_reps = 0
        for span in (1, 2, 3, 4):
            unit = norm(words[i:i + span])
            reps, j = 1, i + span
            while j + span <= n and norm(words[j:j + span]) == unit:
                reps += 1
                j += span
            if reps >= trigger and reps > best_reps:
                best_span, best_reps = span, reps
        if best_span:
            out.extend(words[i:i + best_span * keep])
            i += best_span * best_reps
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


def collapse_para_runs(paras: list[str], keep: int = 3, trigger: int = 4) -> list[str]:
    """Collapse a run of consecutive identical paragraphs.

    Paragraph splitting can divide one long hallucination before
    collapse_repeats sees it, leaving many identical paragraphs. This second
    stage collapses that remaining wall.
    """
    out: list[str] = []
    i, n = 0, len(paras)
    while i < n:
        j = i
        while j < n and paras[j] == paras[i]:
            j += 1
        reps = j - i
        out.extend(paras[i:i + (keep if reps >= trigger else reps)])
        i = j
    return out


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Result:
    segments: list[Segment] = field(default_factory=list)
    language: str = ""
    backend: str = ""
    model: str = ""
    attempts: int = 1
    quality: "QualityMetrics | None" = None
    quality_warning: str = ""

    def markdown_body(self) -> str:
        """Group segments into paragraphs at pauses."""
        paras, cur, prev_end = [], [], None
        for s in self.segments:
            t = s.text.strip()
            if not t:
                continue
            if cur and (
                (prev_end is not None and s.start - prev_end >= PARAGRAPH_GAP)
                or sum(len(x) for x in cur) > PARAGRAPH_MAX
            ):
                paras.append(" ".join(cur))
                cur = []
            cur.append(t)
            prev_end = s.end
        if cur:
            paras.append(" ".join(cur))
        return "\n\n".join(collapse_para_runs([collapse_repeats(p) for p in paras]))


@dataclass(frozen=True)
class QualityMetrics:
    lines: int
    unique_lines: int
    uniqueness: float
    max_identical_run: int
    repeated_lines: int
    repeated_fraction: float
    words: int
    duration_seconds: float
    words_per_minute: float
    reasons: tuple[str, ...] = ()

    @property
    def loop_detected(self) -> bool:
        return bool(self.reasons)

    def summary(self) -> str:
        return (
            f"{self.repeated_fraction:.0%} lines repeated, "
            f"longest run {self.max_identical_run}, "
            f"uniqueness {self.uniqueness:.0%}, "
            f"{self.words_per_minute:.0f} words/min"
        )


def _normalise_line(value: str) -> str:
    raw = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    words = " ".join(part for part in _NON_WORD.split(raw) if part)
    # A punctuation wall is also a loop. Keep an empty word-normalization result
    # as raw text so this class remains visible to the gate.
    return words or raw


def quality_metrics(result: Result, duration_seconds: float) -> QualityMetrics:
    """Measure raw segment transcription before cosmetic collapsing."""
    lines = [
        normalised
        for segment in result.segments
        for line in (segment.text.splitlines() or [segment.text])
        if (normalised := _normalise_line(line))
    ]
    runs: list[int] = []
    previous = None
    for line in lines:
        if runs and line == previous:
            runs[-1] += 1
        else:
            runs.append(1)
        previous = line

    count = len(lines)
    unique = len(set(lines))
    max_run = max(runs, default=0)
    repeated = sum(run for run in runs if run >= QUALITY_REPEAT_RUN_LENGTH)
    repeated_fraction = repeated / count if count else 0.0
    uniqueness = unique / count if count else 1.0
    words = sum(len(_WORD.findall(segment.text)) for segment in result.segments)
    duration = max(0.0, float(duration_seconds or 0.0))
    wpm = words * 60.0 / duration if duration else 0.0

    reasons: list[str] = []
    if max_run >= QUALITY_MAX_IDENTICAL_RUN:
        reasons.append("identical-run")
    if repeated_fraction >= QUALITY_MAX_REPEATED_FRACTION:
        reasons.append("repeated-fraction")
    if count >= QUALITY_MIN_DISTRIBUTION_LINES and uniqueness <= QUALITY_MIN_UNIQUENESS:
        reasons.append("low-uniqueness")
    if duration >= QUALITY_MIN_MEDIA_SECONDS_FOR_WPM and words:
        if wpm < QUALITY_MIN_WPM:
            reasons.append("low-wpm")
        elif wpm > QUALITY_MAX_WPM:
            reasons.append("high-wpm")
    return QualityMetrics(
        lines=count,
        unique_lines=unique,
        uniqueness=uniqueness,
        max_identical_run=max_run,
        repeated_lines=repeated,
        repeated_fraction=repeated_fraction,
        words=words,
        duration_seconds=duration,
        words_per_minute=wpm,
        reasons=tuple(reasons),
    )


def quality_warning(metrics: QualityMetrics) -> str:
    return (
        f"{QUALITY_HEADER_PREFIX} ASR loop, {metrics.summary()}; "
        "speech in these sections was not recovered"
    )


def _require_explicit_kwargs(call, names: set[str], backend: str) -> None:
    """Fail closed on an incompatible version even when the API accepts **kwargs."""
    accepted = set(inspect.signature(call).parameters)
    missing = sorted(names - accepted)
    if missing:
        raise RuntimeError(
            f"{backend}: installed version does not declare kwargs: {', '.join(missing)}"
        )


class MlxWhisperASR:
    name = "mlx-whisper"

    def __init__(self, model: str = "large-v3-turbo", language: str = ""):
        self.requested_model = model
        self.model = model if "/" in model else f"mlx-community/whisper-{model}"
        self.language = language

    def transcribe(
        self,
        media: Path,
        *,
        temperatures: tuple[float, ...] = DEFAULT_TEMPERATURES,
        language: str | object = _AUTO_LANGUAGE,
    ) -> Result:
        import mlx_whisper  # Lazy import because it loads MLX.

        kw = {
            "condition_on_previous_text": CONDITION_ON_PREVIOUS_TEXT,
            "temperature": temperatures,
            "compression_ratio_threshold": COMPRESSION_RATIO_THRESHOLD,
            "logprob_threshold": LOGPROB_THRESHOLD,
            "no_speech_threshold": NO_SPEECH_THRESHOLD,
        }
        _require_explicit_kwargs(mlx_whisper.transcribe, set(kw), self.name)
        selected_language = self.language if language is _AUTO_LANGUAGE else language
        if selected_language:
            kw["language"] = selected_language
        out = mlx_whisper.transcribe(
            str(media), path_or_hf_repo=self.model, verbose=None, **kw)
        return Result(
            segments=[Segment(s["start"], s["end"], s["text"]) for s in out["segments"]],
            language=out.get("language", ""),
            backend=self.name,
            model=self.model,
        )


class FasterWhisperASR:
    name = "faster-whisper"

    def __init__(self, model: str = "large-v3-turbo", language: str = ""):
        self.requested_model = model
        self.model = model
        self.language = language
        self._m = None

    def transcribe(
        self,
        media: Path,
        *,
        temperatures: tuple[float, ...] = DEFAULT_TEMPERATURES,
        language: str | object = _AUTO_LANGUAGE,
    ) -> Result:
        from faster_whisper import WhisperModel

        if self._m is None:
            self._m = WhisperModel(self.model, compute_type="int8")
        kw = {
            "condition_on_previous_text": CONDITION_ON_PREVIOUS_TEXT,
            "temperature": temperatures,
            "compression_ratio_threshold": COMPRESSION_RATIO_THRESHOLD,
            # faster-whisper names the same threshold log_prob, with an underscore.
            "log_prob_threshold": LOGPROB_THRESHOLD,
            "no_speech_threshold": NO_SPEECH_THRESHOLD,
            "vad_filter": True,
        }
        _require_explicit_kwargs(self._m.transcribe, set(kw), self.name)
        selected_language = self.language if language is _AUTO_LANGUAGE else language
        segs, info = self._m.transcribe(
            str(media), language=selected_language or None, **kw)
        return Result(
            segments=[Segment(s.start, s.end, s.text) for s in segs],
            language=info.language or "",
            backend=self.name,
            model=self.model,
        )


class DummyASR:
    """Deterministic test backend with no model or network access."""
    name = "dummy"

    def __init__(self, model: str = "", language: str = ""):
        self.model = "dummy"

    def transcribe(self, media: Path, **_kw) -> Result:
        return Result(
            segments=[Segment(0, 1, f"[dummy transcript of {media.name}]")],
            backend=self.name,
            model=self.model,
        )


def _faster_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def _duration(media: Path, result: Result) -> float:
    probed = ffprobe_duration(media) or 0.0
    return probed or max((segment.end for segment in result.segments), default=0.0)


def _quality_rank(metrics: QualityMetrics) -> tuple[float, ...]:
    """If every attempt is red, retain the least damaged rather than the last."""
    wpm_penalty = (
        max(0.0, QUALITY_MIN_WPM - metrics.words_per_minute)
        + max(0.0, metrics.words_per_minute - QUALITY_MAX_WPM)
    )
    return (
        len(metrics.reasons),
        metrics.repeated_fraction,
        float(metrics.max_identical_run),
        1.0 - metrics.uniqueness,
        wpm_penalty,
    )


class QualityGatedASR:
    """Retry a red transcription with different decoding before publication."""

    def __init__(self, primary):
        self.primary = primary
        self.name = primary.name
        self.model = primary.model

    def _attempts(self):
        yield self.primary, DEFAULT_TEMPERATURES, _AUTO_LANGUAGE
        if self.primary.name == MlxWhisperASR.name and _faster_available():
            yield FasterWhisperASR(
                self.primary.requested_model,
                self.primary.language,
            ), RETRY_TEMPERATURES, _AUTO_LANGUAGE
        else:
            yield self.primary, RETRY_TEMPERATURES, _AUTO_LANGUAGE
        # A red gate with explicit --language justifies trying autodetection;
        # forcing the wrong language can itself provoke loops.
        final_language = "" if self.primary.language else _AUTO_LANGUAGE
        yield self.primary, FINAL_TEMPERATURES, final_language

    def transcribe(self, media: Path) -> Result:
        candidates: list[Result] = []
        attempted = 0
        for attempt, (backend, temperatures, language) in enumerate(
            self._attempts(), 1
        ):
            if attempt > MAX_ASR_ATTEMPTS:
                break
            attempted += 1
            try:
                result = backend.transcribe(
                    media,
                    temperatures=temperatures,
                    language=language,
                )
            except Exception:
                # A broken optional faster-whisper installation must not prevent
                # the final attempt with the primary backend.
                if backend is self.primary:
                    raise
                continue
            metrics = quality_metrics(result, _duration(media, result))
            result = replace(result, attempts=attempted, quality=metrics)
            candidates.append(result)
            if not metrics.loop_detected:
                return result

        best = min(candidates, key=lambda item: _quality_rank(item.quality))
        return replace(
            best,
            attempts=attempted,
            quality_warning=quality_warning(best.quality),
        )


BACKENDS = {"mlx": MlxWhisperASR, "fwhisper": FasterWhisperASR, "dummy": DummyASR}


def get_backend(name: str, model: str, language: str = ""):
    if name not in BACKENDS:
        raise ValueError(f"unknown ASR backend {name!r}; available: {', '.join(BACKENDS)}")
    backend = BACKENDS[name](model=model, language=language)
    return backend if name == "dummy" else QualityGatedASR(backend)
