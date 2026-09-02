"""Source adapters: local, rclone, yt-dlp, and direct HTTP documents.

Interface:
    title()              -> course title used for the slug
    list()               -> list[Item]
    fetched(item, raw)   -> downloaded Path or None
    fetch(item, raw)     -> download and return Path

rclone and yt-dlp own partial-file handling, resume, and retries. This project
does not reimplement those downloaders.
"""

import functools
import glob
import json
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from . import normalize
from .manifest import Item, kind_of, VIDEO, AUDIO
from .util import require_tool, run


class SourceError(RuntimeError):
    pass


# Adapter notices, including pacing, go to stderr. The executor owns stdout for
# its Rich live progress display, which unrelated output would corrupt.
_console = Console(stderr=True)


# ---------------------------------------------------------------- local

class LocalSource:
    is_local = True

    def __init__(self, root: Path, only: str = ""):
        self.root = root.resolve()
        self.only = only  # relative name when a one-file source is the course

    def describe(self) -> dict:
        return {"adapter": "local", "root": str(self.root), "only": self.only}

    def title(self) -> str:
        return Path(self.only).stem if self.only else self.root.name

    def list(self) -> list[Item]:
        if self.only:
            p = self.root / self.only
            return [Item(rel=self.only, kind=kind_of(self.only), size=p.stat().st_size)]
        items = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file() or normalize.is_junk(p.name):
                continue
            rel = p.relative_to(self.root).as_posix()
            if any(normalize.is_junk(c) for c in rel.split("/")):
                continue
            items.append(Item(rel=rel, kind=kind_of(rel), size=p.stat().st_size))
        return items

    def fetched(self, item: Item, raw_dir: Path) -> Path | None:
        return self.root / item.rel

    def fetch(self, item: Item, raw_dir: Path) -> Path:
        return self.root / item.rel  # no-op: read local input in place


# ---------------------------------------------------------------- rclone

class RcloneSource:
    is_local = False

    def __init__(self, spec: str, display: str):
        require_tool("rclone", "brew install rclone; then run rclone config once")
        self.spec = spec          # for example "gdrive,root_folder_id=XXX:" or "remote:path"
        self.display = display

    def describe(self) -> dict:
        return {"adapter": "rclone", "spec": self.spec, "display": self.display}

    def title(self) -> str:
        p = run(["rclone", "lsjson", "--stat", self.spec])
        if p.returncode == 0:
            name = json.loads(p.stdout or "{}").get("Name") or ""
            if name:
                return name
        tail = self.spec.rstrip(":").split(":")[-1].rstrip("/").split("/")[-1]
        return tail or self.display

    def list(self) -> list[Item]:
        p = run(["rclone", "lsjson", "-R", "--files-only", self.spec], timeout=600)
        if p.returncode != 0:
            raise SourceError(f"rclone lsjson failed: {p.stderr.strip()[:400]}")
        items = []
        for e in json.loads(p.stdout):
            rel = e["Path"]
            if normalize.is_junk(Path(rel).name):
                continue
            items.append(Item(rel=rel, kind=kind_of(rel), size=e.get("Size", 0) or 0, remote=rel))
        items.sort(key=lambda i: i.rel)
        return items

    def fetched(self, item: Item, raw_dir: Path) -> Path | None:
        p = raw_dir / item.rel
        return p if p.exists() else None

    def fetch(self, item: Item, raw_dir: Path) -> Path:
        dst = raw_dir / item.rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        src = self.spec + ("" if self.spec.endswith(":") else "/") + item.remote
        p = run(["rclone", "copyto", "--retries", "3", "--low-level-retries", "10",
                 src, str(dst)], timeout=6 * 3600)
        if p.returncode != 0 or not dst.exists():
            raise SourceError(f"rclone copyto {item.rel}: {p.stderr.strip()[:400]}")
        return dst


def _rclone_remotes() -> dict[str, str]:
    """Map remote names to types from the rclone configuration."""
    p = run(["rclone", "config", "dump"])
    if p.returncode != 0:
        return {}
    return {name: cfg.get("type", "") for name, cfg in json.loads(p.stdout or "{}").items()}


def _remote_of_type(t: str, hint: str) -> str:
    for name, typ in _rclone_remotes().items():
        if typ == t:
            return name
    raise SourceError(
        f"No rclone remote of type '{t}' is configured. Run rclone config once ({hint})")


# ---------------------------------------------------------------- yt-dlp

# Invoke the current interpreter rather than a binary name from PATH. Multiple
# yt-dlp versions may coexist and differ on embedded media. Module execution
# selects the version locked for this environment.
YTDLP = [sys.executable, "-m", "yt_dlp"]

# yt-dlp launches Node older than 23.5.0 with --experimental-permission. Those
# releases can reject even a stdin script while resolving its entry module via
# realpathSync, producing ERR_ACCESS_DENIED. yt-dlp may still call that runtime
# usable, but its solver fails, creating the silent degradation guarded here.
_NODE_MIN = (23, 5, 0)


@functools.lru_cache(maxsize=1)
def _js_runtimes() -> tuple[str, ...]:
    """Return runtimes found by yt-dlp in yt-dlp priority order.

    Let the consumer discover its runtimes instead of duplicating PATH and
    version heuristics. Cache the result because probing starts executables.
    """
    import yt_dlp  # noqa: F401 - importing also registers runtimes
    from yt_dlp.globals import supported_js_runtimes

    found = []
    for name, cls in supported_js_runtimes.value.items():
        info = cls().info if cls else None
        if info is None or not info.supported:
            continue
        if name == "node" and info.version_tuple < _NODE_MIN:
            continue
        found.append(name)
    return tuple(found)


def _js_runtime_args() -> list[str]:
    """Build explicit yt-dlp runtime arguments or fail before a download.

    Without a supported runtime, yt-dlp can silently expose an incomplete
    YouTube format list and fail later. Passing all discovered runtimes lets
    yt-dlp choose according to its own priority.
    """
    names = _js_runtimes()
    if not names:
        raise SourceError(
            "yt-dlp needs a JavaScript runtime for complete YouTube format "
            "discovery. Install deno, node >= 23.5, quickjs, or bun and retry; "
            "for example: brew install deno")
    return [arg for name in names for arg in ("--js-runtimes", name)]


# Default YouTube clients may expose no downloadable audio: one can fall back
# to SABR images while another requires a token not requested by yt-dlp. The
# embedded web client provides a usable fallback. This extractor argument is
# scoped to YouTube and does not affect other source adapters in the same run.
YT_CLIENTS = ["--extractor-args", "youtube:player_client=default,-android_vr,web_embedded"]


# ------------------------------------------------- source policy profile

@dataclass(frozen=True)
class SourceProfile:
    """Download settings bound to a source rather than a CLI command.

    Source-specific limits apply by default and are disabled only through the
    explicit `--full-video` and `--no-throttle` flags.
    """
    name: str
    fmt: str                # -f selection; "" keeps yt-dlp's full-video default
    limit_rate: str         # --limit-rate
    pause: tuple[int, int]  # seconds between downloads; none before the first


# Conservative defaults reduce source load for lawful, user-authorized bulk
# downloads. Audio-only mode also avoids transferring video that ASR does not
# need. The 600..1199 interval deliberately preserves the original pacing
# contract; random.randint includes both endpoints.
BOOSTY = SourceProfile(name="boosty", fmt="bestaudio[abr<=70]/bestaudio",
                       limit_rate="1.5M", pause=(600, 1199))

# Test injection points. Tests must not really sleep, but the presence and
# bounds of pacing are part of the contract.
_sleep = time.sleep
_randint = random.randint


class RunPacer:
    """Pace source requests within one run.

    The run owns one pacer shared by its adapters, so pauses apply between any
    two downloads for the same source profile but never leak into another run.
    """

    def __init__(self) -> None:
        self._done: dict[str, int] = {}

    def pause_before_download(self, profile: SourceProfile | None) -> None:
        """Pause 600-1199 seconds before every download except the run's first.

        An already downloaded item does not call fetch, consume a pause, or
        advance the counter.
        """
        if profile is None:
            return
        if self._done.get(profile.name):
            secs = _randint(*profile.pause)
            _console.print(f"[dim]{profile.name}: waiting {secs}s before download "
                           f"(source-profile pacing; disable with --no-throttle)[/dim]")
            _sleep(secs)
        self._done[profile.name] = self._done.get(profile.name, 0) + 1


def _profile_for(url: str) -> SourceProfile | None:
    """Return a host-specific profile; unrelated sources receive none."""
    host = urllib.parse.urlparse(url).netloc.lower().rsplit("@", 1)[-1].split(":")[0]
    # A trailing dot denotes the same DNS host, but plain string comparison
    # would miss it and silently disable the profile.
    host = host.rstrip(".")
    if host == "boosty.to" or host.endswith(".boosty.to"):
        return BOOSTY
    return None


# Last-resort estimate when only duration is known. The 8 Mbit/s full-video
# rate is deliberately conservative relative to observed large formats, and
# the later free-space guard adds its own safety margin. A lower historical
# estimate understated full-video downloads because they select best video,
# not only an audio track.
_FALLBACK_BPS = 8_000_000 / 8  # 8 Mbit/s = 1 MB/s

# The audio-only profile needs a separate fallback. Applying video bitrate
# would overestimate by roughly an order of magnitude and cause false
# insufficient-space failures. 320 kbit/s is a conservative empirical bound,
# not a guarantee for every source or fallback format.
_FALLBACK_AUDIO_BPS = 320_000 / 8


def _fmt_size(f: dict, dur: float) -> int:
    """Estimate one format in bytes, or return zero when unknown."""
    for key in ("filesize", "filesize_approx"):
        if f.get(key):
            return int(f[key])
    tbr = float(f.get("tbr") or 0)  # kbit/s
    return int(dur * tbr * 1000 / 8) if tbr and dur else 0


def _est_size(e: dict, audio_only: bool = False) -> int:
    """Estimate an upper-bound item size, or return zero when unknown.

    Prefer an explicit item size, then the selected formats, then duration and
    a fallback bitrate. In audio-only mode, estimate the audio track rather
    than treating the full video size as the expected transfer.
    """
    whole = next((int(e[k]) for k in ("filesize", "filesize_approx") if e.get(k)), 0)
    dur = float(e.get("duration") or 0)
    fmts = [f for f in (e.get("formats") or []) if isinstance(f, dict)]
    if audio_only:
        # Upper audio bound: the profile asks for <=70 kbit/s, but its
        # /bestaudio fallback may select the heaviest track.
        audio = max((_fmt_size(f, dur) for f in fmts if f.get("vcodec") == "none"),
                    default=0)
        if audio:
            return audio
        if dur:
            return int(dur * _FALLBACK_AUDIO_BPS)
        return whole
    if whole:
        return whole
    if fmts:
        # Fetch invokes yt-dlp without -f, using bestvideo*+bestaudio/best. The
        # upper estimate is the heaviest format plus the heaviest separate
        # audio track, which is merged when the selected video has no audio.
        heaviest = max((_fmt_size(f, dur) for f in fmts), default=0)
        audio = max((_fmt_size(f, dur) for f in fmts if f.get("vcodec") == "none"),
                    default=0)
        if heaviest:
            return heaviest + audio
    if not dur:
        return 0
    tbr = float(e.get("tbr") or 0)  # kbit/s when the source supplies bitrate
    return int(dur * (tbr * 1000 / 8 if tbr else _FALLBACK_BPS))


class YtdlpSource:
    is_local = False

    def __init__(self, url: str, cookies_from_browser: str = "",
                 full_video: bool = False, no_throttle: bool = False,
                 pacer: RunPacer | None = None):
        self.url = url
        self.cookies = cookies_from_browser
        # Resolve the runtime here rather than in _ydl. The adapter is created
        # before any source request, so a missing runtime fails clearly at
        # course start instead of after an hour of downloads and pacing.
        self._js = _js_runtime_args()
        # Caller flags survive canonical-URL reclassification in _apply_profile.
        self._full_video = full_video
        self._no_throttle = no_throttle
        # The run provides one pacer through executor.Opts -> detect, sharing
        # spacing across adapters. Standalone construction, such as planning or
        # a unit test, receives a private pacer for one adapter.
        self.pacer = pacer if pacer is not None else RunPacer()
        self.profile: SourceProfile | None = None
        self.fmt = ""                              # -f: "" keeps yt-dlp's default
        self.throttle: SourceProfile | None = None
        self._apply_profile(_profile_for(url))
        self._info: dict | None = None

    def _apply_profile(self, profile: SourceProfile | None) -> None:
        """Enable a source profile without implicitly disabling one.

        Only explicit caller flags may disable profile behavior, so later
        classification by canonical URL can only strengthen the profile.
        """
        if profile is None or self.profile is not None:
            return
        self.profile = profile
        self.fmt = self._fmt_for(profile)
        self.throttle = self._throttle_for(profile)

    # Centralize caller flags so each half of a profile is disabled explicitly.
    def _fmt_for(self, profile: SourceProfile | None) -> str:
        return "" if profile is None or self._full_video else profile.fmt

    def _throttle_for(self, profile: SourceProfile | None) -> SourceProfile | None:
        return None if profile is None or self._no_throttle else profile

    def _profile_of(self, url: str) -> SourceProfile | None:
        """Return the container profile or one inferred from the item URL.

        A profiled container remains authoritative, while an item with its own
        profiled page can enable the profile inside an unprofiled container.
        """
        return self.profile or _profile_for(url)

    def adopt_cached(self, items: list[Item]) -> None:
        """Recover source classification from a cached manifest.

        When live resolution fails, canonical item URLs from the last successful
        resolution can still enable the correct source profile for resume.
        """
        for it in items:
            self._apply_profile(_profile_for(it.remote))

    def describe(self) -> dict:
        # source.json is human-readable evidence for later diagnosis, including
        # why a download was paced. Only cookies and title are read back. The
        # source profile must be selected again instead of inheriting a
        # one-off disable flag during resume.
        th = self.throttle
        return {"adapter": "ytdlp", "url": self.url, "cookies": self.cookies,
                "profile": self.profile.name if self.profile else "",
                "format": self.fmt,
                "throttle": f"{th.limit_rate}, pauses {th.pause[0]}-{th.pause[1]} s" if th else ""}

    def _ydl(self, args: list[str], timeout: int) -> subprocess.CompletedProcess:
        cmd = list(YTDLP)
        if self.cookies:
            cmd += ["--cookies-from-browser", self.cookies]
        cmd += self._js + YT_CLIENTS
        return run(cmd + args, timeout=timeout)

    def _load(self) -> dict:
        if self._info is None:
            p = self._ydl(["-J", "--flat-playlist", self.url], timeout=600)
            if p.returncode != 0:
                raise SourceError(f"yt-dlp could not resolve {self.url}: {p.stderr.strip()[:400]}")
            self._info = json.loads(p.stdout)
            # Select the profile from the canonical URL, not only the caller's
            # text. A short link has a different host, while yt-dlp follows the
            # redirect and exposes the real URL as webpage_url. Without this
            # reclassification, the wrapper could silently lose its format,
            # rate-limit, and pacing policy.
            self._apply_profile(_profile_for(self._info.get("webpage_url") or ""))
        return self._info

    def title(self) -> str:
        return self._load().get("title") or "playlist"

    def _container_urls(self) -> set[str]:
        """Return every known URL for the container page itself.

        Caller input is not canonical: a share link may contain a query string
        or use HTTP. yt-dlp supplies the canonical ``webpage_url``. Comparing
        against it prevents a URL variant from silently disabling positional
        addressing and making every item fetch the entire container.
        """
        info = self._info or {}
        return {u.rstrip("/") for u in
                (self.url, info.get("webpage_url"), info.get("original_url")) if u}

    def _own_url(self, e: dict) -> str:
        """Return an item's own page URL, or "" when it has none.

        A playlist link may provide ``url`` entries with their own page. A post
        containing N videos can instead be represented as a playlist of already
        extracted entries: every entry has the container ``webpage_url``, while
        ``e['url']`` is a temporary media delivery URL tied to one format. Do
        not persist or reuse that capability URL; address the item by position.
        """
        if e.get("_type") in ("url", "url_transparent"):
            return e.get("url") or ""
        page = e.get("webpage_url") or e.get("original_url") or ""
        return "" if page.rstrip("/") in self._container_urls() else page

    def list(self) -> list[Item]:
        info = self._load()
        entries = info.get("entries")
        seq = entries if entries else [info]
        # Store the canonical container URL in items, not the caller's spelling.
        # It survives manifest caching and lets adopt_cached select the proper
        # profile without another resolution. Unlike a delivery URL, it is not
        # an expiring capability.
        page = info.get("webpage_url") or self.url
        items = []
        for i, e in enumerate(seq, 1):
            if not e:
                continue
            # Remove every control character, not only slash and NUL. A newline
            # in a title must not become part of a filesystem path.
            name = re.sub(r"[/\x00-\x1f\x7f]", " ", e.get("title") or e.get("id") or f"item-{i}")
            own = self._own_url(e)
            # Address an entry without its own page by position. A post with N
            # videos is N items, and the stable locator is the post page plus an
            # index. The entry delivery URL is temporary and must not be stored.
            index = 0
            if entries and not own:
                index = int(e.get("playlist_index") or i)
            # The video ID in the name is the item identity. Tool state is the
            # filesystem, so identity must live in the filename. Position is
            # unstable: inserting a video at the start shifts every index and
            # could otherwise make an old file look complete for a new video.
            vid = str(e.get("id") or "")
            # Select the profile per item, not only per container. An embedded
            # entry can require audio-only mode, which changes the space
            # estimate substantially. Fetch is too late to revise this estimate.
            remote = own or page
            fmt = self._fmt_for(self._profile_of(remote))
            # rel has no extension because yt-dlp selects it while downloading.
            items.append(Item(rel=f"{i:03d} - {name}" + (f" [{vid}]" if vid else ""),
                              kind="video", vid=vid,
                              size=_est_size(e, audio_only=bool(fmt)),
                              remote=remote, index=index,
                              # Keep the raw title rather than rel. Corpus output
                              # uses the same yt-dlp filename rules as the
                              # historical batch exporter.
                              title=e.get("title") or ""))
        return items

    def fetched(self, item: Item, raw_dir: Path) -> Path | None:
        base = raw_dir / item.rel
        # A title-derived name is data, not a glob. Without escaping, a title
        # such as '[SW] Lesson' becomes a character class, the downloaded file
        # is not found, and every resume downloads it again.
        pat = glob.escape(base.name) + ".*"
        for p in sorted(base.parent.glob(pat)) if base.parent.exists() else []:
            if p.suffix.lower() in VIDEO | AUDIO:
                return p
        return None

    def fetch(self, item: Item, raw_dir: Path) -> Path:
        raw_dir.mkdir(parents=True, exist_ok=True)
        # Percent signs in names are yt-dlp template syntax. Escape the complete
        # path except for the intentional .%(ext)s suffix.
        out_tpl = str(raw_dir / item.rel).replace("%", "%%") + ".%(ext)s"
        # An entry with its own URL uses --no-playlist. Otherwise address it by
        # position within the post or playlist.
        pick = ["--playlist-items", str(item.index)] if item.index else ["--no-playlist"]
        # Apply this item's profile, including format, rate limit, and pacing.
        # The item URL matters independently from the container so an embedded
        # entry and a cached manifest after failed resolution retain policy.
        profile = self._profile_of(item.remote)
        fmt = ["-f", f] if (f := self._fmt_for(profile)) else []
        throttle = self._throttle_for(profile)
        rate = ["--limit-rate", throttle.limit_rate] if throttle else []
        self.pacer.pause_before_download(throttle)
        p = self._ydl(["-o", out_tpl, *pick, *fmt, *rate,
                       "--write-subs", "--sub-langs", "ru.*,en.*",
                       # Print the ID that actually arrived. Positional addressing
                       # can shift between resolution and download if the source
                       # changes. after_move reports only after a successful move.
                       "--print", "after_move:%(id)s",
                       "--no-progress", item.remote], timeout=6 * 3600)
        got = self.fetched(item, raw_dir)
        if got is None:
            raise SourceError(f"yt-dlp {item.rel}: {p.stderr.strip()[:400]}")
        got_ids = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
        # Empty --print output is not successful verification. It can mean a
        # different yt-dlp version, an extractor without IDs, or a lost option.
        # Accepting the file would reintroduce the silent substitution this
        # check prevents, so remove it and report an item error.
        if item.vid and not got_ids:
            got.unlink()
            raise SourceError(
                f"{item.rel}: yt-dlp did not print the downloaded item ID "
                f"(--print after_move:%(id)s), so it cannot be checked against "
                f"expected {item.vid}; the unverified file was removed")
        if item.vid and item.vid not in got_ids:
            got.unlink()  # Otherwise resume could accept a different video.
            raise SourceError(
                f"{item.rel}: downloaded video {got_ids[0]}, expected {item.vid}; "
                "the source changed between resolution and download, so the file was removed")
        return got


# ---------------------------------------------------------------- direct HTTP file

class HttpSource:
    is_local = False

    def __init__(self, url: str):
        self.url = url
        name = Path(urllib.parse.urlparse(url).path).name or "file"
        self.name = urllib.parse.unquote(name)

    def describe(self) -> dict:
        return {"adapter": "http", "url": self.url}

    def title(self) -> str:
        return Path(self.name).stem

    def list(self) -> list[Item]:
        return [Item(rel=self.name, kind=kind_of(self.name), remote=self.url)]

    def fetched(self, item: Item, raw_dir: Path) -> Path | None:
        p = raw_dir / item.rel
        return p if p.exists() else None

    def fetch(self, item: Item, raw_dir: Path) -> Path:
        dst = raw_dir / item.rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        urllib.request.urlretrieve(self.url, tmp)
        tmp.rename(dst)
        return dst


# ---------------------------------------------------------------- source detection

_GDRIVE_FOLDER = re.compile(r"drive\.google\.com/drive/(?:u/\d+/)?folders/([\w-]+)")
_DOC_EXT = re.compile(r"\.(pdf|epub|docx?|pptx?|xlsx?|txt|md|mp3|m4a|zip)($|\?)", re.I)


def detect(source: str, cookies_from_browser: str = "",
           full_video: bool = False, no_throttle: bool = False,
           pacer: RunPacer | None = None):
    src = source.strip().rstrip("/") if source.strip().startswith(("http", "www.")) else source.strip()

    p = Path(src).expanduser()
    if p.exists():
        if p.is_file():
            return LocalSource(p.parent, only=p.name)
        return LocalSource(p)

    if src.startswith(("http://", "https://", "www.")):
        url = src if src.startswith("http") else "https://" + src
        host = urllib.parse.urlparse(url).netloc.lower()

        if "drive.google.com" in host or "docs.google.com" in host:
            m = _GDRIVE_FOLDER.search(url)
            if not m:
                raise SourceError(
                    "Google Drive sources must be folder links "
                    "(drive.google.com/drive/folders/...). Put a single file in a "
                    "folder or download the authorized file yourself.")
            remote = _remote_of_type("drive", "Google Drive type")
            return RcloneSource(f"{remote},root_folder_id={m.group(1)}:", display=url)

        if "cloud.mail.ru" in host:
            raise SourceError(
                "rclone cannot read public Mail.ru links. Save authorized content "
                "to your own cloud account, then run coursedump with a path on a "
                "configured Mail.ru rclone remote.")

        if _DOC_EXT.search(url):
            # A direct downloader would bypass the source profile. Fail closed
            # instead of silently skipping browser authentication, rate limits,
            # and pacing. Adding the full profile to HttpSource would create a
            # second download path, while an unauthorized response might be
            # saved under the expected media filename.
            profile = _profile_for(url)
            if profile is not None:
                raise SourceError(
                    f"{profile.name}: direct file URL "
                    f"({Path(urllib.parse.urlparse(url).path).name}) would bypass "
                    "browser authentication and source pacing. Provide the authorized "
                    "post URL for yt-dlp, or download the file yourself and use a local path.")
            return HttpSource(url)

        return YtdlpSource(url, cookies_from_browser,
                           full_video=full_video, no_throttle=no_throttle,
                           pacer=pacer)

    # rclone path in remote:path form
    m = re.match(r"^([\w.-]+):", src)
    if m and m.group(1) in _rclone_remotes():
        return RcloneSource(src, display=src)

    raise SourceError(
        f"Unrecognized source: {source!r} (not a path, URL, or configured rclone remote)"
    )
