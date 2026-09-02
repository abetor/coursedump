"""Filesystem primitives for atomic writes and self-ignoring state directories."""
from __future__ import annotations

import os
from pathlib import Path

# Make the directory self-ignoring independently of the repository root, so
# local runtime state cannot enter `git add .` even in another project.
GITIGNORE_BODY = "# Local tool state is not part of the repository\n*\n"


def atomic_write(path: str | Path, data: str | bytes, encoding: str = "utf-8") -> None:
    """Write without partial state using a sibling temporary file and os.replace.

    Readers never see a partial file. A crashed writer can leave at most a
    temporary fragment, not a damaged original.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data, encoding=encoding)
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


def ensure_state_dir(path: str | Path) -> Path:
    """Create a state directory with a self-ignoring .gitignore.

    Do not overwrite an existing file. Exclusive creation avoids clobbering
    during a race between processes.
    """
    d = Path(path)
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        try:
            with open(gi, "x", encoding="utf-8") as f:
                f.write(GITIGNORE_BODY)
        except FileExistsError:
            pass  # Another process created it between exists() and open().
    return d
