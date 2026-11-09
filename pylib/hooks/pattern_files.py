"""Load a hook pattern catalog (bash-patterns.toml and friends).

The catalogs are TOML, but ``tomllib`` only arrived in Python 3.11 and macOS
still ships 3.9 as ``/usr/bin/python3``. So the plugin also carries a JSON
copy of each catalog (written by ``scripts/sync_local.py``), and this loader
falls back to it when no TOML parser is available (#1089).
"""

from __future__ import annotations

import json
import os

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

__all__ = ["PatternFileError", "json_sibling", "load_pattern_file"]


class PatternFileError(ValueError):
    """The catalog exists but can't be parsed."""


def json_sibling(path: str) -> str:
    return os.path.splitext(path)[0] + ".json"


def load_pattern_file(path: str) -> dict:
    """Parse the catalog at ``path``.

    Raises FileNotFoundError when neither the TOML nor (without a TOML
    parser) its JSON copy exists, and PatternFileError when it won't parse.
    """
    if tomllib is not None:
        with open(path, "rb") as f:
            try:
                return tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise PatternFileError(f"invalid TOML in {path}: {e}") from e
    with open(json_sibling(path), encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise PatternFileError(f"invalid JSON in {json_sibling(path)}: {e}") from e
    if not isinstance(data, dict):
        raise PatternFileError(f"{json_sibling(path)} is not a table")
    return data
