"""Smoke on a local source: a fixture "course" -> snapshot -> resumability."""

import re
import shutil
import subprocess

import pytest

from coursedump import executor, manifest, normalize
from coursedump.executor import Opts, run_course
from coursedump.assemble import EMPTY_TEXT_WORDS
from coursedump.extractors.media import parse_subs

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


@pytest.fixture
def course(tmp_path):
    """A Russian-language course tree, which is what this tool is pointed at.

    The Cyrillic names stay: they exercise non-ASCII paths end to end (ffmpeg
    argv, globs, manifest keys, output file names), and the blacklisted file has
    to match a default filter pattern, all of which are Russian.
    """
    root = tmp_path / "[SWBAND.CO] Тестовый Курс"
    (root / "01 Модуль").mkdir(parents=True)
    (root / "02 Материалы").mkdir()

    # one second of video with sound
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-shortest",
         str(root / "01 Модуль" / "[SWBAND.CO] Урок 1.mp4")],
        check=True, capture_output=True)

    # pdf
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Hello PDF content")
    doc.save(root / "02 Материалы" / "конспект.pdf")

    (root / "заметки.txt").write_text("just text", encoding="utf-8")
    # the name matches a default blacklist pattern
    (root / "Не пропусти этот шаг!.txt").write_text("promo spam", encoding="utf-8")
    (root / ".DS_Store").write_bytes(b"\x00")
    return root


def _run(root, tmp_path):
    return run_course(str(root), Opts(out_root=tmp_path / "out", asr_backend="dummy"))


def test_full_pipeline_and_resume(course, tmp_path):
    stats = _run(course, tmp_path)
    cdir = tmp_path / "out" / "Тестовый Курс"          # the tag is stripped from the course name too

    video_md = cdir / "text" / "01 Модуль" / "Урок 1.mp4.md"  # tag stripped, extension kept
    assert video_md.exists()
    body = video_md.read_text(encoding="utf-8")
    assert "method: asr" in body and "dummy transcript" in body

    pdf_md = cdir / "text" / "02 Материалы" / "конспект.pdf.md"
    assert pdf_md.exists() and "Hello PDF" in pdf_md.read_text(encoding="utf-8")

    assert (cdir / "text" / "заметки.txt.md").exists()
    # neither the blacklisted file nor the junk got in
    assert not (cdir / "text" / "Не пропусти этот шаг!.txt.md").exists()
    assert stats["done"] == stats["total"] == 3
    assert (cdir / "INDEX.md").exists() and (cdir / "manifest.jsonl").exists()

    # second run: redoes nothing, breaks nothing
    mtime = video_md.stat().st_mtime
    stats2 = _run(course, tmp_path)
    assert stats2["done"] == 3 and stats2["errors"] == 0
    assert video_md.stat().st_mtime == mtime


def test_subs_first(course, tmp_path):
    (course / "01 Модуль" / "[SWBAND.CO] Урок 1.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\ntext from subtitles\n", encoding="utf-8")
    _run(course, tmp_path)
    md = (tmp_path / "out" / "Тестовый Курс" / "text" / "01 Модуль" / "Урок 1.mp4.md")
    body = md.read_text(encoding="utf-8")
    assert "method: subs" in body and "text from subtitles" in body


def test_index_reports_word_count_and_an_empty_pdf(course, tmp_path):
    """Incident 2026-08-20: in INDEX.md a scanned pdf with no text layer (8 words)
    was indistinguishable from a 2000 word slide deck - both were `- [x]`. The
    consumer of the dump judged the material by its extension and silently threw
    away the meaningful slides of a course. Now every entry carries its word count
    and the empty ones are collected in a section of their own."""
    import fitz
    doc = fitz.open()
    doc.new_page()  # a page with no text - exactly a scan without a text layer
    doc.save(course / "02 Материалы" / "scan.pdf")
    doc = fitz.open()
    page = doc.new_page()
    # Latin on purpose: the base fitz font has no Cyrillic glyphs and draws dots
    for i in range(20):  # deliberately above the EMPTY_TEXT_WORDS threshold
        page.insert_text((72, 72 + i * 12), f"slide line number {i} with real content")
    doc.save(course / "02 Материалы" / "slides.pdf")
    _run(course, tmp_path)
    index = (tmp_path / "out" / "Тестовый Курс" / "INDEX.md").read_text(encoding="utf-8")

    line = next(x for x in index.splitlines() if "[slides.pdf](" in x)
    assert int(line.split(" - ")[1].split()[0]) >= EMPTY_TEXT_WORDS
    assert "NO TEXT" not in line
    empty = index.split("## Extracted with no usable text")[1]
    assert "scan.pdf" in empty
    assert "slides.pdf" not in empty  # a non-empty file stays out of the empty section


def test_error_isolation(course, tmp_path):
    (course / "02 Материалы" / "broken.pdf").write_bytes(b"not a pdf")
    stats = _run(course, tmp_path)
    assert stats["errors"] == 1
    assert stats["done"] == 3  # the rest are done
    cdir = tmp_path / "out" / "Тестовый Курс"
    assert (cdir / "errors.jsonl").exists()
    assert "broken" in (cdir / "INDEX.md").read_text(encoding="utf-8")


def test_rich_markup_in_foreign_names_is_escaped(tmp_path, monkeypatch, capsys):
    """A foreign string in the rich markup language (docs/DESIGN.md): '[dim]' in a
    name was silently eaten by the markup, and '[/]' breaks rendering with a
    MarkupError - after which the item error does not even reach errors.jsonl, so
    error isolation is gone. Checked against REAL rich, not our own imitation."""
    from coursedump import executor

    # '[/]' cannot appear in a file NAME (it is the path separator), so the closing
    # tag arrives from where it arrives in real life - the exception text
    root = tmp_path / "[SW] course [dim]"
    root.mkdir()
    (root / "lesson [SW].txt").write_text("body", encoding="utf-8")
    (root / "broken [dim].txt").write_text("body", encoding="utf-8")

    real = executor.extract

    def boom(it, path, asr):
        if "broken" in it.rel:
            raise RuntimeError("could not parse [/] the [dim] item")
        return real(it, path, asr)

    monkeypatch.setattr(executor, "extract", boom)
    stats = run_course(str(root), Opts(out_root=tmp_path / "out", asr_backend="dummy"))
    out = capsys.readouterr().out

    # rendering survived, the error is isolated and recorded
    assert stats["errors"] == 1 and stats["done"] == 1
    from pathlib import Path as P
    assert (P(stats["course"]) / "errors.jsonl").exists()
    # foreign markup shows up as it is, neither swallowed nor executed
    assert "broken [dim].txt" in out
    assert "could not parse [/] the [dim] item" in out
    assert "course [dim]" in out        # the course title in the run header


def test_log_line_per_item_when_not_terminal(tmp_path, monkeypatch, capsys):
    """In a log (non-terminal output) the live Progress block is not drawn: it
    collapses into a single line at the end of the course and the log stays silent
    for the whole run. A scheduler watchdog (stall_after=90m) looks at the log
    mtime and once declared a stall on a live course that was at 125/129 at that
    moment. So: one line per file, whole (never wrap a long name - that breaks
    grep over the log)."""
    from rich.console import Console

    from coursedump import executor

    root = tmp_path / "course"
    root.mkdir()
    long_name = "Lesson about " + "considerable length " * 8 + "and more.txt"
    (root / long_name).write_text("body", encoding="utf-8")
    (root / "second.txt").write_text("body", encoding="utf-8")

    run_course(str(root), Opts(out_root=tmp_path / "out", asr_backend="dummy"))
    lines = capsys.readouterr().out.splitlines()
    marked = [ln for ln in lines if re.match(r"^\[\d\d:\d\d] \d/2 ", ln)]

    assert len(marked) == 2                                  # one line per file
    assert any(ln.endswith(long_name) for ln in marked)      # the name is not broken up

    # a live terminal already shows this in the progress bar itself
    monkeypatch.setattr(executor, "console", Console(force_terminal=True, width=120))
    run_course(str(root), Opts(out_root=tmp_path / "out2", asr_backend="dummy"))
    assert not [ln for ln in capsys.readouterr().out.splitlines()
                if re.match(r"^\[\d\d:\d\d] \d/2 ", ln)]


def test_normalize_and_collisions():
    assert normalize.clean_component("[SWBAND.CO] Lesson.mp4") == "Lesson.mp4"
    assert normalize.is_blacklisted("x/Инфо-бизнесмены хватаются за головы.mp4")
    items = manifest.finalize([
        manifest.Item(rel="[A] lesson.mp4", kind="video"),
        manifest.Item(rel="[B] lesson.mp4", kind="video"),
    ])
    targets = {it.target for it in items}
    assert len(targets) == 2  # the collision created by tag stripping is resolved


def test_parse_subs_vtt():
    vtt = "WEBVTT\n\n00:00.000 --> 00:01.000\n<v Speaker>Hello</v>\nHello\n\n00:01.000 --> 00:02.000\nworld\n"
    assert parse_subs(vtt) == "Hello\nworld"


def test_collision_target_stable_across_runs(tmp_path):
    """A name collision: the target has to stay stable across runs, otherwise a
    resume re-extracts the item and leaves an orphan behind."""
    root = tmp_path / "course"
    (root / "01").mkdir(parents=True)
    (root / "01" / "[A] lesson.txt").write_text("A", encoding="utf-8")
    (root / "01" / "[B] lesson.txt").write_text("B", encoding="utf-8")
    out = tmp_path / "out"

    run_course(str(root), Opts(out_root=out, asr_backend="dummy"))
    text = out / "course" / "text" / "01"
    files1 = sorted(p.name for p in text.iterdir())
    assert len(files1) == 2

    run_course(str(root), Opts(out_root=out, asr_backend="dummy"))
    files2 = sorted(p.name for p in text.iterdir())
    assert files1 == files2  # no orphans, the names did not drift


def test_slug_collision_separates_courses(tmp_path):
    """Two different sources with the same folder name must not merge into one course."""
    a = tmp_path / "A" / "Flagship"; a.mkdir(parents=True)
    b = tmp_path / "B" / "Flagship"; b.mkdir(parents=True)
    (a / "l.txt").write_text("course A", encoding="utf-8")
    (b / "l.txt").write_text("course B", encoding="utf-8")
    out = tmp_path / "out"

    run_course(str(a), Opts(out_root=out, asr_backend="dummy"))
    run_course(str(b), Opts(out_root=out, asr_backend="dummy"))

    dirs = sorted(p for p in out.iterdir() if p.is_dir())
    assert len(dirs) == 2
    bodies = {(d / "text" / "l.txt.md").read_text(encoding="utf-8").strip().splitlines()[-1]
              for d in dirs}
    assert bodies == {"course A", "course B"}  # the content did not get mixed up


def test_legacy_relative_source_json_resumes_same_course(tmp_path):
    """A snapshot written before 2026-08: source.json keeps a relative `source`
    (in/<slug>/) and an absolute `root`. A repeated run given the absolute path
    must continue THIS course instead of starting a second one with a hash suffix
    (queue incident: a finished course was transcribed again into
    out/<slug>-<hash>)."""
    import json
    src = tmp_path / "in" / "course"; src.mkdir(parents=True)
    (src / "l.txt").write_text("lesson", encoding="utf-8")
    out = tmp_path / "out"
    run_course(str(src), Opts(out_root=out, asr_backend="dummy"))
    (course_dir,) = [p for p in out.iterdir() if p.is_dir()]
    sj = course_dir / "source.json"
    st = json.loads(sj.read_text(encoding="utf-8"))
    st["source"] = "in/course/"
    st["root"] = str(src.resolve())
    sj.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

    found, _ = executor.saved_state(out, str(src))
    assert found == course_dir
    run_course(str(src), Opts(out_root=out, asr_backend="dummy"))
    assert [p.name for p in out.iterdir() if p.is_dir()] == [course_dir.name]


def test_read_text_best_cp1251(tmp_path):
    from coursedump.util import read_text_best
    p = tmp_path / "s.srt"
    p.write_bytes("привет мир".encode("cp1251"))
    assert read_text_best(p) == "привет мир"


def test_collapse_repeats():
    from coursedump.extractors.asr import collapse_repeats
    # a Whisper hallucination: a long loop of one word -> collapse it to 3
    assert collapse_repeats("see you " + "Buy " * 200) == "see you Buy Buy Buy"
    # a repeated phrase
    assert collapse_repeats("no way " * 5).strip() == "no way no way no way"
    # a real repetition (below trigger) is left alone
    assert collapse_repeats("very very good") == "very very good"
    assert collapse_repeats("this is just text") == "this is just text"


def _wall(n, text, gap=0.0, dur=2.0):
    """n consecutive segments carrying the same text."""
    from coursedump.extractors.asr import Segment
    return [Segment(i * (dur + gap), i * (dur + gap) + dur, text) for i in range(n)]


def test_collapse_survives_paragraph_split():
    """A real wall of repeats longer than one paragraph. PARAGRAPH_MAX cuts the
    hallucination into dozens of paragraphs before collapse_repeats sees it, and
    each one is collapsed on its own
    (measured on an internal run: a 19,662-segment run became 138 without this stage).
    """
    from coursedump.extractors.asr import Result
    body = Result(segments=_wall(2000, "yeah yeah yeah yeah")).markdown_body()
    assert body.split().count("yeah") <= 20
    assert "yeah" in body  # compressed, not erased: the hallucination stays visible


def test_collapse_paragraph_runs_on_gaps():
    """The same class, but the segments are separated by pauses: every repeat is its
    own paragraph and in-paragraph collapsing has nothing to grip."""
    from coursedump.extractors.asr import Result
    body = Result(segments=_wall(120, "Bye.", gap=3.0)).markdown_body()
    assert body.count("Bye.") == 3


def test_collapse_punctuation_wall():
    """Observed case from an internal transcript run: 252 consecutive segments
    holding a single "!". A unit made of punctuation normalized to nothing and was
    skipped, so the wall survived BOTH collapsing stages. On the real fragment:
    the worst run went 252 -> 9 and the count of "!" went 256 -> 12."""
    from coursedump.extractors.asr import Result, collapse_repeats
    assert collapse_repeats(" ".join(["!"] * 252)) == "! ! !"

    body = Result(segments=_wall(252, "!")).markdown_body()
    assert 0 < body.count("!") <= 12  # compressed, but the trace stays visible

    # real punctuation is untouched: fewer repeats than the trigger
    assert collapse_repeats("yes! yes! yes! hooray") == "yes! yes! yes! hooray"
    assert collapse_repeats("What?! Really?") == "What?! Really?"


def test_collapse_paragraph_runs_keeps_live_repeats():
    """Fewer identical paragraphs in a row than the trigger is live speech."""
    from coursedump.extractors.asr import collapse_para_runs
    assert collapse_para_runs(["yes", "yes", "yes", "no"]) == ["yes", "yes", "yes", "no"]
    assert collapse_para_runs(["a", "b", "a", "b"]) == ["a", "b", "a", "b"]


def test_html_extracted(tmp_path):
    """Lesson HTML pages (the written layer of a course) must become text."""
    root = tmp_path / "course"
    root.mkdir()
    (root / "lesson.html").write_text(
        "<html><body><h1>Lesson title</h1><p>Lesson body with text and code.</p></body></html>",
        encoding="utf-8")
    out = tmp_path / "out"
    run_course(str(root), Opts(out_root=out, asr_backend="dummy"))
    md = out / "course" / "text" / "lesson.html.md"
    assert md.exists()
    assert "Lesson body with text" in md.read_text(encoding="utf-8")
