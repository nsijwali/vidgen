"""Load and validate a video spec (YAML), layered over the configuration."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import SHARED_KEYS, Config, _absolutize, merge
from .theme import get_theme

FORMATS = {"landscape": (1920, 1080), "vertical": (1080, 1920), "square": (1080, 1080)}

DEFAULT_SECONDS = {"title": 3.0, "type": 3.0, "shot": 4.0, "diagram": 7.0,
                   "bullets": 5.0, "code": 6.0, "stat": 3.5}
REQUIRED = {
    "title": ["title"],
    "type": ["text"],
    "shot": ["source"],
    "diagram": ["nodes"],
    "bullets": ["items"],
    "code": ["code"],
    "stat": ["value"],
}
MUSIC_STYLES = {"koto", "none"}
WATERMARK_POSITIONS = {"top-left", "top-right", "bottom-left", "bottom-right"}

MAX_SPEC_BYTES = 1024 * 1024
MAX_TEXT = 2000          # characters in any one text field
MAX_CODE_LINES = 60
MAX_CODE_COLS = 120
MAX_ITEMS = 20
MAX_NODES = 40
MAX_EDGES = 80
MAX_FLASH = 40
MAX_CLICKS = 10


class SpecError(ValueError):
    pass


def load_spec(path: str | Path, config: Config | None = None) -> dict[str, Any]:
    p = Path(path)
    cfg = config or Config()
    try:
        if p.stat().st_size > MAX_SPEC_BYTES:
            raise SpecError(f"spec {p} is larger than {MAX_SPEC_BYTES} bytes")
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise SpecError(f"cannot read spec {p}: {e}") from e
    except yaml.YAMLError as e:
        raise SpecError(f"cannot parse spec {p}: {e}") from e
    spec = validate(raw, cfg, base_dir=p.resolve().parent)
    spec["_dir"] = str(p.resolve().parent)
    return spec


def _num(i: int | str, name: str, v: Any, lo: float, hi: float) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise SpecError(f"scene {i}: {name} must be a number between {lo} and {hi}")
    return float(v)


def _text(where: str, v: Any) -> str:
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        raise SpecError(f"{where} must be text")
    s = str(v)
    if len(s) > MAX_TEXT:
        raise SpecError(f"{where} is {len(s)} characters; the limit is {MAX_TEXT}")
    return s


def validate(raw: Any, config: Config | None = None, base_dir: Path | None = None) -> dict[str, Any]:
    cfg = config or Config()
    if not isinstance(raw, dict):
        raise SpecError("spec must be a mapping")
    spec_forbidden = {"allow_paths", "allow_private_hosts", "chromium_path", "font_dir"} & set(raw)
    if spec_forbidden:
        raise SpecError(f"{', '.join(sorted(spec_forbidden))}: not settable from a spec (user config or CLI only)")
    raw = dict(raw)
    unknown_top = sorted(set(raw) - SHARED_KEYS - {"scenes"})
    if unknown_top:
        raise SpecError(f"unknown top-level key(s): {', '.join(unknown_top)}")
    scenes_raw = raw.pop("scenes", None)
    if base_dir is not None:
        _absolutize(raw, base_dir)  # paths in a spec are relative to the spec
    spec: dict[str, Any] = merge(cfg.data, raw)
    spec["scenes"] = scenes_raw

    limits = spec["limits"]
    try:
        spec["_theme_obj"] = get_theme(spec["theme"], spec.get("themes") or {})
    except ValueError as e:
        raise SpecError(str(e)) from e
    fmts = spec["formats"]
    if not isinstance(fmts, list) or not fmts:
        raise SpecError("formats must be a non-empty list")
    bad = [f for f in fmts if f not in FORMATS]
    if bad:
        raise SpecError(f"unknown format(s) {bad}; choose from {', '.join(FORMATS)}")

    music = spec["music"]
    if isinstance(music, str):
        music = {"style": music}
    if not isinstance(music, dict):
        raise SpecError("music must be a mapping or a string")
    if music.get("style") not in MUSIC_STYLES and not Path(str(music.get("style"))).suffix:
        raise SpecError(f"music.style must be one of {sorted(MUSIC_STYLES)} or a path to an audio file")
    for key, lo, hi in (("bpm", 30, 240), ("volume", 0, 2), ("seed", 0, 2**31)):
        v = music.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise SpecError(f"music.{key} must be a number between {lo} and {hi}")
    spec["music"] = music

    spec["watermark"] = _check_watermark(spec.get("watermark"))

    meta = spec["meta"]
    if not isinstance(meta, dict):
        raise SpecError("meta must be a mapping")
    for k in ("title", "description"):
        if k in meta:
            meta[k] = _text(f"meta.{k}", meta[k])
    for k in ("tags", "links", "credits"):
        v = meta.get(k, [])
        if not isinstance(v, list) or len(v) > 50:
            raise SpecError(f"meta.{k} must be a list of at most 50 entries")
        meta[k] = [_text(f"meta.{k}[{n}]", x) for n, x in enumerate(v)]

    poster = spec.get("poster")
    if poster is not None:
        if not isinstance(poster, dict) or set(poster) - {"scene", "at"}:
            raise SpecError("poster is {scene: index, at: seconds}")
        if "scene" in poster and (isinstance(poster["scene"], bool) or not isinstance(poster["scene"], int) or poster["scene"] < 0):
            raise SpecError("poster.scene must be a non-negative integer")
        if "at" in poster:
            _num("poster", "at", poster["at"], 0, limits["max_seconds"])

    if not isinstance(scenes_raw, list) or not scenes_raw:
        raise SpecError("spec needs a non-empty 'scenes' list")
    if len(scenes_raw) > limits["max_scenes"]:
        raise SpecError(f"{len(scenes_raw)} scenes; the limit is {limits['max_scenes']} (limits.max_scenes)")
    out = []
    total = 0.0
    for i, sc in enumerate(scenes_raw):
        if not isinstance(sc, dict) or "type" not in sc:
            raise SpecError(f"scene {i}: needs a 'type'")
        t = sc["type"]
        if t not in REQUIRED:
            raise SpecError(f"scene {i}: unknown type {t!r}; choose from {', '.join(REQUIRED)}")
        missing = [k for k in REQUIRED[t] if k not in sc]
        if missing:
            raise SpecError(f"scene {i} ({t}): missing {', '.join(missing)}")
        sc = dict(sc)
        sc.setdefault("seconds", DEFAULT_SECONDS[t])
        sc["seconds"] = _num(i, "seconds", sc["seconds"], 0.1, limits["max_seconds"])
        total += sc["seconds"]
        for k in ("title", "text", "eyebrow", "subtitle", "url", "caption", "value", "label"):
            if k in sc:
                sc[k] = _text(f"scene {i}: {k}", sc[k])
        _check_scene(i, t, sc)
        out.append(sc)
    if total > limits["max_seconds"]:
        raise SpecError(f"total length {total:.0f}s; the limit is {limits['max_seconds']}s (limits.max_seconds)")
    spec["scenes"] = out
    return spec


def _check_scene(i: int, t: str, sc: dict[str, Any]) -> None:
    if t == "shot":
        if not isinstance(sc["source"], str) or not sc["source"].strip():
            raise SpecError(f"scene {i} (shot): source must be a URL or a file path")
        z = sc.get("zoom", [1.0, 1.0])
        z = z if isinstance(z, list) else [1.0, z]
        if len(z) != 2:
            raise SpecError(f"scene {i} (shot): zoom is a number or [from, to]")
        sc["zoom"] = [_num(i, "zoom", v, 1.0, 8.0) for v in z]
        for k in ("focus", "focus_to"):
            if k in sc:
                f = sc[k]
                if not isinstance(f, list) or len(f) != 2:
                    raise SpecError(f"scene {i} (shot): {k} is [x, y] between 0 and 1")
                sc[k] = [_num(i, k, v, 0.0, 1.0) for v in f]
        if "viewport" in sc:
            v = sc["viewport"]
            if not isinstance(v, list) or len(v) != 2:
                raise SpecError(f"scene {i} (shot): viewport is [width, height]")
            sc["viewport"] = [int(_num(i, "viewport", x, 320, 3840)) for x in v]
        if "scale" in sc:
            sc["scale"] = _num(i, "scale", sc["scale"], 1, 3)
        if "wait_ms" in sc:
            sc["wait_ms"] = int(_num(i, "wait_ms", sc["wait_ms"], 0, 30000))
        if "scroll_y" in sc:
            sc["scroll_y"] = int(_num(i, "scroll_y", sc["scroll_y"], 0, 100000))
        clicks = sc.get("click", [])
        if not isinstance(clicks, list) or len(clicks) > MAX_CLICKS or not all(isinstance(c, str) and len(c) < 300 for c in clicks):
            raise SpecError(f"scene {i} (shot): click is a list of up to {MAX_CLICKS} CSS selectors")
        sc["click"] = clicks
        if sc.get("fit", "card") not in ("card", "cover"):
            raise SpecError(f"scene {i} (shot): fit must be card or cover")
    elif t == "bullets":
        items = sc["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
            raise SpecError(f"scene {i} (bullets): items is a list of 1 to {MAX_ITEMS}")
        sc["items"] = [_text(f"scene {i}: items[{n}]", x) for n, x in enumerate(items)]
    elif t == "code":
        code = _text(f"scene {i}: code", sc["code"]) if len(str(sc["code"])) <= MAX_TEXT * 4 else None
        if code is None:
            raise SpecError(f"scene {i} (code): too long")
        lines = code.rstrip("\n").split("\n")
        if len(lines) > MAX_CODE_LINES or max(len(l) for l in lines) > MAX_CODE_COLS:
            raise SpecError(f"scene {i} (code): at most {MAX_CODE_LINES} lines of {MAX_CODE_COLS} columns")
        sc["code"] = code
        hl = sc.get("highlight", [])
        if not isinstance(hl, list) or not all(isinstance(n, int) and 1 <= n <= len(lines) for n in hl):
            raise SpecError(f"scene {i} (code): highlight is a list of line numbers (1..{len(lines)})")
    elif t == "diagram":
        _check_diagram(i, sc)


def _check_watermark(raw: Any) -> dict[str, Any] | None:
    """Accepts a string (the text) or {text, image, position, opacity, size}; None disables it."""
    if raw in (None, False):
        return None
    wm = {"text": raw} if isinstance(raw, str) else dict(raw) if isinstance(raw, dict) else None
    if wm is None:
        raise SpecError("watermark must be a string or a mapping")
    unknown = set(wm) - {"text", "image", "position", "opacity", "size"}
    if unknown:
        raise SpecError(f"watermark: unknown key(s) {', '.join(sorted(unknown))}")
    if not wm.get("text") and not wm.get("image"):
        raise SpecError("watermark needs text or image")
    if "text" in wm:
        wm["text"] = _text("watermark.text", wm["text"])
    pos = wm.get("position")
    if pos is not None and pos not in WATERMARK_POSITIONS:
        raise SpecError(f"watermark.position must be one of {sorted(WATERMARK_POSITIONS)}")
    op = wm.get("opacity", 0.75)
    if isinstance(op, bool) or not isinstance(op, (int, float)) or not 0 < op <= 1:
        raise SpecError("watermark.opacity must be between 0 and 1")
    wm["opacity"] = float(op)
    size = wm.get("size", 30)
    if isinstance(size, bool) or not isinstance(size, (int, float)) or not 8 <= size <= 200:
        raise SpecError("watermark.size must be between 8 and 200")
    wm["size"] = float(size)
    return wm


def _check_diagram(i: int, sc: dict[str, Any]) -> None:
    nodes, edges, flash = sc["nodes"], sc.get("edges", []), sc.get("flash", [])
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
        raise SpecError(f"scene {i} (diagram): nodes is a list of 1 to {MAX_NODES}")
    if not isinstance(edges, list) or len(edges) > MAX_EDGES or not isinstance(flash, list) or len(flash) > MAX_FLASH:
        raise SpecError(f"scene {i} (diagram): at most {MAX_EDGES} edges and {MAX_FLASH} flashes")
    ids = []
    for n in nodes:
        if not isinstance(n, dict) or "id" not in n or "label" not in n:
            raise SpecError(f"scene {i} (diagram): every node needs id and label")
        if "x" not in n or "y" not in n:
            raise SpecError(f"scene {i} (diagram): node {n['id']!r} needs x and y (0..1)")
        n["label"] = _text(f"scene {i}: node label", n["label"])
        for k in ("x", "y", "vx", "vy"):
            if k in n:
                n[k] = _num(i, f"node {k}", n[k], 0.0, 1.0)
        if "at" in n:
            n["at"] = _num(i, "node at", n["at"], 0, 600)
        if n.get("kind", "service") not in ("client", "service", "cache", "db"):
            raise SpecError(f"scene {i} (diagram): node kind must be client, service, cache or db")
        ids.append(n["id"])
    if len(set(ids)) != len(ids):
        raise SpecError(f"scene {i} (diagram): duplicate node ids")
    for e in edges:
        if not isinstance(e, dict) or e.get("from") not in ids or e.get("to") not in ids:
            raise SpecError(f"scene {i} (diagram): edge {e} references an unknown node")
        if "label" in e:
            e["label"] = _text(f"scene {i}: edge label", e["label"])
        if "at" in e:
            e["at"] = _num(i, "edge at", e["at"], 0, 600)
        if e.get("color", "accent2") not in ("accent", "accent2", "fg", "muted"):
            raise SpecError(f"scene {i} (diagram): edge color must be accent, accent2, fg or muted")
    for f in flash:
        if not isinstance(f, dict) or f.get("node") not in ids:
            raise SpecError(f"scene {i} (diagram): flash references unknown node {f.get('node') if isinstance(f, dict) else f!r}")
        f["at"] = _num(i, "flash at", f.get("at", 0), 0, 600)
        if "text" in f:
            f["text"] = _text(f"scene {i}: flash text", f["text"])
        if f.get("color", "accent") not in ("accent", "accent2", "fg", "muted"):
            raise SpecError(f"scene {i} (diagram): flash color must be accent, accent2, fg or muted")
