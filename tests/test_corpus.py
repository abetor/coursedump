"""Boosty corpus output: deduplication against an existing corpus, plus the
file format.

Two acceptance blockers:
(a) what is already downloaded is not downloaded again - the dedup key is the
    post UUID out of the `# source:` header, and the verdict is decided BEFORE
    the platform is touched;
(b) the corpus file keeps the shape the historical shell wrapper produced.
    "Byte for byte" is proven here for the WRAPPER (header, blank line, file
    names); the body is our own text (paragraphs plus collapsing), and identical
    line breaking against the old corpus is not promised.

Tests use only temporary directories and never touch a real corpus.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from coursedump import cli, corpus, executor, manifest, sources

POST = "https://boosty.to/demo-creator/posts/ce473f44-e17e-4d36-b919-4779192d39b7"
UUID = "ce473f44-e17e-4d36-b919-4779192d39b7"
POST2 = "https://boosty.to/demo-creator/posts/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
# a post from ANOTHER blog: one queue can in principle mix authors
POST_OTHER = "https://boosty.to/mentor/posts/11111111-2222-3333-4444-555555555555"
META = "Four hundred a day. A call from a recruiter. | interview,$10k+ | Dec 11, 2023 | Advanced"


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """Tests never sleep: the boosty profile is on by default and its pause
    between downloads is 10-20 minutes (same argument as the slept fixture in
    tests/test_sources.py: a forgotten injection is not a slow test, it is a
    hung run)."""
    pauses: list[float] = []
    monkeypatch.setattr(sources, "_sleep", pauses.append)
    return pauses


def _corpus_file(root, name, url, meta="meta | tags | Dec 11, 2023 | Advanced",
                 body="text\n"):
    """A corpus file in exactly the shape the historical shell wrapper left."""
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_text(f"{corpus.HEAD_SRC}{url}\n{corpus.HEAD_META}{meta}\n\n{body}",
                 encoding="utf-8")
    return p


def _queue(tmp_path, *lines, name="urls.txt"):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _staging(tmp_path, blog="demo-creator"):
    """Default place for corpus working snapshots: <data>/staging/boosty-<blog>/."""
    return tmp_path / "data" / "staging" / f"boosty-{blog}"


def _course_dir(tmp_path, *titles, name="course"):
    """A finished course snapshot on disk: manifest plus extracted md, as after run."""
    d = tmp_path / name
    (d / "text").mkdir(parents=True, exist_ok=True)
    items = []
    for i, t in enumerate(titles, 1):
        it = manifest.Item(rel=f"{i:03d} - {t} [v{i}]", kind="video", index=i,
                           title=t, target=f"{i:03d} - {t}.md")
        (d / "text" / it.target).write_text(
            f"---\nsource: {it.rel}\n---\n# {it.rel}\n\nbody {i}\n", encoding="utf-8")
        items.append(it)
    manifest.save(items, d / "manifest.jsonl")
    return d


# ------------------------------------------------------------------ queue

def test_queue_is_read_like_batch_sh(tmp_path):
    """`while read -r url rest`: comments and blank lines are skipped, and the
    metadata is the whole rest of the line without leading or trailing spaces
    (read itself eats those; keeping them would misalign our header against the
    corpus for no reason)."""
    q = _queue(tmp_path,
               "# comment",
               "",
               f"{POST}  {META}   ",
               f"{POST}\tmeta\twith\ttabs | x | Dec 11, 2023 | Free",
               "https://boosty.to/x/posts/no-meta-here")
    lines = corpus.read_queue(q)

    assert [ln.lineno for ln in lines] == [3, 4, 5]
    assert lines[0].url == POST and lines[0].meta == META
    assert lines[1].meta == "meta\twith\ttabs | x | Dec 11, 2023 | Free"
    assert lines[2].meta == ""


def test_line_without_date_is_refused_before_any_download(tmp_path):
    """A date is mandatory (the freshness rule for digests): a line without one is
    refused, not turned into a quiet corpus file with no age."""
    assert corpus.meta_problem(META) == ""
    assert corpus.meta_problem("title | tags | 2026-08-10 | Free") == ""
    # live lines do use a fifth field ("| part 1" appears in 9 corpus files)
    assert corpus.meta_problem("title | tags | Dec 09, 2024 | Free | part 1") == ""
    assert "not a calendar date" in corpus.meta_problem("title | tags | | Advanced")
    assert "metadata is missing" in corpus.meta_problem("")

    q = _queue(tmp_path, f"{POST}  title | tags | | Advanced")
    res = CliRunner().invoke(cli.app, ["corpus", str(q), str(tmp_path / "korpus")])
    assert res.exit_code == 1, res.output
    assert "ERROR line 1" in res.output


@pytest.mark.parametrize("meta, why", [
    # a date "somewhere in the line" is not the date of the post
    ("Release 2026-08-10 | news | | Free", "date in the title, date field empty"),
    ("title | 2024,Dec 09, 2024 | | Free", "date in the tags"),
    ("title | tags | yesterday | Free", "the date field is not a date"),
    ("title | tags | 2026-02-30 | Free", "no such calendar day"),
    ("title | tags | 2026-13-01 | Free", "there is no month 13"),
    ("title | tags | Dez 09, 2024 | Free", "month not recognised"),
    ("title | tags | Dec 09, 2024", "fewer than four fields"),
    ("title | Dec 09, 2024 | tags | Free", "date in the wrong field"),
])
def test_date_is_checked_in_the_third_field_only(tmp_path, meta, why):
    """The date is checked IN ITS FIELD, not by searching the whole line.
    Otherwise a year in the title or a '2024' tag passed for the post date and a
    line with no age went off to the platform."""
    assert corpus.meta_problem(meta), why

    q = _queue(tmp_path, f"{POST}  {meta}")
    res = CliRunner().invoke(cli.app, ["corpus", str(q), str(tmp_path / "korpus")])
    assert res.exit_code == 1, res.output
    assert "ERROR line 1" in res.output
    assert not (tmp_path / "korpus").exists(), "a refused line created the corpus folder"


def test_date_forms_of_the_live_queue_are_accepted():
    """The forms that actually occur in the queue and in corpus headers."""
    for good in ("Dec 09, 2024", "Dec 9 2024", "December 09, 2024", "2026-08-10",
                 " Dec 09, 2024 "):
        assert corpus.is_date(good), good
    for bad in ("", "2024", "Dec 2024", "09.12.2024", "2026-08-10 (reupload)",
                "video: youtube"):
        assert not corpus.is_date(bad), bad


# -------------------------------------------------------------- dedup key

@pytest.mark.parametrize("url", [
    POST,
    POST + "?share=post_link",
    POST + "/",
    POST.replace("https://", "http://"),
    POST.replace(UUID, UUID.upper()),
    POST.replace("boosty.to", "www.boosty.to"),
    POST.replace("boosty.to", "boosty.to."),
])
def test_post_uuid_is_the_key_not_the_shape_of_the_link(url):
    """The key is the post UUID: a variation of the link (share, trailing slash,
    http, case) does not turn a downloaded post into a "new" one. A file name
    cannot be the key - two different posts with the same video title gave the
    old shell script a false SKIP."""
    assert corpus.post_uuid(url) == UUID


@pytest.mark.parametrize("url", [
    "https://youtu.be/VaHTfjlEoAc?t=10",
    f"https://youtu.be/{UUID}",
    f"https://boosty.to/demo-creator/video/{UUID}",
    f"https://boosty.to/demo-creator/posts/not-a-uuid?uuid={UUID}",
])
def test_queue_line_without_a_boosty_post_url_is_refused_fail_closed(tmp_path, url):
    """There is no "key = URL" fallback any more. Such a key ended up in the name
    `Video-youtu.be/id` (a slash makes a nested path instead of a flat .txt) and
    broke the promise of UUID deduplication. We do not invent a separate contract
    for generic URLs - the command refuses BEFORE downloading."""
    assert corpus.post_uuid(url) == ""
    assert "boosty.to" in corpus.url_problem(url)
    assert corpus.url_problem(POST) == ""

    q = _queue(tmp_path, f"{url}  {META}")
    res = CliRunner().invoke(cli.app, ["corpus", str(q), str(tmp_path / "korpus")])
    assert res.exit_code == 1, res.output
    assert "ERROR line 1" in res.output and "boosty.to" in res.output
    assert not (tmp_path / "korpus").exists()


def test_one_bad_line_rejects_the_whole_queue_before_network(tmp_path, monkeypatch):
    """A mixed queue is atomic at preflight: a valid NEW line next to a broken one
    must not even reach the yt-dlp metadata resolve."""
    calls = []
    monkeypatch.setattr(sources, "run", lambda *a, **kw: calls.append((a, kw)))
    q = _queue(tmp_path, f"{POST}  {META}",
               "https://youtu.be/VaHTfjlEoAc  broken | x | Dec 11, 2023 | Free")
    data = tmp_path / "data"
    root = tmp_path / "korpus"

    res = CliRunner().invoke(cli.app, ["corpus", str(q), str(root),
                                       "--data", str(data), "--asr", "dummy"])

    assert res.exit_code == 1, res.output
    assert "NEW" in res.output and "ERROR line 2" in res.output
    assert calls == [], "a broken line did not stop the valid post before the network"
    assert not data.exists() and not root.exists()


def test_corpus_header_without_a_post_uuid_stops_the_run(tmp_path):
    """A header with no UUID is a foreign contract inside the corpus folder: UUID
    deduplication does not work on it, so the command refuses outright instead of
    quietly calling the post new."""
    root = tmp_path / "korpus"
    _corpus_file(root, "foreign.txt", "https://youtu.be/VaHTfjlEoAc")
    ledger = corpus.Ledger.scan(root)
    assert [n.split(":")[0] for n in ledger.no_uuid] == ["foreign.txt"]
    assert ledger.by_key == {} and ledger.files == 1

    q = _queue(tmp_path, f"{POST}  {META}")
    res = CliRunner().invoke(cli.app, ["corpus", str(q), str(root), "--dry-run"])
    assert res.exit_code == 1, res.output
    assert "headers without a post UUID" in res.output and "foreign.txt" in res.output
    assert "NEW" not in res.output


# ------------------------------------------------------- ledger and verdict

def test_verdict_done_names_what_matched(tmp_path):
    """The verdict comes with evidence: which UUID and which exact file matched."""
    root = tmp_path / "korpus"
    _corpus_file(root, "Four hundred a day. A call from a recruiter..txt", POST)
    ledger = corpus.Ledger.scan(root)

    v = ledger.verdict(POST + "?share=post_link")
    assert v.done and v.key == UUID
    assert UUID in v.why and "Four hundred a day" in v.why


def test_verdict_new_says_what_was_checked(tmp_path):
    """Control case: "found nothing" must differ from "did not look"."""
    root = tmp_path / "korpus"
    _corpus_file(root, "foreign.txt", "https://boosty.to/demo-creator/posts/"
                                      "11111111-2222-3333-4444-555555555555")
    v = corpus.Ledger.scan(root).verdict(POST)

    assert not v.done and v.status == "NEW"
    assert UUID in v.why and "1 files" in v.why


def test_file_without_header_is_reported_not_silently_ignored(tmp_path):
    """A file without a header is invisible to deduplication - that has to be SAID,
    otherwise the post is downloaded again silently."""
    root = tmp_path / "korpus"
    root.mkdir()
    (root / "manual.txt").write_text("just text without a header\n", encoding="utf-8")
    _corpus_file(root, "normal.txt", POST)

    ledger = corpus.Ledger.scan(root)
    assert ledger.headless == ["manual.txt"]
    assert ledger.files == 2

    q = _queue(tmp_path, f"{POST}  {META}")
    res = CliRunner().invoke(cli.app, ["corpus", str(q), str(root)])
    assert "manual.txt" in res.output and "without a header" in res.output


def test_scan_survives_a_file_that_vanished(tmp_path):
    """A live batch is running alongside: a file can disappear between the glob and
    the read. The scan has to finish instead of taking the whole preflight down."""
    root = tmp_path / "korpus"
    _corpus_file(root, "live.txt", POST)
    (root / "gone.txt").symlink_to(root / "no-such-file.txt")

    ledger = corpus.Ledger.scan(root)
    assert ledger.verdict(POST).done
    assert ledger.unreadable == []      # "no such file" is not a read error


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores read permissions")
def test_unreadable_corpus_file_stops_the_run_before_any_verdict(tmp_path):
    """The file EXISTS but cannot be read (permissions, I/O) - the UUID we are
    looking for may be inside it. Calling such a post NEW and going to the platform
    is not allowed: the run fails before the todo list is built instead of quietly
    changing the verdict."""
    root = tmp_path / "korpus"
    closed = _corpus_file(root, "closed.txt", POST)
    closed.chmod(0o000)
    try:
        ledger = corpus.Ledger.scan(root)
        assert [u.split(":")[0] for u in ledger.unreadable] == ["closed.txt"]
        assert ledger.by_key == {} and ledger.headless == []
        assert ledger.files == 1        # the file is there, it just was not read

        q = _queue(tmp_path, f"{POST}  {META}", f"{POST2}  {META}")
        res = CliRunner().invoke(cli.app, ["corpus", str(q), str(root), "--dry-run"])
        assert res.exit_code == 1, res.output
        assert "unreadable corpus files" in res.output
        assert "closed.txt" in res.output
        assert "NEW" not in res.output, "a verdict on an incomplete ledger is false"
    finally:
        closed.chmod(0o644)


def test_preflight_says_done_and_new_on_a_real_shaped_queue(tmp_path):
    """End-to-end preflight: a queue of two posts, one already in the corpus."""
    root = tmp_path / "korpus"
    _corpus_file(root, "done.txt", POST)
    q = _queue(tmp_path, f"{POST}  {META}", f"{POST2}  {META}")

    res = CliRunner().invoke(cli.app, ["corpus", str(q), str(root), "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "DONE 1" in res.output and "NEW 1" in res.output
    assert "done.txt" in res.output             # the evidence is in the output


def test_missing_queue_is_an_error_not_an_empty_run(tmp_path):
    res = CliRunner().invoke(cli.app, ["corpus", str(tmp_path / "missing.txt"),
                                       str(tmp_path / "korpus")])
    assert res.exit_code == 1 and "queue not found" in res.output


# ---------------------------------------------------------------- file format

# Execute the historical shell wrapper with a real shell so the test covers
# shell semantics rather than a handwritten imitation. The public suite owns
# this contract and never reads a private external reference file.
_BATCH_SH_GROUP = ('{ echo "# source: $url"; [[ -n "${rest:-}" ]] && '
                   'echo "# metadata: $rest"; echo; cat "$txt"; }')


def _batch_sh_bytes(tmp_path, url, meta, body) -> bytes:
    """Render URL, metadata, and transcript through the shell wrapper."""
    txt, dst = tmp_path / "whisper.txt", tmp_path / "batch-sh.txt"
    txt.write_text(body, encoding="utf-8")
    subprocess.run(["bash", "-c", f'{_BATCH_SH_GROUP} > "$dst"'], check=True,
                   env={"PATH": os.environ["PATH"], "url": url, "rest": meta,
                        "txt": str(txt), "dst": str(dst)})
    return dst.read_bytes()

@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.parametrize("meta", [META, ""])
def test_wrapper_is_byte_for_byte_the_batch_sh_one(tmp_path, meta):
    """Blocker (b): not an md snapshot but the same .txt. The reference is not the
    text of this test but the output of the shell wrapper itself (with no metadata
    it writes no '# metadata:' line, and that is part of the reference too).

    WHAT IS PROVEN: the WRAPPER - header, blank line and the bytes around the
    body. The body is fed in identically on both sides, so this test proves
    nothing about the REAL body of a new file (our paragraphs split on pauses plus
    collapsing, against the line-by-line output of mlx_whisper). That boundary is
    written down in docs/DESIGN.md.
    """
    body = "first line\nsecond line\n"          # mlx_whisper ends with a single \n
    assert corpus.render(POST, meta, body).encode("utf-8") == \
        _batch_sh_bytes(tmp_path, POST, meta, body)


def test_render_normalises_the_tail_to_one_newline():
    """Our one deviation from `cat`: the tail of the body is normalised to exactly
    one newline (the shell wrapper had the same tail because whisper handed it over
    that way; our body is assembled in code, where nothing guarantees it)."""
    head = f"# source: {POST}\n# metadata: {META}\n"
    for body in ("text", "text\n", "text\n\n\n"):
        assert corpus.render(POST, META, body) == f"{head}\ntext\n"
    assert corpus.render(POST, META, "") == f"{head}\n"   # silent video: header still there


def test_body_is_the_transcript_without_frontmatter_and_title():
    md = ("---\nsource: 001 - Talk [a1]\nmethod: asr\ncreated: 2026-08-10T00:00:00\n"
          "---\n# 001 - Talk [a1]\n\nbody\n\nsecond paragraph\n")
    assert corpus.md_body(md) == "body\n\nsecond paragraph"


# -------------------------------------------------------------- file name

def test_stem_is_made_by_the_ytdlp_engine_not_by_our_own_cleanup():
    """The corpus file name is the name yt-dlp itself would produce
    (`-o '%(title)s...'`), that is '/' -> '⧸' and ':' -> '：'. Checked against the
    REAL engine: a hand-written list of bad characters is always incomplete. The
    title keeps non-ASCII characters, which must survive the sanitiser."""
    from yt_dlp import YoutubeDL

    title = 'Golang/PHP удаленка: 7к+ | "тест" <a>? *x'
    ydl = YoutubeDL({"outtmpl": "/tmp/%(title)s.%(ext)s", "quiet": True})
    expect = Path(ydl.prepare_filename({"title": title, "ext": "txt"})).stem

    assert corpus.file_stem(title, UUID) == expect
    assert "⧸" in expect and "/" not in expect


def test_real_title_is_not_stripped_like_a_rel():
    """Stripping 'NNN - ' and ' [id]' applies only to the FALLBACK name built from
    rel: on a real title it would eat part of the name."""
    assert corpus.file_stem("100 - best questions [top]", UUID) == "100 - best questions [top]"
    assert corpus.rel_title("001 - Talk [v1]") == "Talk"


def test_generic_video_title_gets_the_post_uuid():
    """Rule inherited from the shell script: 'Video' collides across posts (8 such
    files in the corpus), so the post id makes it unique."""
    assert corpus.file_stem("Video", UUID) == f"Video-{UUID}"


def test_multivideo_post_numbers_parts_like_the_corpus():
    """The corpus convention for a post with N videos."""
    assert corpus.file_stem("Talk", UUID, part=2, total=2) == "Talk - part 2"
    assert corpus.file_stem("Talk", UUID, part=1, total=1) == "Talk"


def test_single_video_title_that_looks_like_a_later_part_is_a_completion_proof(tmp_path):
    """A natural single-video title that ends in a part suffix must not become a
    permanent NEW: export tells it apart with a UUID tail without touching the
    corpus header. The legacy Russian part marker is the case that occurs in the
    live corpus."""
    course = _course_dir(tmp_path, "Talk - часть 2")
    root = tmp_path / "korpus"

    written, missing = corpus.export(course, corpus.Line(1, POST, META), root)

    assert missing == []
    assert [p.name for p in written] == ["Talk - часть 2 [ce473f44].txt"]
    assert corpus.is_completion_proof(written[0].name)
    assert corpus.Ledger.scan(root).verdict(POST).done
    assert written[0].read_text(encoding="utf-8").startswith(
        f"{corpus.HEAD_SRC}{POST}\n{corpus.HEAD_META}{META}\n\n")

    repeated, missing = corpus.export(course, corpus.Line(1, POST, META), root)
    assert missing == [] and repeated == written
    assert [p.name for p in root.glob("*.txt")] == [written[0].name]


@pytest.mark.parametrize("name", [
    "Old single video post.txt",
    "Old multivideo post - часть 1.txt",
    "Old multivideo post - часть 1 (ce473f44).txt",
])
def test_old_completion_proof_names_stay_done(tmp_path, name):
    """The new disambiguation applies only on export and never takes DONE away from
    old single-file entries or from the first parts already in the live corpus,
    which carry the Russian part marker."""
    root = tmp_path / "korpus"
    _corpus_file(root, name, POST)
    assert corpus.Ledger.scan(root).verdict(POST).done


def test_long_title_fits_the_filesystem_limit(tmp_path):
    """A deviation from the shell script, where a long title killed the download
    with ENAMETOOLONG. The filler is a two-byte character: the filesystem limit is
    counted in bytes, not characters."""
    stem = corpus.file_stem("Я" * 300, UUID, part=1, total=2)
    assert stem.endswith(" - part 1")
    assert len((stem + ".txt").encode("utf-8")) <= corpus.MAX_NAME_BYTES
    (tmp_path / f"{stem}.txt").write_text("ok", encoding="utf-8")   # the filesystem took it


def test_name_collision_with_another_post_does_not_overwrite(tmp_path):
    """Two different posts with the same video title - the real case behind a false
    SKIP in the old shell script. The other post's file is not overwritten."""
    root = tmp_path / "korpus"
    _corpus_file(root, "Talk.txt", POST)
    p = corpus.target_path(root, "Talk", corpus.post_uuid(POST2))

    assert p.name == "Talk (aaaaaaaa).txt"
    assert (root / "Talk.txt").read_text(encoding="utf-8").startswith(
        f"{corpus.HEAD_SRC}{POST}")


# --------------------------------------------------------- end-to-end run

def _fake_ytdlp(monkeypatch, entries_by_url, fail_on=()):
    """yt-dlp on a post: -J returns entries, a download drops a file. argv is logged."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        url = cmd[-1]
        entries = entries_by_url[url]
        if "-J" in cmd:
            return type("P", (), {"returncode": 0, "stderr": "", "stdout": json.dumps(
                {"title": f"post {corpus.post_uuid(url)[:8]}", "_type": "playlist",
                 "id": corpus.post_uuid(url)[:8], "webpage_url": url,
                 "entries": entries})})()
        e = entries[int(cmd[cmd.index("--playlist-items") + 1]) - 1]
        if e["id"] in fail_on:
            return type("P", (), {"returncode": 1, "stdout": "",
                                  "stderr": "403 Forbidden"})()
        path = Path(cmd[cmd.index("-o") + 1][: -len(".%(ext)s")].replace("%%", "%") + ".m4a")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        return type("P", (), {"returncode": 0, "stdout": e["id"] + "\n", "stderr": ""})()

    monkeypatch.setattr(sources, "run", fake_run)
    monkeypatch.setattr(cli, "_caffeinate", lambda: None)
    return calls


def _entries(post, *titles):
    return [{"title": t, "id": f"v{i}", "playlist_index": i, "webpage_url": post,
             "duration": 100, "url": f"https://cdn.example.test/media/{i}.mp4"}
            for i, t in enumerate(titles, 1)]


def _corpus_run(tmp_path, queue_lines, entries_by_url, monkeypatch, fail_on=(),
                extra=()):
    calls = _fake_ytdlp(monkeypatch, entries_by_url, fail_on=fail_on)
    q = _queue(tmp_path, *queue_lines)
    res = CliRunner().invoke(cli.app, [
        "corpus", str(q), str(tmp_path / "korpus"),
        "--data", str(tmp_path / "data"), "--asr", "dummy", *extra])
    return res, calls


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe required")
def test_post_goes_into_the_corpus_and_is_not_downloaded_twice(tmp_path, monkeypatch):
    """Both blockers in one run: a file in corpus format inside the corpus folder,
    and a repeat run that does not touch the platform AT ALL (not one yt-dlp call).

    The slash in the title is deliberate: in a raw file name '/' becomes a space
    for us (it is a path separator), while the corpus convention is yt-dlp's '⧸'.
    So the raw title has to reach the corpus THROUGH the manifest instead of being
    rebuilt from rel."""
    entries = {POST: _entries(POST, "Golang/PHP удаленка")}
    res, calls = _corpus_run(tmp_path, [f"{POST}  {META}"], entries, monkeypatch)
    assert res.exit_code == 0, res.output

    txt = tmp_path / "korpus" / "Golang⧸PHP удаленка.txt"
    assert txt.read_text(encoding="utf-8") == (
        f"# source: {POST}\n# metadata: {META}\n\n"
        "[dummy transcript of 001 - Golang PHP удаленка [v1].m4a]\n")

    calls.clear()
    again, _ = _corpus_run(tmp_path, [f"{POST}  {META}"], entries, monkeypatch)
    assert again.exit_code == 0, again.output
    assert calls == [], "an already downloaded post went to the platform"
    assert "DONE" in again.output and "Golang⧸PHP удаленка.txt" in again.output


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe required")
def test_multivideo_post_gives_two_files_with_one_header(tmp_path, monkeypatch):
    """A post with N videos: N files, one shared header (it is one post) and names
    following the corpus convention. The old shell script could not do this at all."""
    res, _ = _corpus_run(tmp_path, [f"{POST}  {META}"],
                         {POST: _entries(POST, "Talk", "Talk")}, monkeypatch)
    assert res.exit_code == 0, res.output

    korpus = tmp_path / "korpus"
    names = sorted(p.name for p in korpus.glob("*.txt"))
    assert names == ["Talk - part 1.txt", "Talk - part 2.txt"]
    for name in names:
        assert (korpus / name).read_text(encoding="utf-8").startswith(
            f"# source: {POST}\n# metadata: {META}\n\n")
    # and deduplication sees both parts as one post
    assert corpus.Ledger.scan(korpus).verdict(POST).done


# --------------------------------- corpus staging (not the shared out/)

def test_blog_is_read_from_the_post_url_by_the_same_parser():
    """The blog name comes from the same URL parsing as the UUID: if it is not a
    boosty post URL there is no blog either."""
    assert corpus.post_blog(POST) == "demo-creator"
    assert corpus.post_blog(POST + "?share=post_link") == "demo-creator"
    assert corpus.post_blog(POST_OTHER) == "mentor"
    assert corpus.post_blog(f"https://youtu.be/{UUID}") == ""
    assert corpus.post_blog(f"https://boosty.to/demo-creator/video/{UUID}") == ""


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe required")
def test_staging_of_a_post_stays_out_of_the_course_out(tmp_path, monkeypatch):
    """The working snapshot of a post goes to <data>/staging/boosty-<blog>/ and the
    course out/ stays clean.

    A blog post is not a course: on a live machine out/ once held 115 directories
    (17 courses and 98 posts of one blog). Mixing them is not cosmetic:
    `coursedump run` with no arguments continues EVERYTHING in out/, so it would
    have pulled the platform for each of those 98 posts, and `status` printed 115
    lines."""
    res, _ = _corpus_run(tmp_path, [f"{POST}  {META}"],
                         {POST: _entries(POST, "Talk")}, monkeypatch)
    assert res.exit_code == 0, res.output
    assert (tmp_path / "korpus" / "Talk.txt").is_file()

    staged = [d for d in _staging(tmp_path).iterdir() if (d / "source.json").is_file()]
    assert len(staged) == 1, sorted(p.name for p in _staging(tmp_path).iterdir())
    assert json.loads((staged[0] / "source.json").read_text(encoding="utf-8"))["source"] == POST
    assert list((tmp_path / "data" / "out").iterdir()) == [], "a post landed in the course out/"


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe required")
def test_two_blogs_in_one_queue_get_their_own_staging(tmp_path, monkeypatch):
    """Staging is chosen PER LINE from that line's own url: a mixed queue puts posts
    into the folders of their own blogs, not of the first line in the file."""
    entries = {POST: _entries(POST, "Talk"),
               POST_OTHER: _entries(POST_OTHER, "Lecture")}
    res, _ = _corpus_run(tmp_path, [f"{POST}  {META}", f"{POST_OTHER}  {META}"],
                         entries, monkeypatch)
    assert res.exit_code == 0, res.output

    for blog in ("demo-creator", "mentor"):
        staged = [d for d in _staging(tmp_path, blog).iterdir()
                  if (d / "source.json").is_file()]
        assert len(staged) == 1, f"{blog}: {staged}"
    assert list((tmp_path / "data" / "out").iterdir()) == []


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe required")
def test_explicit_out_still_beats_the_staging_default(tmp_path, monkeypatch):
    """An explicit `--out` beats the default, among other things to move snapshots
    onto an external disk (docs/DESIGN.md, the alternative to --purge-video)."""
    mine = tmp_path / "my-disk"
    res, _ = _corpus_run(tmp_path, [f"{POST}  {META}"],
                         {POST: _entries(POST, "Talk")}, monkeypatch,
                         extra=("--out", str(mine)))
    assert res.exit_code == 0, res.output

    assert [d.name for d in mine.iterdir() if (d / "source.json").is_file()]
    assert not _staging(tmp_path).exists()


def test_course_scan_sees_the_nested_staging_but_not_the_inside_of_a_course(tmp_path):
    """The snapshot walk goes two levels deep: the flat out/ and the per-blog
    staging/boosty-<blog>/<post>/.

    If it failed to find a finished snapshot, `completed_course` would return None
    and a repeat run would go to the platform for it - the most expensive mistake
    here (an extra request plus hours of profile pauses). But no deeper either:
    rglob would descend into a course raw/ with thousands of files, and source.json
    does not live there."""
    root = tmp_path / "staging"
    flat = root / "course-alongside"
    nested = root / "boosty-demo-creator" / "post"
    deep = nested / "raw" / "nested folder"
    for d, source in ((flat, "/tmp/course"), (nested, POST), (deep, "level 3")):
        d.mkdir(parents=True)
        (d / "source.json").write_text(json.dumps({"source": source}),
                                       encoding="utf-8")

    found = {str(d) for d, _ in executor._course_states(root)}
    assert found == {str(flat), str(nested)}
    assert executor.saved_state(root, POST)[0] == nested
    assert sorted(s for s, _ in executor.known_courses(root)) == sorted(
        ["/tmp/course", POST])


def test_completed_course_finds_a_full_snapshot_two_levels_down(tmp_path):
    """The same walk seen from the corpus side: a complete post snapshot sitting in
    staging/boosty-<blog>/<post>/ must also be found through the staging parent,
    otherwise finishing a publication would download the post again."""
    staging = tmp_path / "staging"
    course = _course_dir(staging / "boosty-demo-creator", "Talk", name="post")
    (course / "source.json").write_text(json.dumps({"source": POST}), encoding="utf-8")

    assert executor.completed_course(staging / "boosty-demo-creator", POST) == course
    assert executor.completed_course(staging, POST) == course
    (course / "text" / "001 - Talk.md").unlink()
    assert executor.completed_course(staging, POST) is None


# ------------------------------------------- post level transaction

def test_post_with_only_a_later_part_is_not_done(tmp_path):
    """The completion evidence for a post is its FIRST part. Until that exists, the
    UUID in the header of a second part has no right to declare the whole post
    finished."""
    root = tmp_path / "korpus"
    _corpus_file(root, "Talk - part 2.txt", POST)
    v = corpus.Ledger.scan(root).verdict(POST)
    assert not v.done and "INCOMPLETE" in v.why

    _corpus_file(root, "Talk - part 1.txt", POST)
    assert corpus.Ledger.scan(root).verdict(POST).done
    # a name disambiguated from another post by a suffix is still evidence
    assert corpus.is_completion_proof("Talk - part 1 (aaaaaaaa).txt")
    assert not corpus.is_completion_proof("Talk - part 2 (aaaaaaaa).txt")


def test_export_publishes_nothing_when_a_part_is_missing(tmp_path):
    """A post-level transaction: if one part was not extracted, not a single part
    goes into the corpus. Otherwise the very first part carrying the UUID in its
    header means DONE forever."""
    course = _course_dir(tmp_path, "Talk", "Talk")
    (course / "text" / "002 - Talk.md").unlink()
    root = tmp_path / "korpus"
    root.mkdir()

    written, missing = corpus.export(course, corpus.Line(1, POST, META), root)
    assert written == [] and missing == ["002 - Talk [v2]"]
    assert list(root.glob("*")) == []


def test_the_completion_proof_is_written_last(tmp_path, monkeypatch):
    """ONE rename is atomic, but there are N parts: the first part (the evidence)
    has to be written last, otherwise a death between renames looks like a finished
    post."""
    course = _course_dir(tmp_path, "Talk", "Talk")
    order: list[str] = []
    real = corpus.atomic_write_text
    monkeypatch.setattr(corpus, "atomic_write_text",
                        lambda p, t: (order.append(p.name), real(p, t))[1])

    written, missing = corpus.export(course, corpus.Line(1, POST, META),
                                     tmp_path / "korpus")
    assert missing == []
    assert [p.name for p in written] == ["Talk - part 1.txt", "Talk - part 2.txt"]
    assert order == ["Talk - part 2.txt", "Talk - part 1.txt"]


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe required")
def test_partial_post_publishes_nothing_and_the_next_run_finishes_it(tmp_path, monkeypatch):
    """End-to-end regression: one video out of two failed, so the corpus stays
    empty, the post stays NEW and the next run finishes it by itself."""
    entries = {POST: _entries(POST, "Talk", "Talk")}
    korpus = tmp_path / "korpus"
    res, _ = _corpus_run(tmp_path, [f"{POST}  {META}"], entries, monkeypatch,
                         fail_on=("v2",))

    assert res.exit_code == 1, res.output
    assert "INCOMPLETE line 1" in res.output
    assert list(korpus.glob("*.txt")) == []
    assert not corpus.Ledger.scan(korpus).verdict(POST).done

    again, _ = _corpus_run(tmp_path, [f"{POST}  {META}"], entries, monkeypatch)
    assert again.exit_code == 0, again.output
    assert sorted(p.name for p in korpus.glob("*.txt")) == \
        ["Talk - part 1.txt", "Talk - part 2.txt"]
    assert corpus.Ledger.scan(korpus).verdict(POST).done


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe required")
def test_death_between_renames_is_finished_without_touching_the_platform(tmp_path, monkeypatch):
    """Publication broke between renames (no evidence, part 2 is on disk): the post
    is not DONE, and finishing it works from the snapshot already on disk - not one
    yt-dlp call, not even for metadata (resolve asks the source too)."""
    entries = {POST: _entries(POST, "Talk", "Talk")}
    korpus = tmp_path / "korpus"
    res, _ = _corpus_run(tmp_path, [f"{POST}  {META}"], entries, monkeypatch)
    assert res.exit_code == 0, res.output

    (korpus / "Talk - part 1.txt").unlink()        # death between renames
    assert not corpus.Ledger.scan(korpus).verdict(POST).done

    again, calls = _corpus_run(tmp_path, [f"{POST}  {META}"], entries, monkeypatch)
    assert again.exit_code == 0, again.output
    assert calls == [], "finishing the publication went to the platform"
    assert "FINISHING" in again.output
    assert sorted(p.name for p in korpus.glob("*.txt")) == \
        ["Talk - part 1.txt", "Talk - part 2.txt"]
    assert corpus.Ledger.scan(korpus).verdict(POST).done


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe required")
def test_death_after_last_ordinary_part_is_completed_from_disk(tmp_path, monkeypatch):
    """The exact cut: the last ordinary part is already written atomically, the
    first part that serves as evidence is not. completed_course finds the complete
    legacy snapshot in the shared out/ and the CLI finishes publishing the post
    without a metadata resolve or a download. The new staging layout does not
    cancel resume of old snapshots."""
    out_root = tmp_path / "data" / "out"
    course = _course_dir(out_root, "Talk", "Talk", name="cached")
    (course / "source.json").write_text(json.dumps({"source": POST}), encoding="utf-8")
    korpus = tmp_path / "korpus"
    real_write = corpus.atomic_write_text
    order = []

    def die_before_proof(path, text):
        order.append(path.name)
        if corpus.is_completion_proof(path.name):
            raise RuntimeError("death before the evidence")
        real_write(path, text)

    monkeypatch.setattr(corpus, "atomic_write_text", die_before_proof)
    with pytest.raises(RuntimeError, match="before the evidence"):
        corpus.export(course, corpus.Line(1, POST, META), korpus)

    assert order == ["Talk - part 2.txt", "Talk - part 1.txt"]
    assert [p.name for p in korpus.glob("*.txt")] == ["Talk - part 2.txt"]
    assert not corpus.Ledger.scan(korpus).verdict(POST).done
    assert executor.completed_course(out_root, POST) == course

    monkeypatch.setattr(corpus, "atomic_write_text", real_write)
    again, calls = _corpus_run(
        tmp_path, [f"{POST}  {META}"],
        {POST: _entries(POST, "Talk", "Talk")}, monkeypatch)

    assert again.exit_code == 0, again.output
    assert "FINISHING" in again.output
    assert calls == [], "completed_course did not stop the call to the platform"
    assert not _staging(tmp_path).exists(), "the legacy snapshot was duplicated into staging"
    assert sorted(p.name for p in korpus.glob("*.txt")) == \
        ["Talk - part 1.txt", "Talk - part 2.txt"]
    assert corpus.Ledger.scan(korpus).verdict(POST).done


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe required")
def test_partial_legacy_snapshot_resumes_in_out_not_staging(tmp_path, monkeypatch):
    """An incomplete snapshot created by an older version in out/ continues there.

    Otherwise the new staging default creates a second directory for the same post
    while the old one stays incomplete forever, which breaks the main idempotency
    promise of run.
    """
    entries = {POST: _entries(POST, "Talk", "Talk")}
    out_root = tmp_path / "data" / "out"
    first, _ = _corpus_run(
        tmp_path, [f"{POST}  {META}"], entries, monkeypatch,
        extra=("--out", str(out_root)),
    )
    assert first.exit_code == 0, first.output

    courses = [d for d in out_root.iterdir() if (d / "source.json").is_file()]
    assert len(courses) == 1
    course = courses[0]
    missing = sorted((course / "text").glob("*.md"))[-1]
    missing.unlink()
    for path in (tmp_path / "korpus").glob("*.txt"):
        path.unlink()

    again, calls = _corpus_run(tmp_path, [f"{POST}  {META}"], entries, monkeypatch)

    assert again.exit_code == 0, again.output
    assert missing.is_file(), "the old out/ directory was not finished"
    assert len([d for d in out_root.iterdir() if (d / "source.json").is_file()]) == 1
    assert not _staging(tmp_path).exists(), "the partial legacy snapshot was duplicated"
    assert any("-J" in call for call in calls), "the partial snapshot did not refresh the manifest"


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe required")
def test_same_post_twice_in_the_queue_is_downloaded_once(tmp_path, monkeypatch):
    """Two queue lines for one post (another shape of the link, say): a second
    export would drop a twin file with a suffix next to the first."""
    res, calls = _corpus_run(
        tmp_path, [f"{POST}  {META}", f"{POST}?share=post_link  {META}"],
        {POST: _entries(POST, "Talk")}, monkeypatch)

    assert res.exit_code == 0, res.output
    assert "DUPLICATE line 2" in res.output
    assert [p.name for p in (tmp_path / "korpus").glob("*.txt")] == ["Talk.txt"]
    assert len([c for c in calls if "-J" not in c]) == 1


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe required")
def test_queue_shares_one_pacer_and_skips_what_the_corpus_has(tmp_path, monkeypatch, slept):
    """The queue behaves like the old shell script: a pause between ANY two
    downloads of a batch, and a post that is already done does not spend one (SKIP
    happens before `first=0`)."""
    _corpus_file(tmp_path / "korpus", "done.txt", POST2)
    res, calls = _corpus_run(
        tmp_path, [f"{POST2}  {META}", f"{POST}  {META}"],
        {POST: _entries(POST, "A", "B")}, monkeypatch)

    assert res.exit_code == 0, res.output
    assert all(POST2 not in c for c in calls), "a post already in the corpus went to the platform"
    dl = [c for c in calls if "-J" not in c]
    assert len(dl) == 2 and all(c[c.index("-f") + 1] == sources.BOOSTY.fmt for c in dl)
    assert len(slept) == 1, "a skipped post must not cost a pause, and two downloads must have one between them"
