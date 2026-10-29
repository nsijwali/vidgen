"""Layered configuration.

Precedence, lowest to highest: built-in defaults < user config < project config < the spec itself.
CLI flags override all of them.

  user config     $YAMLREEL_CONFIG, else $XDG_CONFIG_HOME/yamlreel/config.yaml (~/.config/yamlreel/config.yaml)
  project config  --config PATH, else ./yamlreel.config.yaml

The pre-rename names (old env vars and config locations) still work until 0.3.0; see legacy.py.
One file is used per slot, never a merge of old and new.

Settings that widen what a spec may touch (`allow_paths`, `allow_private_hosts`, `chromium_path`) are only
honoured from the user config or the command line. A project config or spec that you downloaded cannot
grant itself access to your files or network.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import legacy

MAX_CONFIG_BYTES = 256 * 1024
CONFIG_FILENAME = "yamlreel.config.yaml"

# keys any config layer (and the spec) may set
SHARED_KEYS = {"theme", "themes", "formats", "watermark", "music", "meta", "poster", "fps", "output_dir", "limits", "voiceover"}
# keys only the user config / CLI may set (they widen access)
TRUSTED_KEYS = {"allow_paths", "allow_private_hosts", "chromium_path", "font_dir", "voice_server"}

DEFAULTS: dict[str, Any] = {
    "theme": "sumi",
    "formats": ["landscape"],
    "music": {"style": "koto", "bpm": 72, "root": "D", "scale": "hirajoshi", "seed": 7, "volume": 0.9, "snap": False},
    "meta": {"tags": [], "links": [], "credits": []},
    "fps": 30,
    "output_dir": "yamlreel-output",
    "limits": {"max_seconds": 600, "max_scenes": 200},
}
HARD_LIMITS = {"max_seconds": 3600, "max_scenes": 1000}
PATH_KEYS = {("watermark", "image"), ("output_dir",), ("font_dir",), ("chromium_path",)}


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    allow_paths: list[Path] = field(default_factory=list)
    allow_private_hosts: bool = False
    chromium_path: str | None = None
    font_dirs: list[str] = field(default_factory=list)
    voice_server: dict[str, Any] | None = None  # {url, client_id}; user config / CLI only
    sources: list[Path] = field(default_factory=list)


def _config_base() -> str:
    return os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")


def user_config_path() -> Path:
    """Where the user config is written (config init --user)."""
    explicit = legacy.env("CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    return Path(_config_base()) / "yamlreel" / "config.yaml"


def find_user_config() -> Path:
    """Where the user config is read from: the same path, or the pre-rename location if only that exists."""
    explicit = legacy.env("CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    base = Path(_config_base())
    return legacy.pick(base / "yamlreel" / "config.yaml", legacy.legacy_user_config(base))


def _read(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ConfigError(f"{path}: config file is larger than {MAX_CONFIG_BYTES} bytes")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: config must be a mapping")
    return raw


def _absolutize(d: dict[str, Any], base: Path) -> None:
    """Resolve path-valued keys relative to the file that set them."""
    for keys in PATH_KEYS:
        node: Any = d
        for k in keys[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, dict) and isinstance(node.get(keys[-1]), str):
            p = Path(node[keys[-1]]).expanduser()
            node[keys[-1]] = str(p if p.is_absolute() else (base / p))
    music = d.get("music")
    if isinstance(music, dict) and isinstance(music.get("style"), str) and Path(music["style"]).suffix:
        p = Path(music["style"]).expanduser()
        music["style"] = str(p if p.is_absolute() else (base / p))


def merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge mappings; replace everything else. meta.tags/links/credits concatenate."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k == "meta" and isinstance(v, dict) and isinstance(out.get("meta"), dict):
            meta = dict(out["meta"])
            for mk, mv in v.items():
                if mk in ("tags", "links", "credits") and isinstance(mv, list):
                    merged = list(meta.get(mk, []))
                    merged += [x for x in mv if x not in merged]
                    meta[mk] = merged
                else:
                    meta[mk] = copy.deepcopy(mv)
            out["meta"] = meta
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _check_keys(path: Path, raw: dict[str, Any], trusted: bool) -> None:
    allowed = SHARED_KEYS | (TRUSTED_KEYS if trusted else set())
    unknown = sorted(set(raw) - allowed)
    if unknown:
        hint = ""
        if set(unknown) & TRUSTED_KEYS:
            hint = " (allow_paths, allow_private_hosts, chromium_path, font_dir and voice_server are only read from the user config or the CLI)"
        raise ConfigError(f"{path}: unknown or not-permitted key(s): {', '.join(unknown)}{hint}")


def load_config(explicit: str | Path | None = None, cwd: Path | None = None,
                cli_allow_paths: list[str] | None = None, cli_allow_private_hosts: bool = False,
                cli_voice_url: str | None = None) -> Config:
    cfg = Config()
    user = find_user_config()
    layers: list[tuple[Path, bool]] = []
    if user.exists():
        layers.append((user, True))
    project = Path(explicit).expanduser() if explicit else legacy.pick((cwd or Path.cwd()) / CONFIG_FILENAME,
                                                                  (cwd or Path.cwd()) / legacy.LEGACY_PROJECT_FILENAME)
    if explicit and not project.exists():
        raise ConfigError(f"config file not found: {project}")
    if project.exists() and (not user.exists() or project.resolve() != user.resolve()):
        layers.append((project, False))
    for path, trusted in layers:
        raw = _read(path)
        _check_keys(path, raw, trusted)
        _absolutize(raw, path.resolve().parent)
        if trusted:
            for p in raw.pop("allow_paths", []) or []:
                cfg.allow_paths.append(Path(os.path.expanduser(str(p))))
            cfg.allow_private_hosts = bool(raw.pop("allow_private_hosts", cfg.allow_private_hosts))
            cfg.chromium_path = raw.pop("chromium_path", cfg.chromium_path)
            vs = raw.pop("voice_server", None)
            if vs is not None:
                if not isinstance(vs, dict) or set(vs) - {"url", "client_id", "timeout"}:
                    raise ConfigError(f"{path}: voice_server is {{url, client_id, timeout}}")
                cfg.voice_server = {**(cfg.voice_server or {}), **vs}
            fd = raw.pop("font_dir", None)
            if fd:
                cfg.font_dirs.append(str(fd))
        cfg.data = merge(cfg.data, raw)
        cfg.sources.append(path)
    cfg.allow_paths += [Path(os.path.expanduser(p)) for p in (cli_allow_paths or [])]
    cfg.allow_private_hosts = cfg.allow_private_hosts or cli_allow_private_hosts
    if cli_voice_url:
        cfg.voice_server = {**(cfg.voice_server or {}), "url": cli_voice_url}
    cfg.chromium_path = cfg.chromium_path or legacy.env("CHROMIUM")
    font_dir = legacy.env("FONT_DIR")
    if font_dir:
        cfg.font_dirs.append(font_dir)
    lim = cfg.data.get("limits", {})
    for k, hard in HARD_LIMITS.items():
        v = lim.get(k)
        if not isinstance(v, (int, float)) or v <= 0:
            raise ConfigError(f"limits.{k} must be a positive number")
        lim[k] = min(v, hard)
    return cfg
