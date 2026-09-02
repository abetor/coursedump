# Design

## Scope

`coursedump` turns an authorized source into a durable Markdown snapshot. Its
boundary ends at faithful text extraction and provenance. Summarization,
semantic cleanup, indexing, and retrieval belong to downstream systems.

The implementation has three primary modes:

- `run` reconciles one source or resumes incomplete snapshots;
- `queue` processes a file-backed list sequentially;
- `corpus` exports supported post feeds into a flat text corpus with stable
  source identifiers.

## Data boundary

All mutable data stays outside the repository. The root is selected by
`--data`, then `COURSEDUMP_DATA`, then the documented user-data default. An
explicit `--out` overrides only snapshot output. The current working directory
is never treated as implicit application state.

The data root can contain:

```text
in/                     optional local inputs
out/<course>/            course snapshots
staging/                 post-corpus working snapshots
queue.txt                queue input
queue.lock               process lock
logs/                    queue log and observable state
config.toml              optional event-hook configuration
filters.toml             editable normalization filters
catalog.tsv              reconstructed course catalog
```

None of these paths belongs in a source release.

## Source model

Source detection selects one adapter:

- local directory;
- rclone remote;
- web source handled by `yt-dlp`;
- direct HTTP document;
- ZIP archive, which is validated before extraction and then treated as a local
  directory.

Adapters produce a manifest of `Item` records. Item identity and target paths
must remain stable across retries. A remote playlist item uses its stable media
identifier, not an expiring delivery URL, as identity.

Remote tooling is delegated to subprocesses. The project does not implement
service authentication, access-control bypasses, or custom media downloaders.
Callers are responsible for authorization and source-service compliance.

## Reconciliation and durability

The output filesystem is the source of truth. For every manifest item the
executor:

1. skips an already complete output;
2. reuses a present raw file;
3. checks the required free space;
4. fetches a missing raw file;
5. extracts and normalizes text;
6. atomically replaces the target Markdown file;
7. records an isolated item error without aborting unrelated items.

Writes use a sibling temporary file followed by rename. Re-running the same
source resumes work. `source.json` retains the source description needed for a
later resume, while `manifest.jsonl` describes the intended item set.

A complete snapshot may still contain item-level errors. Human callers must
inspect the summary or `errors.jsonl`; machine callers receive explicit counts
in the JSON result.

## Extraction

Media follows a subtitles-first policy. Existing or downloaded subtitles are
converted directly; otherwise a local ASR backend transcribes the media.
Supported backends are mlx-whisper, faster-whisper, and a deterministic dummy
backend for tests.

Both real ASR backends use explicit anti-loop decoding settings. A quality gate
examines raw segments before cosmetic repeat collapsing. Suspicious output is
retried with bounded alternative settings. If every attempt is suspicious, the
least damaged result is retained with a visible quality warning rather than
being presented as clean text.

Document extraction routes PDF files through a layered fallback, Office and
HTML documents through MarkItDown, saved MHTML pages through MIME root-part
selection, and text/subtitle files through encoding-aware readers.

All extractor output passes through one Unicode cleanup function. It removes
control and formatting characters except newline and tab, normalizes line
separators, and preserves ordinary Unicode spaces.

## Queue contract

Queue execution is sequential and non-interactive. A PID lock prevents a second
runner. Stale locks are recoverable. State is written atomically and is
observability data, not the authority for course completion.

Transient exit code 111 is retried with bounded waits. Resource exit code 75 is
recorded as a failure without retrying. A repeated identical failure with an
unchanged manifest and output snapshot remains visible but does not emit a
duplicate alert or a new failing queue exit.

An optional event hook runs through `/bin/sh -c` with a 30-second timeout and a
bounded environment payload. Hook failures are logged and never change the
queue result. Unknown configuration fields fail closed before execution.

## Corpus contract

The corpus command reads a queue of supported post URLs and caller-supplied
metadata. Deduplication uses a stable post identifier found in existing corpus
headers before any remote resolution. Invalid queue entries reject the whole
queue before network access.

Publishing is transactional at the post level. Multi-part posts become
multiple files, but the completion-proof part is written last. A partial post
therefore remains incomplete and can be resumed without falsely becoming DONE.
Filename collisions are disambiguated instead of overwriting existing content.

The wrapper format is stable: source header, optional metadata header, blank
line, transcript body, and exactly one trailing newline. Transcript paragraph
boundaries are produced by this project's ASR normalization and are not claimed
to be byte-identical to other transcription programs.

## Foreign strings and mini-languages

Titles, paths, URLs, and exception text are data. Before inserting foreign text
into another language, the receiving language's escaping rules apply:

- double `%` in `yt-dlp` output templates;
- use `glob.escape` for filesystem patterns;
- use `rich.markup.escape` for Rich output;
- use `re.escape` when interpolating into regular expressions.

Tests exercise the real template, glob, and Rich engines because hand-written
imitations do not enforce these contracts.

## Machine JSON v1

`capabilities --json`, `plan --json`, `run --json`, `status --json`, and queue
status expose versioned objects. JSON mode writes exactly one object to stdout;
diagnostics go to stderr. Detail strings are bounded and normalized so raw
subprocess output and source secrets are not echoed through the machine API.

Stable machine exit codes are:

- 0: success;
- 1: permanent or invalid-input failure;
- 75: local resource shortage;
- 111: transient interruption;
- 130: interrupted human-mode command.

## Security and privacy

- Cookie and token values are never written by the application.
- Browser authentication is delegated by name through the downloader's
  `--cookies-from-browser` option.
- Signed delivery URLs are neither stable identifiers nor acceptable fixtures.
- ZIP members are rejected before extraction if they are absolute, escape the
  destination, use ambiguous separators, or represent symlinks.
- Error and JSON detail fields are bounded to reduce accidental disclosure.
- Tests use `example.test`, local servers, and temporary directories.

## Deliberate constraints

- No database or web UI.
- No parallel local ASR workers.
- No API-based speech recognition.
- No custom remote downloader or authentication implementation.
- No OCR or frame-description pipeline in the current version.
- No claim that a remote size estimate is an exact upper bound.
