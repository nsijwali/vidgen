"""Compatibility with the pre-0.2 name of this tool. Delete this module in yamlreel 0.3.0.

This is the only code that may mention the old name. Old env vars, config paths and the old console script keep
working, each with a one-line warning on stderr (once per process).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

LEGACY = "vidgen"
REMOVE_IN = "0.3.0"
LEGACY_PROJECT_FILENAME = "vidgen.config.yaml"

_warned: set[str] = set()  # warn-once registry (tests clear it)


def warn_once(key: str, msg: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    print(f"warning: {msg}", file=sys.stderr)


def _deprecated(old: str, new: str) -> str:
    return f"{old} is deprecated, use {new} (support ends in yamlreel {REMOVE_IN})"


def env(suffix: str) -> str | None:
    """YAMLREEL_<suffix> if set and non-empty, else the legacy VIDGEN_<suffix> (with a warning), else None."""
    new_name, old_name = f"YAMLREEL_{suffix}", f"{LEGACY.upper()}_{suffix}"
    value = os.environ.get(new_name)
    if value:
        return value
    value = os.environ.get(old_name)
    if value:
        warn_once(f"env:{old_name}", _deprecated(old_name, new_name))
        return value
    return None


def legacy_user_config(base: Path) -> Path:
    return base / LEGACY / "config.yaml"


def pick(new: Path, old: Path) -> Path:
    """One file per config slot, never merged: `new` if it exists, else `old` if it exists (warn), else `new`."""
    if new.exists():
        if old.exists() and not _same_file(new, old):
            warn_once(f"path:{old}", f"ignoring legacy config {old} because {new} exists; "
                                     f"remove or move it (support for the old location ends in yamlreel {REMOVE_IN})")
        return new
    if old.exists():
        warn_once(f"path:{old}", _deprecated(str(old), str(new)))
        return old
    return new


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    """Entry point of the legacy `vidgen` console script."""
    from . import cli
    warn_once("script", f"the `{LEGACY}` command is now `yamlreel` (support ends in yamlreel {REMOVE_IN})")
    return cli.main(argv)
