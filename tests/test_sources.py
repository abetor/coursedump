"""Regression tests for the yt-dlp adapter's multi-video post handling.

Tests use synthetic metadata only and never contact a source. They verify that
items are addressed through the canonical post page plus `--playlist-items`,
never through temporary media capability URLs.
"""
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from coursedump import cli, executor, manifest, sources

POST = "https://boosty.to/demo-creator/posts/f897a4cc-06ca-4b29-af1c-abd487c00dd8"
POST2 = "https://boosty.to/demo-creator/posts/b2cc8fd7-1111-4222-8333-444455556666"


def _ytdlp_with_entries(entries, url="https://example.com/post", src_kw=None, **info):
    src = sources.YtdlpSource(url, **(src_kw or {}))
    src._info = {"title": "post", "entries": entries, **info}  # no network: info is injected
    return src


def _boosty_two_videos(**src_kw):
    """The shape of a real yt-dlp -J --flat-playlist answer for a post with 2 videos.

    filesize_approx is EMPTY on live boosty entries; the durations are real ones:
    7088 and 6908 seconds."""
    entry = {
        "title": "Talk on Web3, Solidity and DeFi development (parts 1 and 2)",
        "webpage_url": POST,          # the same for both: it is the post page
        "extractor": "Boosty",
        "filesize_approx": None,
    }
    return _ytdlp_with_entries(
        [
            {**entry, "id": "e73044e1", "playlist_index": 1, "duration": 7088,
             "url": "https://cdn.example.test/media/part-1.mp4"},
            {**entry, "id": "7c84e518", "playlist_index": 2, "duration": 6908,
             "url": "https://cdn.example.test/media/part-2.mp4"},
        ],
        url=POST, src_kw=src_kw, _type="playlist", id="f897a4cc", webpage_url=POST,
    )


def _proc(code=0, stdout="", stderr=""):
    return type("P", (), {"returncode": code, "stdout": stdout, "stderr": stderr})()


class _Ydl:
    """A stand-in for util.run: records argv and pretends it downloaded a file.

    The stub is no kinder than the real yt-dlp: it prints the downloaded id ONLY
    when asked for it (`--print after_move:%(id)s`), otherwise stdout is empty.
    What it prints: `got_id`, and by default the id taken from the requested file
    name (the source did not change, so exactly what was ordered arrived).
    `silent=True` means yt-dlp said nothing even though the flag was there."""

    def __init__(self, ext=".m4a", got_id=None, silent=False):
        self.calls: list[list[str]] = []
        self.ext = ext
        self.got_id = got_id  # what was actually "downloaded"
        self.silent = silent

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        out = cmd[cmd.index("-o") + 1]
        # yt-dlp appends the extension itself and folds '%%' back into '%'
        path = Path(out[: -len(".%(ext)s")].replace("%%", "%") + self.ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        asked = "--print" in cmd and "after_move:%(id)s" in cmd
        vid = self.got_id or _vid_of(path.name)
        return _proc(0, f"{vid}\n" if asked and vid and not self.silent else "")


def _vid_of(name: str) -> str:
    """The id inside a name like '001 - Lesson [abc].m4a' - the very one yt-dlp
    reports as downloaded."""
    m = re.search(r"\[([^\[\]]+)\]\.[^.]+$", name)
    return m.group(1) if m else ""


@pytest.fixture
def ydl(monkeypatch):
    fake = _Ydl()
    monkeypatch.setattr(sources, "run", fake)
    return fake


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """Tests never sleep: the profile throttling (pauses of 10-20 minutes) is
    injected time.

    autouse over the whole module is deliberate: a forgotten injection is not a
    "slow test" but a run hung for 20 minutes (the boosty profile is on by default
    and boosty links appear in nearly every test here). Returns the list of pause
    durations, which the pacing tests assert on.
    It lives here rather than in tests/conftest.py because that file is the shared
    isolation layer and is kept untouched; a new test module that downloads will
    need its own injection just like this one.
    """
    pauses: list[float] = []
    monkeypatch.setattr(sources, "_sleep", pauses.append)
    return pauses


# ------------------------------------------------------------------ names

def test_ytdlp_title_control_chars_sanitized():
    src = _ytdlp_with_entries([
        {"title": "Video\nsecond\rline\ttab\x0band\x7fDEL", "id": "a1", "url": "https://v/1"},
    ])
    rel = src.list()[0].rel
    for bad in ("\n", "\r", "\t", "\x0b", "\x7f"):
        assert bad not in rel
    assert rel.startswith("001 - ") and "Video" in rel


def test_ytdlp_slash_and_nul_still_sanitized():
    src = _ytdlp_with_entries([{"title": "a/b\x00c", "id": "a", "url": "https://v/1"}])
    rel = src.list()[0].rel
    assert "/" not in rel and "\x00" not in rel


# ------------------------------------------------------- multi-video post

def test_multivideo_post_gives_item_per_video():
    """A post with N videos is N items: each with its own position in the post and
    its own stable id in the name."""
    items = _boosty_two_videos().list()
    assert len(items) == 2
    assert [it.index for it in items] == [1, 2]
    assert [it.vid for it in items] == ["e73044e1", "7c84e518"]
    assert all(f"[{it.vid}]" in it.rel for it in items)
    assert len({it.rel for it in items}) == 2  # same title for both, the id tells them apart


@pytest.mark.parametrize("variant", [
    POST + "?share=post_link",              # the link the boosty share button gives
    POST + "?utm_source=tg",
    POST.replace("https://", "http://"),
])
def test_url_variant_does_not_disable_multivideo(variant):
    """A user variant of the link must not switch off addressing by position: we
    compare against the canonical webpage_url from yt-dlp, not against the string
    the user typed. Otherwise both items get index 0 and are downloaded with
    `--no-playlist` from the same post, which that flag does not narrow (it prints
    both lines) - so position addressing turns itself off silently."""
    src = _boosty_two_videos()
    src.url = variant
    assert [it.index for it in src.list()] == [1, 2]


def test_multivideo_item_not_addressed_by_expiring_cdn_url():
    """remote is the post page, not a temporary media capability URL."""
    for it in _boosty_two_videos().list():
        assert it.remote == POST
        assert "cdn.example.test" not in it.remote


def test_multivideo_fetch_picks_the_right_video(tmp_path, ydl):
    """Every item is narrowed by its own position: --no-playlist does not narrow a
    post at all (checked live - it prints both lines), so without a position both
    items would download the whole post into the same file."""
    src = _boosty_two_videos()
    items = src.list()
    raw = tmp_path / "raw"

    src.fetch(items[1], raw)
    cmd = ydl.calls[-1]
    assert "--playlist-items" in cmd and cmd[cmd.index("--playlist-items") + 1] == "2"
    assert "--no-playlist" not in cmd
    assert cmd[-1] == POST
    assert src.fetched(items[1], raw) is not None

    src.fetch(items[0], raw)
    assert ydl.calls[-1][ydl.calls[-1].index("--playlist-items") + 1] == "1"
    # two different files, not one overwritten
    assert len({p.name for p in raw.iterdir()}) == 2


def test_playlist_entry_with_own_page_still_uses_no_playlist(tmp_path, ydl):
    """A YouTube embed in a post, or an ordinary playlist: the entry has a page of
    its own, so we download from it with --no-playlist and need no position."""
    src = _ytdlp_with_entries([
        {"_type": "url", "title": "Clip", "id": "yt1",
         "url": "https://www.youtube.com/watch?v=abc"},
    ], url=POST)
    item = src.list()[0]
    assert item.index == 0 and item.remote == "https://www.youtube.com/watch?v=abc"

    src.fetch(item, tmp_path / "raw")
    cmd = ydl.calls[-1]
    assert "--no-playlist" in cmd and "--playlist-items" not in cmd
    assert cmd[-1] == "https://www.youtube.com/watch?v=abc"


def test_single_video_post_uses_no_playlist(tmp_path, ydl):
    """A post with a single video: yt-dlp returns the video itself, not a playlist."""
    src = sources.YtdlpSource(POST)
    src._info = {"title": "One clip", "id": "v1", "webpage_url": POST,
                 "url": "https://cdn.example.test/media/single.mp4"}
    item = src.list()[0]
    assert item.index == 0 and item.remote == POST

    src.fetch(item, tmp_path / "raw")
    assert "--no-playlist" in ydl.calls[-1] and "--playlist-items" not in ydl.calls[-1]


# --------------------------------------------------------- name as template

def test_percent_in_title_escaped_in_output_template(tmp_path, ydl):
    """'Top 100%(best)' inside a title is a yt-dlp template: without escaping the
    download dies with a KeyError."""
    src = _ytdlp_with_entries([
        {"title": "Top 100%(best) courses", "id": "p1", "url": "https://v/1"}])
    item = src.list()[0]
    got = src.fetch(item, tmp_path / "raw")

    out = ydl.calls[-1][ydl.calls[-1].index("-o") + 1]
    assert "%%(best)" in out and out.endswith(".%(ext)s")
    assert got.name.startswith("001 - Top 100%(best) courses")

    from yt_dlp import YoutubeDL  # the template must survive the real yt-dlp engine
    assert YoutubeDL({"outtmpl": out, "quiet": True}).prepare_filename(
        {"id": "p1", "ext": "m4a"}).endswith("Top 100%(best) courses [p1].m4a")


def test_fetched_survives_glob_chars_in_title(tmp_path, ydl):
    """A name built from a title is NOT a glob pattern. With '[SW]' in the title
    fetched() failed to find the already downloaded file: fetch raised a SourceError
    AFTER a successful download and the resume downloaded it again."""
    src = _ytdlp_with_entries([
        {"title": "[SW] Lesson 1 - what? *all*", "id": "b1", "url": "https://v/1"}])
    item = src.list()[0]
    got = src.fetch(item, tmp_path / "raw")

    assert got.exists() and "[SW]" in got.name
    assert src.fetched(item, tmp_path / "raw") == got  # the resume sees what was downloaded


# ------------------------------------------------------------ item identity

def _post_entries(*vids, post=POST):
    """A post where every video shares ONE title (the real boosty shape): the only
    way to tell them apart is the id."""
    return [{"title": "Talk", "id": v, "playlist_index": i, "webpage_url": post,
             "duration": 100, "url": f"https://cdn.example.test/media/{i}.mp4"}
            for i, v in enumerate(vids, 1)]


def _fake_post(monkeypatch, box) -> list[list[str]]:
    """yt-dlp on a post: -J returns box[0], and a download writes into the file the
    id of the video that actually sits at the requested position. Returns the argv
    log."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "-J" in cmd:
            return _proc(0, json.dumps({"title": "post", "_type": "playlist", "id": "p",
                                        "webpage_url": POST, "entries": box[0]}))
        vid = box[0][int(cmd[cmd.index("--playlist-items") + 1]) - 1]["id"]
        path = Path(cmd[cmd.index("-o") + 1][: -len(".%(ext)s")].replace("%%", "%") + ".m4a")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(vid.encode())  # the "content" of a video is its id
        return _proc(0, vid + "\n")
    monkeypatch.setattr(sources, "run", fake_run)
    return calls


def _fake_posts(monkeypatch, by_url: dict[str, list[dict]]) -> list[list[str]]:
    """The same, but for SEVERAL posts at once: it answers by the URL in argv.

    Needed for the `run POST_A POST_B` queue: every argument gets its own adapter,
    so a single shared box will not do."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        entries = by_url[cmd[-1]]
        if "-J" in cmd:
            name = cmd[-1].rsplit("/", 1)[-1][:8]
            return _proc(0, json.dumps({"title": f"post {name}", "_type": "playlist",
                                        "id": name, "webpage_url": cmd[-1],
                                        "entries": entries}))
        vid = entries[int(cmd[cmd.index("--playlist-items") + 1]) - 1]["id"]
        path = Path(cmd[cmd.index("-o") + 1][: -len(".%(ext)s")].replace("%%", "%") + ".m4a")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(vid.encode())
        return _proc(0, vid + "\n")
    monkeypatch.setattr(sources, "run", fake_run)
    return calls


def test_inserted_video_does_not_steal_finished_item(tmp_path, monkeypatch):
    """Post A/B is downloaded, then X is inserted before them. Positions shifted and
    every title is the same, so without a stable id the old '001' would silently
    count as finished for a NEW video - a quiet content swap."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe required")
    box = [_post_entries("a1", "b1")]
    _fake_post(monkeypatch, box)
    out = tmp_path / "out"
    opts = executor.Opts(out_root=out, asr_backend="dummy")

    executor.run_course(POST, opts)
    box[0] = _post_entries("x1", "a1", "b1")   # the source changed
    executor.run_course(POST, opts)

    cdir = out / "post"
    items = manifest.load(cdir / "manifest.jsonl")
    assert [it.vid for it in items] == ["x1", "a1", "b1"]
    for it in items:
        raw = cdir / "raw" / (it.rel + ".m4a")
        assert raw.read_bytes().decode() == it.vid, "the raw file of an item is another video"
        body = (cdir / "text" / it.target).read_text(encoding="utf-8")
        assert f"transcript of {raw.name}" in body, "the text of an item came from another file"


def test_downloaded_id_must_match_expected(tmp_path, ydl):
    """The post changed between resolve and download: the position brought a
    different video. The decoy file must be deleted, otherwise a resume takes it
    for a finished one."""
    src = _boosty_two_videos()
    item = src.list()[0]
    ydl.got_id = "someone-else"
    with pytest.raises(sources.SourceError):
        src.fetch(item, tmp_path / "raw")
    assert list((tmp_path / "raw").iterdir()) == []

    ydl.got_id = item.vid  # same id, so the download is accepted
    assert src.fetch(item, tmp_path / "raw").exists()


def test_fetch_asks_ytdlp_to_print_downloaded_id(tmp_path, ydl):
    """The check rests on one flag: with no `--print after_move:%(id)s` there is no
    id in stdout and nothing to compare. So the flag itself is guarded."""
    src = _boosty_two_videos()
    src.fetch(src.list()[0], tmp_path / "raw")
    cmd = ydl.calls[-1]
    assert "--print" in cmd and cmd[cmd.index("--print") + 1] == "after_move:%(id)s"


def test_silent_print_is_not_a_passed_check(tmp_path, ydl):
    """yt-dlp said nothing (another version, an extractor without ids): there is
    NOTHING to compare. An empty stdout used to count as a passed check, which made
    the guard fail-open - it let through exactly the quiet swap it exists to catch."""
    src = _boosty_two_videos()
    ydl.silent = True
    with pytest.raises(sources.SourceError, match="did not print the downloaded item ID"):
        src.fetch(src.list()[0], tmp_path / "raw")
    assert list((tmp_path / "raw").iterdir()) == []  # nothing unverified is kept


# ------------------------------------------------------------- space check

CLUB_DUR = 56 * 60  # 3360 seconds, representative long-form media duration


def _club_formats():
    """Representative non-flat yt-dlp formats for conservative size tests.

    The synthetic range includes separate 52-265 kbit/s audio tracks, video up
    to 2560x1362, and 0.3-3 GB combined media. ``filesize`` is exactly bitrate
    times duration, matching the downloader's reported relationship.
    """
    def size(tbr):
        return int(CLUB_DUR * tbr * 1000 / 8)
    return [
        {"format_id": "dash-9", "vcodec": "none", "acodec": "mp4a.40.2",
         "tbr": 52, "filesize": size(52)},
        {"format_id": "dash-12", "vcodec": "none", "acodec": "mp4a.40.2",
         "tbr": 265, "filesize": size(265)},
        {"format_id": "sd", "vcodec": "avc1.4d401f", "acodec": "none",
         "height": 480, "tbr": 1100, "filesize": size(1100)},
        {"format_id": "quad_hd", "vcodec": "avc1.640033", "acodec": "none",
         "height": 1362, "tbr": 7000, "filesize": size(7000)},
    ]


# what actually arrives with the default bestvideo*+bestaudio: 2.94 GB of video
# plus 111 MB of audio
CLUB_REAL_BYTES = int(CLUB_DUR * (7000 + 265) * 1000 / 8)


def _club_entry(**over):
    return {"title": "English club", "id": "c1", "duration": CLUB_DUR,
            "url": "https://cdn.example.test/media/club.mp4", **over}


def test_size_of_selected_format_not_of_the_cheapest():
    """When format metadata exists, take the size of what will REALLY be selected.
    fetch calls yt-dlp without `-f`, that is bestvideo*+bestaudio: 2560x1362 plus
    the heaviest audio track, not dash-9 and not the first format in the list."""
    fmts = _club_formats()
    src = _ytdlp_with_entries([_club_entry(formats=fmts)])
    got = src.list()[0].size

    assert got == CLUB_REAL_BYTES                       # video plus the merged audio
    assert all(got >= f["filesize"] for f in fmts)      # below no single format
    assert got > max(f["filesize"] for f in fmts)       # the merge is counted, not forgotten


def test_size_from_format_bitrate_when_format_has_no_filesize():
    """The formats carry only bitrates (yt-dlp does not always know the size), so we
    compute from the bitrate of the format that will be chosen, not from the cheap
    or the average one."""
    fmts = [{k: v for k, v in f.items() if k != "filesize"} for f in _club_formats()]
    src = _ytdlp_with_entries([_club_entry(formats=fmts)])
    got = src.list()[0].size

    assert got == CLUB_REAL_BYTES
    assert got > int(CLUB_DUR * 1100 * 1000 / 8)  # not from the sd format


def test_duration_fallback_is_above_the_real_video():
    """The duration fallback has to be an UPPER bound, not a "typical" one: on the
    same real post the full video is 2.94 GB for 56 minutes (~7 Mbit/s). The old
    2 Mbit/s gave 0.84 GB, three times less than reality, and the space contract
    (`2 * item size`) would hold only on paper."""
    src = _ytdlp_with_entries([_club_entry()])  # no filesize, no formats, no tbr
    assert src.list()[0].size >= CLUB_REAL_BYTES


def test_size_estimated_when_source_gives_no_filesize():
    """boosty entries under --flat-playlist have neither filesize_approx nor
    formats, so the size is estimated from the duration; otherwise the manifest
    holds zero and the space check degenerates. Here the profile is off
    (`--full-video`), so we download full video: 7088 s at 8 Mbit/s is about 7 GB,
    and the order of magnitude has to be gigabytes."""
    items = _boosty_two_videos(full_video=True).list()
    assert all(it.size > 0 for it in items)
    assert 3.0e9 < items[0].size < 1.5e10
    assert items[0].size > items[1].size  # a longer video gets a bigger estimate


def test_size_from_bitrate_when_source_names_it():
    """If the source named the bitrate of the item itself, use it."""
    src = _ytdlp_with_entries([{"title": "Lesson", "id": "a1", "duration": 3600,
                                "tbr": 400, "url": "https://v/1"}])
    assert src.list()[0].size == int(3600 * 400 * 1000 / 8)


def test_space_check_gets_nonzero_need(tmp_path, monkeypatch):
    """The estimate has to reach ensure_space: the contract in docs/DESIGN.md is
    `2 * item size`, not min_free alone."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe required")
    _fake_post(monkeypatch, [_post_entries("a1")])
    needs: list[int] = []
    monkeypatch.setattr(executor, "ensure_space", lambda where, need: needs.append(need))
    executor.run_course(POST, executor.Opts(out_root=tmp_path / "out", asr_backend="dummy"))
    assert needs and all(n > 0 for n in needs)


# ----------------------------------------------------------- boosty profile
# By default a boosty source downloads only audio and uses conservative source
# pacing (tuned on 78 real videos). Explicit flags relax it. The tests are
# hermetic: argv is checked against a stub, while time and randomness are
# injected (the autouse slept fixture of this module; its docstring says why it
# is not in conftest). The pacer needs no reset between tests: it belongs to a
# RUN (sources.RunPacer inside executor.Opts), not to the module, so a "new run"
# in a test is a new Opts, exactly like a new `coursedump run`.

def test_boosty_profile_values_are_a_copy_of_batch_sh():
    """The values are a copy of the reference script, not "roughly the same". This
    guards against a silent "optimisation": nudging the limit or the pauses changes
    the established lawful, user-authorized download profile."""
    assert sources.BOOSTY.fmt == "bestaudio[abr<=70]/bestaudio"   # reference -f
    assert sources.BOOSTY.limit_rate == "1.5M"                    # reference --limit-rate
    # the reference does `600 + RANDOM % 600` = 600..1199, and randint includes the
    # upper bound, so 1199 - otherwise the profile can pause longer than the reference
    assert sources.BOOSTY.pause == (600, 1199)


def test_boosty_fetch_is_audio_only_and_rate_limited(tmp_path, ydl):
    """The real download argv: transcription does not need the video track (20-60 MB
    instead of 0.3-3 GB), and the rate is limited."""
    src = _boosty_two_videos()
    src.fetch(src.list()[0], tmp_path / "raw")

    cmd = ydl.calls[-1]
    assert cmd[cmd.index("-f") + 1] == "bestaudio[abr<=70]/bestaudio"
    assert cmd[cmd.index("--limit-rate") + 1] == "1.5M"


@pytest.mark.parametrize("url", [
    POST,
    POST + "?share=post_link",
    POST.replace("https://", "http://"),
    "https://boosty.to/demo-creator",
    POST.replace("boosty.to", "www.boosty.to"),   # subdomain: the endswith('.boosty.to') branch
    POST.replace("boosty.to", "boosty.to."),      # a trailing dot FQDN is the same DNS host
])
def test_profile_follows_the_host_not_the_shape_of_the_link(url):
    """The profile is bound to the HOST: a variant of the link (share, http, the
    blog page, www, a trailing dot) must not silently switch off audio mode and
    throttling."""
    src = sources.YtdlpSource(url)
    assert src.fmt == sources.BOOSTY.fmt and src.throttle is sources.BOOSTY


def test_redirect_wrapper_to_boosty_gets_the_profile_by_canonical_url(tmp_path, monkeypatch):
    """A shortener in front of a post (bit.ly / t.co / vk.cc - the usual shape of a
    link shared in a messenger) is not boosty by the user string: the profile did
    not switch on and the post was downloaded as full video, at full speed and with
    no pauses - SILENTLY. That is exactly the human slip the profile has to absorb.
    yt-dlp follows the redirect itself and reports the real address in
    webpage_url, which is what we decide on."""
    short = "https://bit.ly/3xYzAbC"
    entries = _post_entries("a1", "b1")           # webpage_url of the entries is the post
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "-J" in cmd:
            assert cmd[-1] == short               # we resolve the user string as it is
            return _proc(0, json.dumps({"title": "post", "_type": "playlist", "id": "p",
                                        "webpage_url": POST, "entries": entries}))
        path = Path(cmd[cmd.index("-o") + 1][: -len(".%(ext)s")].replace("%%", "%") + ".m4a")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        return _proc(0, entries[int(cmd[cmd.index("--playlist-items") + 1]) - 1]["id"] + "\n")

    monkeypatch.setattr(sources, "run", fake_run)
    src = sources.detect(short)
    assert src.profile is None                    # by the user string it is not boosty

    items = src.list()                            # the resolve brought the canonical URL
    assert src.profile is sources.BOOSTY and src.throttle is sources.BOOSTY
    assert src.fmt == sources.BOOSTY.fmt
    assert items[0].size < 1.0e9                  # and the space estimate is audio sized

    src.fetch(items[0], tmp_path / "raw")
    cmd = calls[-1]
    assert cmd[cmd.index("-f") + 1] == sources.BOOSTY.fmt
    assert cmd[cmd.index("--limit-rate") + 1] == "1.5M"

    # reclassification only switches ON: it never overrides explicit user flags
    off = sources.detect(short, full_video=True, no_throttle=True)
    off.list()
    assert off.profile is sources.BOOSTY and off.fmt == "" and off.throttle is None


def test_foreign_canonical_url_does_not_take_the_profile_off(tmp_path, monkeypatch, slept):
    """The reverse direction of the same reclassification is fail-closed: it can NOT
    take an enabled profile off. The real shape: a post with a single YouTube embed,
    where the user string is boosty but the `webpage_url` from yt-dlp is already the
    video page. A metadata request has gone to the platform by then and downloads of
    the same batch follow; silently dropping throttling because of a foreign URL is
    exactly the quiet failure of the protection the profile exists for. Only a human
    takes it off."""
    yt = "https://www.youtube.com/watch?v=abc"
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "-J" in cmd:
            return _proc(0, json.dumps({"title": "Clip", "id": "e1", "webpage_url": yt}))
        path = Path(cmd[cmd.index("-o") + 1][: -len(".%(ext)s")].replace("%%", "%") + ".m4a")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        return _proc(0, "e1\n")

    monkeypatch.setattr(sources, "run", fake_run)
    src = sources.detect(POST)
    items = src.list()

    assert src.profile is sources.BOOSTY and src.throttle is sources.BOOSTY
    assert src.fmt == sources.BOOSTY.fmt
    src.fetch(items[0], tmp_path / "raw")
    cmd = calls[-1]
    assert cmd[cmd.index("-f") + 1] == sources.BOOSTY.fmt
    assert cmd[cmd.index("--limit-rate") + 1] == "1.5M"


def test_cached_manifest_keeps_the_profile_after_a_failed_resolve(tmp_path, monkeypatch, slept):
    """The resolve failed (stale cookies, no network), so work continues from the
    cached manifest and the second classification by `webpage_url` never happens:
    its place is in `_load`, which did not exist. For a shortener wrapper that meant
    losing the profile exactly on a resume - full video at full speed, silently. The
    URL from the last successful resolve is in the manifest (`item.remote`), so we
    decide on it and download from it rather than from the wrapper."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe required")
    short = "https://bit.ly/3xYzAbC"
    entries = _post_entries("a1", post=POST)
    live = {"resolve": True, "download": False}   # first pass: manifest ok, download failed
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "-J" in cmd:
            if not live["resolve"]:
                return _proc(1, "", "ERROR: HTTP Error 403")
            return _proc(0, json.dumps({"title": "post", "_type": "playlist", "id": "p",
                                        "webpage_url": POST, "entries": entries}))
        if not live["download"]:
            return _proc(1, "", "ERROR: HTTP Error 403")
        path = Path(cmd[cmd.index("-o") + 1][: -len(".%(ext)s")].replace("%%", "%") + ".m4a")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        return _proc(0, "a1\n")

    monkeypatch.setattr(sources, "run", fake_run)
    out = tmp_path / "out"
    executor.run_course(short, executor.Opts(out_root=out, asr_backend="dummy"))

    live.update(resolve=False, download=True)     # no more metadata, downloading still works
    calls.clear()
    stats = executor.run_course(short, executor.Opts(out_root=out, asr_backend="dummy"))

    assert stats["errors"] == 0
    dl = [c for c in calls if "-J" not in c]
    assert len(dl) == 1
    assert dl[0][dl[0].index("-f") + 1] == sources.BOOSTY.fmt
    assert dl[0][dl[0].index("--limit-rate") + 1] == "1.5M"
    assert dl[0][-1] == POST                      # the canonical URL from cache, not the wrapper
    saved = json.loads((out / "post" / "source.json").read_text(encoding="utf-8"))
    assert saved["profile"] == "boosty", "source.json lies about how it was downloaded"


def test_boosty_entry_in_a_foreign_container_gets_the_profile(tmp_path, ydl, slept):
    """The mirror image of the shortener case: the container is not boosty (a foreign
    playlist or a link collection page) while an entry points at boosty through its
    own page. The profile is a property of the PLATFORM, so it is decided per item:
    the boosty entry gets audio mode, the rate limit and the pause, its youtube
    neighbour gets nothing. The size estimate is per item too: by fetch time it is
    too late, and the difference between an audio track and full video is an order
    of magnitude (a false "not enough space" stop, or a check about nothing)."""
    src = _ytdlp_with_entries([
        {"_type": "url", "title": "Clip", "id": "y1", "duration": 7088,
         "url": "https://youtu.be/1"},
        {"_type": "url", "title": "Post", "id": "b1", "duration": 7088, "url": POST},
        {"_type": "url", "title": "Another post", "id": "b2", "duration": 6908, "url": POST2},
    ], url="https://example.com/list")
    assert src.profile is None                     # the container itself gives no profile
    yt_item, first, second = src.list()

    assert first.size < 1.0e9 < yt_item.size       # audio sized against video sized

    src.fetch(yt_item, tmp_path / "raw")
    assert "-f" not in ydl.calls[-1] and "--limit-rate" not in ydl.calls[-1]

    src.fetch(first, tmp_path / "raw")
    cmd = ydl.calls[-1]
    assert cmd[cmd.index("-f") + 1] == sources.BOOSTY.fmt
    assert cmd[cmd.index("--limit-rate") + 1] == "1.5M"
    assert cmd[-1] == POST
    assert slept == [], "a pause before the FIRST profiled download (the neighbour has no profile)"

    src.fetch(second, tmp_path / "raw")            # the second download of the platform pauses
    assert len(slept) == 1


def test_file_link_on_a_profiled_host_is_refused_not_downloaded_bare():
    """The `_DOC_EXT` route: a link ending in a file extension went to HttpSource -
    a bare urlretrieve with no cookies, no rate limit and no pauses, that is, past
    the profile. Teaching HttpSource the whole profile would add a second download
    path for a rare shape, and without cookies a closed post answers 403, so we
    would silently save the error page under the file name. Hence an explicit
    refusal. Foreign hosts keep using this route as before: the profile is not a
    universal policy but knowledge about one platform."""
    with pytest.raises(sources.SourceError, match="boosty"):
        sources.detect("https://boosty.to/demo-creator/media/lecture.mp3")
    with pytest.raises(sources.SourceError, match="boosty"):
        sources.detect("https://www.boosty.to/x/y.zip?dl=1")
    assert isinstance(sources.detect("https://example.com/lecture.mp3"), sources.HttpSource)


def test_non_boosty_source_is_not_touched_by_profile(tmp_path, ydl, slept, js_registry):
    """The profile applies to one source, not to everyone: youtube is downloaded as
    before, with no `-f`, no rate limit and no pauses. argv is compared IN FULL
    rather than by the absence of new flags: an empty profile must neither reorder
    the arguments nor slip an empty string into the list."""
    js_registry(deno=(2, 3, 0))   # argv in full, so the runtime is pinned too
    src = _ytdlp_with_entries([
        {"_type": "url", "title": "One", "id": "y1", "url": "https://youtu.be/1"},
        {"_type": "url", "title": "Two", "id": "y2", "url": "https://youtu.be/2"},
    ], url="https://www.youtube.com/playlist?list=PL1")
    assert src.profile is None and src.fmt == "" and src.throttle is None

    raw = tmp_path / "raw"
    items = src.list()
    for it in items:
        src.fetch(it, raw)
    assert ydl.calls[0] == [
        sys.executable, "-m", "yt_dlp", "--js-runtimes", "deno", *sources.YT_CLIENTS,
        "-o", f"{raw / items[0].rel}.%(ext)s", "--no-playlist",
        "--write-subs", "--sub-langs", "ru.*,en.*",
        "--print", "after_move:%(id)s", "--no-progress", "https://youtu.be/1",
    ]
    assert all("-f" not in c and "--limit-rate" not in c for c in ydl.calls)
    assert slept == []


def test_pause_between_items_but_not_before_the_first(tmp_path, ydl, slept, monkeypatch):
    """Pauses as in the reference script: no pause before the first download
    (otherwise a single item costs ten minutes for nothing), and a random 600-1199 s
    between the following ones."""
    asked: list[tuple[int, int]] = []
    monkeypatch.setattr(sources, "_randint",
                        lambda lo, hi: (asked.append((lo, hi)), lo + 7)[1])
    src = _boosty_two_videos()
    items = src.list()

    src.fetch(items[0], tmp_path / "raw")
    assert slept == [], "a pause before the first download"
    src.fetch(items[1], tmp_path / "raw")
    assert slept == [607] and asked == [(600, 1199)]


def test_pause_comes_before_the_download_not_after(tmp_path, ydl, monkeypatch):
    """The pause has to come BEFORE the download: the pattern is set by the interval
    between requests to the platform, while sleeping after the last item is just ten
    minutes lost at the end of the run."""
    events: list[str] = []
    monkeypatch.setattr(sources, "_sleep", lambda s: events.append("pause"))
    stub = sources.run
    monkeypatch.setattr(sources, "run",
                        lambda cmd, **kw: (events.append("download"), stub(cmd, **kw))[1])
    src = _boosty_two_videos()
    items = src.list()

    src.fetch(items[0], tmp_path / "raw")
    src.fetch(items[1], tmp_path / "raw")
    assert events == ["download", "pause", "download"]


def test_pause_is_shared_by_all_sources_of_one_run(tmp_path, monkeypatch, slept):
    """The pause belongs to the RUN, not to a source: in the reference script one
    `first` lives for the whole urls.txt, so an interval is guaranteed between ANY
    two downloads of a batch. A counter inside the adapter gave zero pauses in
    exactly the target scenario - a queue of single-video posts
    (`run POST_A POST_B`, or resuming several courses), where the CLI creates a new
    adapter per argument: N downloads in a row with no interval."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe required")
    calls = _fake_posts(monkeypatch, {POST: _post_entries("a1", post=POST),
                                      POST2: _post_entries("b1", post=POST2)})
    opts = executor.Opts(out_root=tmp_path / "out", asr_backend="dummy")
    for url in (POST, POST2):        # exactly what cli.run does with two arguments
        executor.run_course(url, opts)

    assert len([c for c in calls if "-J" not in c]) == 2   # one download per post
    assert len(slept) == 1           # none before the first, one between the posts


def test_pacer_does_not_leak_between_two_cli_runs(tmp_path, monkeypatch, slept):
    """...but NOT longer than the run. The pacer is an object of `coursedump run`
    (RunPacer inside executor.Opts), not of the module: two CLI calls in one process
    (this test, a wrapper script, a future daemon) do not share state, otherwise the
    first download of the second run pays 10-20 minutes for somebody else's run -
    and in the reference script every run starts with `first=1`."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe required")
    calls = _fake_posts(monkeypatch, {POST: _post_entries("a1", post=POST),
                                      POST2: _post_entries("b1", post=POST2)})
    monkeypatch.setattr(cli, "_caffeinate", lambda: None)  # the test does not keep the mac awake
    args = ["run", "--data", str(tmp_path / "d"), "--asr", "dummy"]
    runner = CliRunner()

    first = runner.invoke(cli.app, args + [POST])
    second = runner.invoke(cli.app, args + [POST2])

    assert (first.exit_code, second.exit_code) == (0, 0), (first.output, second.output)
    assert len([c for c in calls if "-J" not in c]) == 2   # one download per run
    assert slept == [], "the second run paid a pause for the first"


def test_finished_source_does_not_spend_a_pause(tmp_path, monkeypatch, slept):
    """A SKIP does not move the pacer: in the reference script the `continue` for an
    existing file comes BEFORE `first=0`, so a skip does not count as a request to
    the platform (nor does metadata: `--print filename` runs before the pause).
    Otherwise the first post of the queue that is really downloaded would pay 10-20
    minutes for nothing."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe required")
    out = tmp_path / "out"
    calls = _fake_posts(monkeypatch, {POST: _post_entries("a1", post=POST),
                                      POST2: _post_entries("b1", post=POST2)})
    executor.run_course(POST, executor.Opts(out_root=out, asr_backend="dummy"))
    slept.clear()                             # post A is downloaded and processed

    nxt = executor.Opts(out_root=out, asr_backend="dummy")  # new run: A is already done
    executor.run_course(POST, nxt)            # all done: not a single download
    executor.run_course(POST2, nxt)           # the first REAL download of this run
    assert len([c for c in calls if "-J" not in c]) == 2   # only a1 and b1, A not refetched
    assert slept == []


def test_full_video_flag_drops_audio_mode_and_keeps_throttle(tmp_path, ydl, slept):
    """`--full-video` relaxes exactly the audio mode: throttling is a separate
    decision, otherwise "I want the video track" quietly becomes "I download at full
    speed"."""
    src = _boosty_two_videos(full_video=True)
    items = src.list()
    src.fetch(items[0], tmp_path / "raw")
    src.fetch(items[1], tmp_path / "raw")

    assert all("-f" not in c for c in ydl.calls)
    assert all(c[c.index("--limit-rate") + 1] == "1.5M" for c in ydl.calls)
    assert len(slept) == 1


def test_no_throttle_flag_drops_pauses_and_limit_and_keeps_audio(tmp_path, ydl, slept):
    """`--no-throttle` relaxes exactly the throttling; audio mode stays."""
    src = _boosty_two_videos(no_throttle=True)
    items = src.list()
    src.fetch(items[0], tmp_path / "raw")
    src.fetch(items[1], tmp_path / "raw")

    assert all("--limit-rate" not in c for c in ydl.calls)
    assert slept == []
    assert all(c[c.index("-f") + 1] == sources.BOOSTY.fmt for c in ydl.calls)


def test_detect_switches_the_profile_on_and_flags_switch_it_off():
    """The profile is switched on in detect (the only entry point into adapters), so
    it works for both `run` and `plan`, not only in an adapter unit test."""
    assert sources.detect(POST).fmt == sources.BOOSTY.fmt
    assert sources.detect(POST).throttle is sources.BOOSTY
    assert sources.detect(POST, full_video=True).fmt == ""
    assert sources.detect(POST, no_throttle=True).throttle is None
    assert sources.detect("https://www.youtube.com/watch?v=abc").profile is None


def test_profile_reaches_argv_through_the_executor(tmp_path, monkeypatch, slept):
    """There are two links between the profile and the real argv (Opts -> detect):
    the whole path is checked, not only the adapter."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe required")
    calls = _fake_post(monkeypatch, [_post_entries("a1", "b1")])
    executor.run_course(POST, executor.Opts(out_root=tmp_path / "out", asr_backend="dummy"))

    dl = [c for c in calls if "-J" not in c]
    assert len(dl) == 2
    assert all(c[c.index("-f") + 1] == sources.BOOSTY.fmt for c in dl)
    assert all(c[c.index("--limit-rate") + 1] == "1.5M" for c in dl)
    assert len(slept) == 1  # a pause between the two videos, none before the first


def test_executor_flags_switch_the_profile_off(tmp_path, monkeypatch, slept):
    """The same path for relaxing it: the flags have to reach argv, otherwise they
    lie."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe required")
    calls = _fake_post(monkeypatch, [_post_entries("a1", "b1")])
    executor.run_course(POST, executor.Opts(out_root=tmp_path / "out", asr_backend="dummy",
                                            full_video=True, no_throttle=True))

    dl = [c for c in calls if "-J" not in c]
    assert len(dl) == 2
    assert all("-f" not in c and "--limit-rate" not in c for c in dl)
    assert slept == []


@pytest.mark.parametrize("off", [
    {"full_video": True},
    {"no_throttle": True},
    {"full_video": True, "no_throttle": True},
])
def test_profile_off_does_not_stick_to_the_next_run(tmp_path, monkeypatch, slept, off):
    """Switching the profile off is a property of the RUN, not of the course: one
    `run --full-video` must not silently drop audio mode for every later resume.
    Only a trace for the human goes into source.json, and cookies/title are read
    back (executor.saved_state) - but since the safe default rests on this, it is
    guarded."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe required")
    box = [_post_entries("a1")]
    calls = _fake_post(monkeypatch, box)
    out = tmp_path / "out"
    executor.run_course(POST, executor.Opts(out_root=out, asr_backend="dummy", **off))

    box[0] = _post_entries("a1", "b1")   # a video was added, so the resume has work
    calls.clear()
    executor.run_course(POST, executor.Opts(out_root=out, asr_backend="dummy"))

    dl = [c for c in calls if "-J" not in c]
    assert len(dl) == 1                                        # a1 is done, b1 is downloaded
    assert dl[0][dl[0].index("-f") + 1] == sources.BOOSTY.fmt
    assert dl[0][dl[0].index("--limit-rate") + 1] == "1.5M"
    saved = json.loads((out / "post" / "source.json").read_text(encoding="utf-8"))
    assert saved["profile"] == "boosty" and saved["format"] == sources.BOOSTY.fmt


# space estimate in audio mode: we download a track, not a video

def test_audio_mode_estimates_size_by_the_audio_format():
    """When formats exist, take the heaviest AUDIO track (the profile asks for
    `bestaudio[abr<=70]`, but the `/bestaudio` fallback may pick that one), not
    bestvideo+bestaudio: otherwise the space check demands 25 times more than will
    actually arrive and stops the run for no reason."""
    src = _ytdlp_with_entries([_club_entry(formats=_club_formats())], url=POST)
    got = src.list()[0].size

    assert got == int(CLUB_DUR * 265 * 1000 / 8)   # dash-12, the heaviest track
    assert got < CLUB_REAL_BYTES / 10              # not video sized


def test_audio_mode_fallback_is_above_the_real_audio_but_not_video_sized():
    """The real boosty shape: no filesize, no formats, only a duration. The fallback
    has to stay above a real audio download (28 MB for 56 minutes) and still not be
    gigabyte sized."""
    real_per_sec = 28 * 1024**2 / (56 * 60)        # a real dash-10 download
    got = _boosty_two_videos().list()[0].size      # 7088 s

    assert got > real_per_sec * 7088               # above the real one
    assert got < 1.0e9                             # but not like full video
    assert _boosty_two_videos(full_video=True).list()[0].size > 3.0e9


# ------------------------------------------------------ YouTube JS runtime
#
# An observed 2026-08-26 failure affected 44 of 46 records: yt-dlp 2026-07
# requires an external JS runtime, enables only deno by default, and deno was
# not installed on the machine. There was no refusal - there was a SILENT
# degradation: one warning line lost among the progress output, a truncated
# format list, and failures on resolve ("The page needs to be reloaded") and on
# download ("HTTP Error 403") for the two boosty posts whose video lives on
# YouTube.


@pytest.fixture
def js_registry(monkeypatch):
    """Replaces yt-dlp's own runtime registry and clears the probe cache on both
    sides.

    The probe is cached per process (running the binary for every post of the queue
    is expensive), so the test has to clear the cache after itself as well,
    otherwise the next test would get a forged answer. Only `supported` and
    `version_tuple` are needed out of info.
    """
    from yt_dlp.globals import supported_js_runtimes

    def install(**by_name):
        registry = {}
        for name, version in by_name.items():
            info = None if version is None else type(
                "Info", (), {"supported": True, "version_tuple": version})()
            registry[name] = type("Runtime", (), {"info": info})
        monkeypatch.setattr(supported_js_runtimes, "value", registry)
        sources._js_runtimes.cache_clear()

    sources._js_runtimes.cache_clear()
    yield install
    sources._js_runtimes.cache_clear()


def test_js_runtime_is_enabled_in_argv(tmp_path, ydl, js_registry):
    """A runtime we found is enabled EXPLICITLY: the yt-dlp default is deno alone."""
    js_registry(deno=(2, 3, 0), node=(24, 0, 0))
    src = _boosty_two_videos()
    src.fetch(src.list()[0], tmp_path / "raw")

    cmd = ydl.calls[-1]
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "--js-runtimes"] == ["deno", "node"]


def test_no_js_runtime_is_refused_at_the_start_not_mid_queue(js_registry, ydl):
    """No runtime at all means a clear refusal when the adapter is created, before
    any network call.

    This guards against going back to the old trap: it used to be a 403 in the
    middle of the queue, after an hour of downloads and pauses, with the cause not
    named in the message at all.
    """
    js_registry(deno=None, node=None, quickjs=None, bun=None)
    with pytest.raises(sources.SourceError) as e:
        sources.detect(POST)
    assert "runtime" in str(e.value) and "deno" in str(e.value)
    assert not ydl.calls, "not a single yt-dlp call may happen before the refusal"


def test_node_older_than_the_permission_switch_is_not_a_runtime(js_registry):
    """yt-dlp considers node < 23.5 usable, but the solver does not work on it at
    all: under `--experimental-permission` even an empty script from stdin fails
    (ERR_ACCESS_DENIED on realpathSync, checked on node 22.22.2). Counting it as a
    runtime would bring back the same silent degradation."""
    js_registry(node=(22, 22, 2))
    assert sources._js_runtimes() == ()
    js_registry(node=(23, 5, 0))
    assert sources._js_runtimes() == ("node",)


def test_youtube_gets_a_client_that_actually_downloads(tmp_path, ydl):
    """android_vr answers 403 without a PO token and web_safari gives SABR with no
    direct links; web_embedded is what actually downloads. The argument is addressed
    to the youtube extractor, so it is harmless on a boosty download."""
    src = _boosty_two_videos()
    src.fetch(src.list()[0], tmp_path / "raw")

    cmd = ydl.calls[-1]
    arg = cmd[cmd.index("--extractor-args") + 1]
    assert arg.startswith("youtube:player_client=")
    assert "-android_vr" in arg and "web_embedded" in arg


# ------------------------------------------------------ which yt-dlp exactly

def test_ytdlp_is_module_of_this_interpreter_not_path(tmp_path, ydl):
    """A machine can carry two different yt-dlp installs (one in the virtualenv, one
    system wide) whose behaviour on embeds diverges. We call the module of OUR OWN
    interpreter, otherwise the version depends on the caller's PATH."""
    src = _boosty_two_videos()
    src.fetch(src.list()[0], tmp_path / "raw")
    assert ydl.calls[-1][:3] == [sys.executable, "-m", "yt_dlp"]


# ------------------------------------------------ resume of a closed course

def test_closed_course_resume_restores_browser_from_source_json(tmp_path, monkeypatch):
    """`coursedump run` with no arguments continues a CLOSED course: the browser for
    cookies is taken from source.json. Without that a closed post fails on the very
    first request to the source (title), so a boosty resume did not work at all."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe required")
    out = tmp_path / "out"
    cdir = out / "post"
    cdir.mkdir(parents=True)
    (cdir / "source.json").write_text(json.dumps(
        {"source": POST, "adapter": "ytdlp", "url": POST, "cookies": "firefox",
         "title": "post"}, ensure_ascii=False), encoding="utf-8")

    info = {"title": "post", "_type": "playlist", "id": "f897a4cc", "webpage_url": POST,
            "entries": [{"title": "Lesson", "id": "e73044e1", "playlist_index": 1,
                         "webpage_url": POST, "duration": 7088,
                         "url": "https://cdn.example.test/media/resume.mp4"}]}
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        authorized = "--cookies-from-browser" in cmd
        if "-J" in cmd:  # a closed post gives no metadata without cookies
            if not authorized:
                return _proc(1, "", "ERROR: Unable to download webpage: HTTP Error 403")
            return _proc(0, json.dumps(info), "")
        assert authorized, "a download without cookies on a closed post"
        path = Path(cmd[cmd.index("-o") + 1][: -len(".%(ext)s")].replace("%%", "%") + ".m4a")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        return _proc(0, "e73044e1\n", "")

    monkeypatch.setattr(sources, "run", fake_run)
    # no arguments -> the cli takes sources from known_courses and passes NO cookies
    assert [s for s, _ in executor.known_courses(out)] == [POST]
    stats = executor.run_course(POST, executor.Opts(out_root=out, asr_backend="dummy"))

    assert stats["done"] == 1 and stats["errors"] == 0
    assert stats["course"] == str(cdir)  # the same directory, not a second course beside it
    assert calls and all(c[:5] == [sys.executable, "-m", "yt_dlp",
                                   "--cookies-from-browser", "firefox"] for c in calls)
    assert json.loads((cdir / "source.json").read_text(encoding="utf-8"))["cookies"] == "firefox"


def test_resume_works_when_source_unreachable(tmp_path, monkeypatch):
    """The source does not answer at all (stale cookies, no network), but what is
    already downloaded must still reach the text: title and directory come from
    disk, the manifest from cache. src.title() used to fail BEFORE the fallback to
    cache and killed the whole resume."""
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe required")
    out = tmp_path / "out"
    cdir = out / "post"
    (cdir / "raw").mkdir(parents=True)
    (cdir / "source.json").write_text(json.dumps(
        {"source": POST, "adapter": "ytdlp", "url": POST, "cookies": "firefox",
         "title": "post"}, ensure_ascii=False), encoding="utf-8")
    item = sources.Item(rel="001 - Lesson", kind="video", remote=POST, index=1,
                        target="001 - Lesson.md")
    (cdir / "manifest.jsonl").write_text(item.to_json() + "\n", encoding="utf-8")
    (cdir / "raw" / "001 - Lesson.m4a").write_bytes(b"audio")

    monkeypatch.setattr(sources, "run",
                        lambda cmd, **kw: _proc(1, "", "ERROR: HTTP Error 403"))
    stats = executor.run_course(POST, executor.Opts(out_root=out, asr_backend="dummy"))

    assert stats["errors"] == 0
    assert (cdir / "text" / "001 - Lesson.md").exists()


# ---------------------------------------------------------------- cookies

def test_cookies_go_through_browser_flag_not_a_file(tmp_path, ydl):
    """Secrets are never materialised: only --cookies-from-browser, no
    --cookies <file> and no cookie files on disk."""
    src = _boosty_two_videos()
    src.cookies = "firefox"
    src.fetch(src.list()[0], tmp_path / "raw")

    cmd = ydl.calls[-1]
    assert cmd[:5] == [sys.executable, "-m", "yt_dlp", "--cookies-from-browser", "firefox"]
    assert "--cookies" not in cmd
    assert not any("cookie" in p.name.lower() for p in tmp_path.rglob("*"))
