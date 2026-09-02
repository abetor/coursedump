# tool-coursedump

Convert authorized course files, videos, subtitles, and documents into resumable Markdown corpora with local speech recognition.

[Quick start](#quick-start) | [Offline demo](#demo) | [Architecture](docs/DESIGN.md) | [Tests](#tests) | [Contributing and agent guide](AGENTS.md) | [MIT license](LICENSE)

## Problem

Course material often arrives as a mixture of local folders, archives, cloud
paths, web pages, media, subtitles, and office documents. Turning that mixture
into a consistent text corpus is repetitive, failure-prone, and expensive to
restart after an interruption.

## What it does

`coursedump` creates a structured Markdown snapshot while preserving the source
layout. It can:

- read local directories, ZIP archives, rclone paths, HTTP URLs, and sources
  supported by `yt-dlp`;
- prefer existing subtitles and use local speech recognition only when needed;
- extract PDF, Office, HTML, MHTML, text, and subtitle documents;
- resume from files already written to disk instead of starting over;
- process a durable queue with progress, ETA, retry, and optional event hooks;
- emit versioned JSON for machine consumers;
- build a flat, deduplicated text corpus from supported post feeds.

The output is an input corpus for search, indexing, or research tools. It is not
a summarizer or a semantic editor.

Use this software only for material you own or are authorized to download.
Respect the source service's terms, access controls, and rate limits.

## Architecture

The CLI resolves a source into a manifest of immutable items. A sequential
reconciliation loop then checks free space, fetches only missing raw inputs,
extracts text, writes each result atomically, records item errors, and rebuilds
`INDEX.md`. Completion is derived from the manifest and output files; there is
no database.

```text
source -> adapter -> manifest -> reconciliation loop -> extractors -> Markdown
                         |                |
                         |                +-> errors.jsonl
                         +-> source.json      INDEX.md
```

Adapters delegate remote transport to established tools such as `rclone` and
`yt-dlp`. Media extraction prefers subtitles, then uses a local ASR backend.
See [docs/DESIGN.md](docs/DESIGN.md) for contracts and failure semantics.

## Quick start

Use Python 3.13 with the bundled lockfile. The source requires Python 3.12+,
but locked third-party dependencies do not yet install on Python 3.14.
`ffmpeg` is required for media tests and processing; install `rclone` only
for rclone sources. On macOS, install the host tools with `brew install ffmpeg uv`.
On other systems, install `uv` and `ffmpeg` with your platform package manager.

From a source checkout:

```bash
git clone https://github.com/abetor/coursedump.git
cd coursedump
uv sync --python 3.13
uv run coursedump --help
```

For a self-contained first run, use the [offline demo](#demo) below.
To process an existing local directory, replace `./sample-course` with its path:

```bash
demo_root="$(mktemp -d)"
uv run coursedump plan ./sample-course --data "$demo_root/data"
uv run coursedump run ./sample-course --data "$demo_root/data" --asr dummy
uv run coursedump status --data "$demo_root/data"
```

For real media, omit `--asr dummy` to use the default local backend. On
non-Apple systems, install and select faster-whisper:

```bash
uv sync --extra fwhisper
uv run coursedump run ./sample-course --data "$demo_root/data" --asr fwhisper
```

`uv run coursedump doctor` checks optional processing capabilities too. A missing
ASR backend or JavaScript runtime can make it return 1 even when the offline
text demo is usable. Install the reported components for the sources you use.

## Demo

This offline demo creates a tiny course, runs the complete pipeline, and prints
the generated index:

```bash
demo_root="$(mktemp -d)"
mkdir -p "$demo_root/course/module-1"
printf '%s\n' \
  'This lesson explains how a local text source becomes a readable Markdown snapshot. The pipeline preserves the original directory structure, extracts the supplied lesson, writes its output atomically, and records the result in an index. Repeating the same command reuses completed files. This synthetic material contains no personal data and needs no network access, media download, or speech recognition model.' \
  > "$demo_root/course/module-1/lesson.txt"
uv run coursedump run "$demo_root/course" --data "$demo_root/data" --asr dummy
find "$demo_root/data/out" -name INDEX.md -exec sed -n '1,80p' {} \;
```

Machine consumers can request one JSON object on stdout:

```bash
uv run coursedump capabilities --json
uv run coursedump plan ./sample-course --data "$demo_root/data" --json
uv run coursedump run ./sample-course --data "$demo_root/data" --json
uv run coursedump status --data "$demo_root/data" --json
```

## Queue mode

`<data>/queue.txt` contains one source per line. Blank lines and comments are
ignored. Using the source created by the offline demo:

```bash
printf '%s\n' "$demo_root/course" > "$demo_root/data/queue.txt"
uv run coursedump queue plan --data "$demo_root/data"
uv run coursedump queue run --data "$demo_root/data"
uv run coursedump queue status --data "$demo_root/data"
```

Queue state and logs live under the selected data directory. An optional
`config.toml` may define one `on_event` shell command. Invalid or unknown
configuration fields fail before the queue lock is acquired.

## Data and credential boundary

Repository files contain code and synthetic test fixtures only. Course media,
transcripts, manifests, queue state, logs, models, and credentials must stay
outside the repository.

Data-root precedence is:

1. `--data DIR`
2. `COURSEDUMP_DATA`
3. `~/tools-data/coursedump-data`

`--out DIR` can override only the output directory. The default is convenient,
not mandatory; tests and automation should pass a temporary `--data` path.

Authenticated sources may use `--cookies-from-browser BROWSER`, which asks the
downstream downloader to read an existing browser session. `coursedump` does
not write cookie or token files. Do not put credential values, cookie exports,
signed media URLs, or downloaded content in issues, logs, fixtures, or commits.

## Limitations

- Russian survives in the test suite only, as test data: 87 lines across nine
  test files where the Russian text is itself the thing under test. Source
  comments and docstrings, CLI messages, README and DESIGN are English, and so
  are test comments, docstrings, test names and placeholder data.
- Download support depends on the installed versions and capabilities of
  `rclone` and `yt-dlp`.
- Cloud adapters and authenticated services require user configuration and may
  change independently of this project.
- ASR runs sequentially; there is no built-in GPU worker pool.
- Images are listed in manifests but are not OCRed.
- A few document fallbacks preserve text at the cost of layout fidelity and
  mark the result explicitly.
- Size estimates for remote media are conservative estimates, not guarantees.
- Concurrent runs that target the same output directory are unsupported.
- The post-corpus path preserves the wrapper format, but transcript paragraph
  boundaries may differ from other transcription tools.

The suite keeps those Cyrillic fixtures on purpose. They cover a windows-1251
saved web page, CP1251 subtitle decoding, Unicode normalization and combining
marks, Cyrillic directory and file names travelling through the pipeline and
through `ffmpeg`, filename truncation and hook field limits counted in bytes on
multi-byte text, the Russian promotional-clutter samples that the default
blacklist matches, the legacy Russian part marker in corpus file names, catalog
rows with Russian titles, and a captured Boosty text post with the output
expected from it. These strings are test data, not comments, documentation,
credentials, or operational configuration.

## Tests

Run the hermetic suite without bytecode or pytest caches:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --group dev python -m pytest -q -p no:cacheprovider
```

The suite uses temporary directories, local stubs, synthetic URLs, and local
HTTP fixtures. It does not require real course data or live credentials.

## License

MIT. See [LICENSE](LICENSE). External tools, libraries, models, and downloaded
content keep their own licenses and terms.
