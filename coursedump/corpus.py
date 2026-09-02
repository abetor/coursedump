"""Corpus output: an authorized post queue becomes flat, headed text files.

This is a second output contract beside normal course snapshots. Course output
uses Markdown with frontmatter under `out/<course>/text/`; corpus output uses
flat `.txt` files with `# source:` and `# metadata:` headers.

The queue supplies metadata that may not exist at the source: title, tags,
date, and level. The date is required because downstream summaries can weight
fresh material more heavily.

Deduplication uses the post UUID in each source header and the files themselves
as the ledger. The verdict is computed before any source request.
"""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from . import manifest
from .util import atomic_write_text

HEAD_SRC = "# source: "
HEAD_META = "# metadata: "
HEAD_TYPE = "# type: "
TEXT_POST_TYPE = "Boosty text post"

# Filesystem component limit for APFS and ext4. Earlier batch processing did
# not enforce it, so a long title could fail with ENAMETOOLONG. See _fit.
MAX_NAME_BYTES = 255

# Post UUID shape: https://boosty.to/<creator>/posts/<uuid>
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_POST_PATH = re.compile(
    rf"^/(?P<blog>[^/]+)/posts/(?P<uuid>{_UUID.pattern})/?$", re.I)

# Corpus metadata is '<title> | <tags> | <date> | <level>'. The date is
# required because summaries weight freshness. At least four fields are needed.
# A fifth field such as a part marker or media origin is accepted. An extra
# field is harmless, while a missing one moves the date away from the position
# expected by corpus readers.
META_SHAPE = "<title> | <tags> | <date> | <level>"
DATE_FIELD = 3   # 1-based metadata field containing the date

_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july",
                "august", "september", "october", "november", "december")
# Accept both full month names and three-letter abbreviations used by queues.
_MONTHS = {n[:k]: i for i, n in enumerate(_MONTH_NAMES, 1) for k in (3, len(n))}

_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_MDY = re.compile(r"([A-Za-z]+) (\d{1,2}),? (\d{4})")


def _post_match(url: str):
    """Return a post-URL match with blog and UUID groups, or None."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    if host != "boosty.to" and not host.endswith(".boosty.to"):
        return None
    return _POST_PATH.fullmatch(parsed.path)


def post_blog(url: str) -> str:
    """Return the blog name from a post URL, or an empty string.

    A blog is one corpus source, so staging is grouped by blog rather than
    mixing post snapshots with course snapshots in the shared out/ directory.
    """
    m = _post_match(url)
    return m.group("blog") if m else ""


def post_uuid(url: str) -> str:
    """Return the post UUID used for deduplication, or an empty string.

    UUID identity avoids false matches between posts with the same title and
    remains stable across share-query, scheme, and trailing-slash URL variants.
    There is deliberately no normalized-URL fallback: arbitrary URL keys can
    contain path separators and would violate the flat corpus contract.
    """
    m = _post_match(url)
    return m.group("uuid").lower() if m else ""


def url_problem(url: str) -> str:
    """Return why a URL is invalid for corpus processing, or an empty string."""
    if not post_uuid(url):
        return ("URL must have the form "
                "http(s)://[<subdomain>.]boosty.to/<blog>/posts/<uuid> "
                "(the UUID provides the deduplication key and filename; the "
                "corpus command has no generic URL mode)")
    return ""


@dataclass(frozen=True)
class Line:
    """A queue line containing a post URL and header metadata."""
    lineno: int
    url: str
    meta: str

    @property
    def key(self) -> str:
        return post_uuid(self.url)

    @property
    def problem(self) -> str:
        """Return why this line is invalid, or an empty string."""
        return url_problem(self.url) or meta_problem(self.meta)


def read_queue(path: Path) -> list[Line]:
    """Read `<url><spaces><metadata>` lines, with `#` comments.

    Parsing follows `while read -r url rest`: the first field is the URL and
    the trimmed remainder is metadata.
    """
    out: list[Line] = []
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        parts = raw.split(None, 1)
        if not parts or parts[0].startswith("#"):
            continue
        out.append(Line(i, parts[0], parts[1].strip() if len(parts) > 1 else ""))
    return out


def is_date(field: str) -> bool:
    """Return whether the entire metadata field is a valid calendar date.

    This is not a substring search: a year in a title or tag is not the post
    date. Accepted forms are ISO and English month-name dates.
    """
    s = field.strip()
    m = _ISO.fullmatch(s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
    else:
        m = _MDY.fullmatch(s)
        if not m:
            return False
        name, day, year = m.groups()
        mo = _MONTHS.get(name.lower(), 0)
        y, d = int(year), int(day)
    try:
        date(y, mo, d)
    except ValueError:
        return False
    return True


def meta_problem(meta: str) -> str:
    """Return why metadata is invalid for the corpus, or an empty string.

    Validation fails closed and requires the date in the third field, rather
    than accepting a date-like substring elsewhere.
    """
    if not meta:
        return f"metadata is missing (expected '{META_SHAPE}')"
    fields = [f.strip() for f in meta.split("|")]
    if len(fields) < 4:
        return (f"metadata has {len(fields)} fields; at least 4 are required ('{META_SHAPE}')")
    if not is_date(fields[DATE_FIELD - 1]):
        return (f"metadata field {DATE_FIELD} is not a calendar date: "
                f"'{fields[DATE_FIELD - 1]}' (expected '{META_SHAPE}'; "
                "accepted forms include 'Dec 09, 2024' and 2026-08-10)")
    return ""


@dataclass(frozen=True)
class Verdict:
    status: str        # NEW | DONE
    key: str
    why: str           # evidence: what matched, or what was searched

    @property
    def done(self) -> bool:
        return self.status == "DONE"


# New post parts use '<title> - part N'; accept the escaped legacy Russian
# marker when reading an existing corpus. A collision adds ' (<8 hex>)'.
_PART = re.compile(
    r" - (?:part|\u0447\u0430\u0441\u0442\u044c) (\d+)(?: \([0-9a-f]{8}\))?$"
)


def is_completion_proof(name: str) -> bool:
    """Return whether a filename proves a post export completed.

    A single-file post or part one is the proof. Export writes part one last,
    so a process death between renames leaves the post NEW and resumable.
    """
    m = _PART.search(Path(name).stem)
    return m is None or m.group(1) == "1"


class Ledger:
    """Treat the corpus files themselves as the processing ledger.

    Keys come from live file headers, with no database or one-time import.
    Only the first line of each file is read.
    """

    def __init__(self, root: Path, by_key: dict[str, list[str]],
                 url_of: dict[str, str], headless: list[str],
                 unreadable: list[str], no_uuid: list[str]) -> None:
        self.root = root
        self.by_key = by_key
        self.url_of = url_of        # key -> header URL, retained as evidence
        self.headless = headless    # files without a header are invisible to dedup
        self.unreadable = unreadable  # files exist but could not be read
        self.no_uuid = no_uuid      # header exists but has no post UUID

    @classmethod
    def scan(cls, root: Path) -> "Ledger":
        by_key: dict[str, list[str]] = {}
        url_of: dict[str, str] = {}
        headless: list[str] = []
        unreadable: list[str] = []
        no_uuid: list[str] = []
        for p in sorted(root.glob("*.txt")) if root.is_dir() else []:
            try:
                with p.open(encoding="utf-8", errors="replace") as f:
                    first = f.readline()
            except FileNotFoundError:
                continue   # the file vanished between glob and read
            except OSError as e:
                # Any other read error is not equivalent to a missing file. The
                # requested UUID may be in this file, and silently continuing
                # would declare the post NEW and fetch it again. An incomplete
                # ledger must fail the run instead of changing the verdict.
                unreadable.append(f"{p.name}: {e.strerror or e}")
                continue
            if not first.startswith(HEAD_SRC):
                headless.append(p.name)
                continue
            url = first[len(HEAD_SRC):].strip()
            key = post_uuid(url)
            if not key:
                # The post UUID is the deduplication key. A header without it
                # makes the file indistinguishable from an unrelated post.
                no_uuid.append(f"{p.name}: '{url}'")
                continue
            by_key.setdefault(key, []).append(p.name)
            url_of.setdefault(key, url)
        return cls(root, by_key, url_of, headless, unreadable, no_uuid)

    @property
    def files(self) -> int:
        return (sum(len(v) for v in self.by_key.values()) + len(self.headless)
                + len(self.unreadable) + len(self.no_uuid))

    def verdict(self, url: str) -> Verdict:
        """Return an evidence-backed NEW or DONE verdict.

        `url_problem` must reject URLs without UUIDs before this point.
        """
        key = post_uuid(url)
        if not key:
            raise ValueError(f"URL has no Boosty post UUID: {url}")
        hit = self.by_key.get(key)
        if hit and any(is_completion_proof(n) for n in hit):
            return Verdict("DONE", key,
                           f"UUID {key} is already in the corpus: {', '.join(hit)} "
                           f"(header '{HEAD_SRC}{self.url_of[key]}')")
        if hit:  # Parts exist without a completion proof: the post is partial.
            return Verdict("NEW", key,
                           f"UUID {key} is INCOMPLETE in the corpus: {', '.join(hit)}; "
                           "part one (the completion proof) is missing, so a rerun "
                           "will finish the post")
        return Verdict("NEW", key,
                       f"UUID {key} does not appear in any '{HEAD_SRC}' header "
                       f"({self.files} files in {self.root})")


# ---------------------------------------------------------------- file format

def render(url: str, meta: str, body: str, record_type: str = "") -> str:
    """Render the corpus wrapper used by the historical batch export.

        { echo "# source: $url"; [[ -n "${rest:-}" ]] && echo "# metadata: $rest";
          echo; cat "$txt"; } > "$txt.tmp"

    The wrapper consists of a header, one blank line, and the body. The ASR
    body ends in exactly one newline. Metadata is preserved verbatim because
    it is the remainder of the queue line. ``record_type`` stays empty for
    media; a text post adds the agreed third ``# type:`` header and therefore
    does not claim byte-for-byte compatibility with the older wrapper.
    """
    head = f"{HEAD_SRC}{url}\n"
    if meta:
        head += f"{HEAD_META}{meta}\n"
    if record_type:
        head += f"{HEAD_TYPE}{record_type}\n"
    body = body.strip("\n")
    return head + "\n" + (body + "\n" if body else "")


def md_body(md: str) -> str:
    """Return snapshot content without front matter or the title heading.

    Extractors produce ``---\\n...\\n---\\n# <title>\\n\\n<text>\\n``. The
    corpus has its own header, so both structural sections are removed. The
    remaining text is preserved because repeat collapse has already run; that
    changes content quality, not the wrapper format.
    """
    text = md
    if text.startswith("---\n"):
        _, sep, rest = text[4:].partition("\n---\n")
        if sep:
            text = rest
    if text.startswith("# "):
        text = text.partition("\n")[2]
    return text.strip("\n")


def _sanitize(title: str) -> str:
    """Derive a filename from an external title with yt-dlp's own rules.

    The earlier batch used ``--print filename`` with
    ``-o '%(title)s.%(ext)s'``, which resolves through this function, including
    '/' -> '⧸' and ':' -> '：'. A local list of unsafe characters would always
    be incomplete, so the implementation calls the owning engine. The import
    is lazy because yt-dlp is heavy and every CLI command starts a process.
    """
    from yt_dlp.utils import sanitize_filename
    return sanitize_filename(title)


def _fit(base: str, tail: str) -> str:
    """Fit a filename component without trimming its disambiguating suffix."""
    budget = MAX_NAME_BYTES - len((tail + ".txt").encode("utf-8"))
    while len(base.encode("utf-8")) > budget:
        base = base[:-1]
    return base.rstrip() + tail


_REL_DECOR = re.compile(r"^\d{3} - |\s\[[^\[\]]+\]$")


def rel_title(rel: str) -> str:
    """Derive a fallback title from ``rel`` by removing generated decoration.

    This is used when no raw title exists, such as an older manifest or a
    non-yt-dlp source. It is deliberately a fallback: applying this cleanup to
    a real title could remove meaningful leading text.
    """
    return _REL_DECOR.sub("", Path(rel).name)


def file_stem(title: str, key: str, part: int = 0, total: int = 1) -> str:
    """Return a corpus filename stem according to the corpus convention."""
    base = _sanitize(title.strip()).strip() or key
    if base == "Video":
        # A generic embedded-video title collides across posts. Disambiguate it
        # with the post ID, following the historical batch convention.
        base = f"Video-{key}"
    if total == 1 and _PART.search(base):
        # One video with a natural '<something> - part N' title is not a
        # transaction part. Without a distinct suffix Ledger would treat it as
        # an interrupted multi-video post and return NEW on every rerun. Square
        # brackets intentionally differ from the collision suffix '(<uuid>)',
        # which _PART accepts for real transaction parts.
        return _fit(base, f" [{key[:8]}]")
    # A post with N videos becomes N files with the same header. Numbering
    # follows the established corpus convention.
    return _fit(base, f" - part {part}" if total > 1 else "")


def head_uuid(path: Path) -> str:
    """Read a post UUID from a file header, or return "" if unavailable."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            first = f.readline()
    except OSError:
        return ""
    if not first.startswith(HEAD_SRC):
        return ""
    return post_uuid(first[len(HEAD_SRC):].strip())


def target_path(corpus_dir: Path, stem: str, key: str,
                taken: set[Path] | None = None) -> Path:
    """Choose a path that preserves unrelated files but can replace our own.

    A conflicting filename from another post is disambiguated with the UUID
    prefix. An existing file for this same post, identified by its header UUID,
    is an incomplete publication and may be replaced; otherwise a resume would
    create suffixed duplicates. ``taken`` contains names reserved by the same
    transaction, which is planned completely before the first write.
    """
    taken = taken or set()
    for name in (f"{stem}.txt", f"{stem} ({key[:8]}).txt"):
        p = corpus_dir / name
        if p in taken:
            continue
        if not p.exists() or head_uuid(p) == key:
            return p
    raise FileExistsError(
        f"both {stem}.txt and {stem} ({key[:8]}).txt are occupied in {corpus_dir}; "
        "refusing to overwrite an unrelated corpus file")


def export(course_dir: Path, line: Line, corpus_dir: Path) -> tuple[list[Path], list[str]]:
    """Export a course snapshot into corpus files.

    Return ``(written, incomplete)``. Publication is transactional at the post
    level: if any video has not been extracted, no part enters the corpus.
    Otherwise the first published part would carry the post UUID and permanent
    deduplication could incorrectly mark the whole post DONE.

    All Markdown files are checked first, then names and collisions are
    resolved, and only then are files written. The completion-proof file is
    written last so a process death between renames cannot look complete. Body
    text comes from the completed snapshot on disk, allowing an interrupted
    export to resume without revisiting the authorized source.
    """
    items = [it for it in manifest.load(course_dir / "manifest.jsonl") if it.target]
    if not items:
        return [], ["<the course manifest has no processable items>"]
    missing = [it.rel for it in items
               if not (course_dir / "text" / it.target).exists()]
    if missing:
        return [], missing   # A partial post publishes nothing.

    plan: list[tuple[Path, str]] = []
    taken: set[Path] = set()
    for n, it in enumerate(items, 1):
        stem = file_stem(it.title or rel_title(it.rel), line.key,
                         part=it.index or n, total=len(items))
        path = target_path(corpus_dir, stem, line.key, taken)
        taken.add(path)
        md = (course_dir / "text" / it.target).read_text(encoding="utf-8")
        plan.append((path, render(line.url, line.meta, md_body(md))))

    # Write completion evidence last. The stable sort preserves part order
    # within each group.
    for path, text in sorted(plan, key=lambda pt: is_completion_proof(pt[0].name)):
        atomic_write_text(path, text)
    return [p for p, _ in plan], []


def export_text_post(line: Line, title: str, body: str, corpus_dir: Path) -> Path:
    """Atomically export an API text post into the same flat corpus."""
    fallback_title = line.meta.split("|", 1)[0].strip()
    stem = file_stem(title or fallback_title, line.key)
    path = target_path(corpus_dir, stem, line.key)
    atomic_write_text(
        path,
        render(line.url, line.meta, body, record_type=TEXT_POST_TYPE),
    )
    return path
