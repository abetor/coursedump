"""Filename cleanup and configurable clutter filters from filters.toml.

The working filters.toml lives in the data directory and is seeded from the
package default on first run. Until configure is called, including direct
executor use in tests, the package default remains active.
"""

import re
import tomllib
from pathlib import Path

DEFAULT_FILTERS = Path(__file__).resolve().parent / "filters_default.toml"

_filters_path: Path = DEFAULT_FILTERS
_cache: dict | None = None


def configure(path: Path) -> None:
    """Select the working filters.toml, normally <data>/filters.toml."""
    global _filters_path, _cache
    _filters_path = Path(path)
    _cache = None


def _filters() -> dict:
    global _cache
    if _cache is None:
        path = _filters_path if _filters_path.exists() else DEFAULT_FILTERS
        raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        _cache = {
            "strip": [re.compile(p, re.IGNORECASE) for p in raw.get("strip_prefixes", [])],
            "blacklist": [re.compile(p, re.IGNORECASE) for p in raw.get("blacklist", [])],
            "junk": [re.compile(p, re.IGNORECASE) for p in raw.get("junk", [])],
        }
    return _cache


def clean_component(name: str) -> str:
    """Repeatedly remove known promotional prefixes from one path component."""
    prev = None
    while prev != name:
        prev = name
        for rx in _filters()["strip"]:
            name = rx.sub("", name, count=1)
    return name.strip() or prev.strip()


def clean_rel(rel: str) -> str:
    return "/".join(clean_component(c) for c in rel.split("/"))


def is_blacklisted(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(rx.search(name) for rx in _filters()["blacklist"])


def is_junk(name: str) -> bool:
    return any(rx.search(name) for rx in _filters()["junk"])
