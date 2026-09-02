# AGENTS.md - tool-coursedump

`coursedump` turns an authorized local or remote course source into a resumable
Markdown snapshot and machine-readable manifests. It performs faithful text
extraction, not summarization or semantic rewriting. The repository contains
code and synthetic fixtures only. Media, transcripts, manifests, queue state,
logs, ASR models, cookies, and credentials belong in an external data root.
Transport and media binaries are host dependencies and are not vendored here.

## Read in this order

1. `README.md` - public scope, setup, data boundary, CLI examples, and tests.
2. `docs/DESIGN.md` - source, durability, extraction, queue, corpus, and JSON
   contracts.
3. `tests/` - executable behavior and regression cases. Start with
   `tests/conftest.py` and `tests/_helpers.py` for fixture conventions.

## Core invariants

- Files on disk are the source of truth. A completed output file marks its
  stage complete; there is no database-backed completion flag.
- Result writes are atomic. Write a sibling temporary file and rename it only
  after the complete result is available.
- Re-running the same source resumes missing work instead of restarting it.
- Keep transport in established external tools. Do not add a custom downloader,
  service authentication implementation, or access-control bypass.
- Speech recognition stays local. Do not add a hosted ASR backend.
- Run and queue execution must remain non-interactive and safe to resume.
- Source profiles may tighten rate or media behavior by default. Only an
  explicit caller option may relax such a profile.

## Working rules

- Before claiming a change passes, run exactly:

```bash
uv run --group dev python -m pytest -q -p no:cacheprovider
```

- The suite is hermetic. Use temporary directories, local stubs, synthetic
  addresses, and local HTTP fixtures. Tests must not contact an external
  network, live service, real course source, or persistent user data.
- Never add credential values, browser data, signed or expiring delivery URLs,
  downloaded content, or real user metadata to fixtures, logs, issues, or
  commits. Browser authentication is delegated by browser name only.
- Mutable state and optional configuration stay under the selected external
  data root. Resolution order is the CLI data option, then COURSEDUMP_DATA,
  then the documented user-data default. Tests must pass a temporary root.
- Do not scan a large data root to determine status. Use the public plan and
  status commands described in `README.md`.
- Keep source, docstrings, CLI output, and documentation in English. Non-English
  text is allowed only when it is the fixture data required by a Unicode,
  encoding, normalization, filename, subtitle, or transcript test.
- Escape foreign titles, filenames, and addresses before inserting them into a
  glob, regular expression, output template, or rich-text renderer. Exercise
  the real receiving engine in the regression test.
- Add a regression test for every fixed incident.
- Keep changes narrow. New optional capabilities belong under
  `coursedump/ext/`; shared compatibility code under `coursedump/shared/`
  changes only with an explicit reason.
- Use readable typed Python consistent with nearby code. Prefer small pure
  helpers around filesystem and subprocess boundaries.
- Commits use an imperative English subject, one logical change, no generated
  data, no cache files, no credential files, and plain ASCII punctuation.

## Contract changes

Treat the CLI surface and exit codes, machine JSON schemas, snapshot layout,
corpus wrapper and deduplication key, queue file and state layout, data-root
precedence, and resume semantics as public contracts. A contract change must
update `README.md` and `docs/DESIGN.md` and add or adjust tests in the same
change. Version machine-readable output instead of silently reshaping it.
