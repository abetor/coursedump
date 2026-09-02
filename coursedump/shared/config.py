"""Load TOML fail closed: unknown fields are errors, never silent no-ops.

Silently accepting a misspelled field creates configuration that appears to
apply but has no effect. Rejecting unknown fields prevents that failure mode.
"""
from __future__ import annotations

import tomllib
from pathlib import Path


def load_toml(path: str | Path, *, known: set[str],
              required: frozenset[str] | set[str] = frozenset()) -> dict:
    """Load TOML, validate field names, and return the raw dictionary.

    Invalid syntax, unknown fields, and missing required fields raise
    ValueError. The caller owns defaults and type validation.
    """
    path = Path(path)
    raw = tomllib.loads(path.read_text("utf-8"))
    unknown = set(raw) - set(known)
    if unknown:
        raise ValueError(f"{path.name}: unknown fields {sorted(unknown)}")
    missing = set(required) - set(raw)
    if missing:
        raise ValueError(f"{path.name}: missing required fields {sorted(missing)}")
    return raw
