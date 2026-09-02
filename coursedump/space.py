"""Check disk space before download so exhaustion fails cleanly before writing."""

import shutil
from pathlib import Path

from .util import human_size

MIN_FREE = 5 * 1024**3  # Keep at least 5 GiB free.


class NoSpace(RuntimeError):
    pass


def ensure_space(where: Path, need_bytes: int) -> None:
    """Require max(2 * item size, MIN_FREE).

    A non-positive size means no estimate was possible, so only the minimum
    reserve can be enforced. Source adapters should provide an estimate through
    sources._est_size whenever possible.
    """
    free = shutil.disk_usage(where).free
    need_bytes = max(need_bytes, 0)
    if free - 2 * need_bytes < MIN_FREE:
        raise NoSpace(
            f"Not enough space on {where}: {human_size(free)} free; "
            f"need about {human_size(2 * need_bytes)} plus a {human_size(MIN_FREE)} reserve. "
            f"Free space, add --purge-video, or use --out on another disk.")
