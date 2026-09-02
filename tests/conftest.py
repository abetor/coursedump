"""Isolate tests from the user disk and credentials: HOME/cwd -> tmp, env -> clean.

A test that wanders into the real $HOME (configs, state directories) or litters
the current directory breaks the machine silently. Cut that off by construction,
not by discipline: the fixtures are autouse, so every test is hermetic whether
or not its author thought about it.
"""
import re
import sys
from pathlib import Path

import pytest

# Import the package without installing it: tests run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Credential stripping: environment variables declared in .env.sample (commented
# out examples included) are invisible to tests. The motivating incident: a real
# token in the process environment leaks into a "hermetic" test, which then goes
# green locally while calling a live API, and red on a clean machine.
# Deliberately conservative: ONLY names listed in .env.sample are removed, never
# a heuristic over all of os.environ - unsetting somebody else's variable (a
# PATH-shaped one) is worse than missing one.
_ENV_VAR_RE = re.compile(r"([A-Z][A-Z0-9_]*)=")


def _env_sample_vars(sample: Path | None = None) -> list[str]:
    """Variable names out of .env.sample; line format: [# ]NAME=   # why."""
    if sample is None:
        sample = Path(__file__).resolve().parents[1] / ".env.sample"
    if not sample.is_file():
        return []
    names = []
    for line in sample.read_text("utf-8").splitlines():
        m = _ENV_VAR_RE.match(line.lstrip("# ").strip())
        if m:
            names.append(m.group(1))
    return names


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    # The underscore is deliberate: tmp_path/"home" collided with tests that
    # create their own "home" directory in tmp_path (FileExistsError).
    home = tmp_path / "_isolated_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    for name in _env_sample_vars():
        monkeypatch.delenv(name, raising=False)
