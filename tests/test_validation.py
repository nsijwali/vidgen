"""Strict validation and absolute limits (Batch 1, PR B)."""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from yamlreel import voice
from yamlreel.scenes import CAPTURE_OPTS, SCENE_TYPES, DiagramScene, Scene
from yamlreel.specs import REQUIRED


def _literal_keys(func, names: set[str]) -> set[str]:
    """Literal keys read from the variables `names` in `func`: x.get("k") and x["k"]."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in names
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
            found.add(node.slice.value)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
              and isinstance(node.func.value, ast.Name) and node.func.value.id in names
              and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            found.add(node.args[0].value)
    return found


def test_field_tables_cover_every_key_the_scenes_read():
    """Drift guard: a key a scene constructor reads must be in its FIELDS table, or the strict validator would
    reject (or silently drop) a field that works."""
    assert _literal_keys(Scene.__init__, {"spec"}) <= Scene.COMMON_FIELDS
    for name, cls in SCENE_TYPES.items():
        read = _literal_keys(cls.__init__, {"spec", "s"})
        if name == "shot":
            read |= set(CAPTURE_OPTS)
        assert read <= cls.FIELDS | Scene.COMMON_FIELDS, f"{name}: {sorted(read - cls.FIELDS - Scene.COMMON_FIELDS)}"
        assert REQUIRED[name] and set(REQUIRED[name]) <= cls.FIELDS, name
    assert _literal_keys(DiagramScene.__init__, {"n"}) <= DiagramScene.NODE_FIELDS
    assert _literal_keys(DiagramScene.__init__, {"e"}) <= DiagramScene.EDGE_FIELDS
    assert _literal_keys(DiagramScene.__init__, {"f"}) <= DiagramScene.FLASH_FIELDS


def test_voice_screen_text_reads_only_known_fields():
    read = _literal_keys(voice.screen_text, {"sc"})
    known = set(Scene.COMMON_FIELDS).union(*(c.FIELDS for c in SCENE_TYPES.values()))
    assert read <= known


def test_drift_guard_catches_a_field_missing_from_a_table():
    """The guard must be able to fail: pretend TitleScene forgot `subtitle`."""
    read = _literal_keys(SCENE_TYPES["title"].__init__, {"spec", "s"})
    assert "subtitle" in read and "subtitle" in SCENE_TYPES["title"].FIELDS
    assert not read <= (SCENE_TYPES["title"].FIELDS - {"subtitle"}) | Scene.COMMON_FIELDS


# --- shared helpers -------------------------------------------------------------------------------------

import re
import sys
from pathlib import Path

import numpy as np
import yaml

from yamlreel import legacy, render as render_mod, shots, specs
from yamlreel.cli import main
from yamlreel.config import HARD_LIMITS, Config, ConfigError, clamp_limits, load_config
from yamlreel.render import RenderError, Runtime, Timeline, render
from yamlreel.shots import CaptureError
from yamlreel.specs import (FIELD_CHECKS, MAX_FRAMES, SpecError, TOP_CHECKS, check_frame_budget, load_spec,
                            validate)

ROOT = Path(__file__).parent.parent
TITLE = {"type": "title", "title": "x"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Never read the developer's own config; run from a scratch folder."""
    for prefix in ("YAMLREEL", legacy.LEGACY.upper()):
        for name in ("CONFIG", "FONT_DIR", "CHROMIUM"):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("YAMLREEL_CONFIG", str(tmp_path / "no-config.yaml"))
    monkeypatch.chdir(tmp_path)
    legacy._warned.clear()


def spec_file(tmp_path: Path, body: str, name: str = "s.yaml") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def cli_error(capsys, *argv) -> list[str]:
    """Run the CLI, expect exit 1, and return the stderr lines."""
    assert main(list(argv)) == 1
    return capsys.readouterr().err.splitlines()


# --- 1. the reproductions -------------------------------------------------------------------------------

REPRODUCTIONS = [
    ({"limits": None}, "limits must be a mapping"),
    ({"formats": [{}]}, "formats[0] must be one of landscape, vertical, square"),
    ({"fps": "oops"}, "fps must be an integer between 1 and 60"),
    ({"scenes": [{**TITLE, "titel": "y"}]}, "scenes[0].titel: unknown field for a title scene; allowed: caption, "),
]


@pytest.mark.parametrize("extra,expected", REPRODUCTIONS)
def test_reproductions_raise_spec_error_with_the_path(extra, expected):
    with pytest.raises(SpecError) as exc:
        validate({"scenes": [TITLE], **extra})
    assert str(exc.value).startswith(expected)


@pytest.mark.parametrize("extra,expected", REPRODUCTIONS)
def test_cli_validate_prints_one_error_line_and_exits_1(tmp_path, capsys, extra, expected):
    f = spec_file(tmp_path, yaml.safe_dump({"scenes": [TITLE], **extra}))
    lines = cli_error(capsys, "validate", str(f))
    assert len(lines) == 1 and lines[0].startswith("error: " + expected)


def test_other_reported_crashes_are_spec_errors():
    for bad, path in (({"type": "code", "code": "a", "highlight": [True]}, "scenes[0].highlight[0]"),
                      ({"type": "diagram", "nodes": [{"id": [1], "label": "a", "x": 0, "y": 0}]}, "scenes[0].nodes[0].id")):
        with pytest.raises(SpecError) as exc:
            validate({"scenes": [bad]})
        assert str(exc.value).startswith(path)


def test_limits_null_in_a_project_config_is_a_config_error(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("limits: null\n")
    with pytest.raises(ConfigError, match="limits must be a mapping"):
        load_config(explicit=cfg)


def test_yaml_errors_are_one_line(tmp_path, capsys):
    f = spec_file(tmp_path, "scenes:\n  - {type: title, title: [oops\n  - x: y\n")
    lines = cli_error(capsys, "validate", str(f))
    assert len(lines) == 1 and re.search(r"cannot parse spec .*s\.yaml: .* at line \d+, column \d+$", lines[0])


def test_unknown_keys_cannot_break_the_one_line_contract():
    with pytest.raises(SpecError) as exc:
        validate({"scenes": [TITLE], "bad\nkey": 1})
    assert "\n" not in str(exc.value)
    with pytest.raises(SpecError):
        validate({"scenes": [TITLE], 1: "int key", "a": 1})  # mixed key types must not break sorting
    with pytest.raises(SpecError) as exc:
        validate({"scenes": [{**TITLE, 7: "x"}]})
    assert str(exc.value).startswith("scenes[0].7: unknown field")


def test_a_music_string_is_shorthand_for_the_style():
    assert validate({"scenes": [TITLE], "music": "none"})["music"]["style"] == "none"


# --- 2. type-confusion matrix ---------------------------------------------------------------------------

VALUES = [None, True, "xyz", 0, -1, 1e12, [], {}, [{}], [[]], float("nan"), float("inf"), 10**400]
NODES = [{"id": "a", "label": "A", "x": 0.2, "y": 0.5}, {"id": "b", "label": "B", "x": 0.8, "y": 0.5}]
BASE_SCENES = {
    "title": {"type": "title", "title": "x"},
    "type": {"type": "type", "text": "x"},
    "shot": {"type": "shot", "source": "a.png"},
    "bullets": {"type": "bullets", "items": ["a"]},
    "stat": {"type": "stat", "value": "1"},
    "code": {"type": "code", "code": "x = 1\ny = 2"},
    "diagram": {"type": "diagram", "nodes": NODES, "edges": [{"from": "a", "to": "b"}],
                "flash": [{"node": "a", "at": 1}]},
}


def outcome(spec):
    """Validation must pass or raise SpecError with a one-line message, whatever the values."""
    try:
        validate(spec)
    except SpecError as e:
        assert "\n" not in str(e), str(e)


@pytest.mark.parametrize("value", VALUES, ids=repr)
def test_type_confusion_top_level_keys(value):
    for key in (*specs.SHARED_KEYS, "scenes"):
        outcome({"scenes": [TITLE], key: value})
    for key in ("title", "description", "tags", "links", "credits"):
        outcome({"scenes": [TITLE], "meta": {key: value}})
    for key in specs.MUSIC_KEYS:
        outcome({"scenes": [TITLE], "music": {key: value}})
    for key in ("scene", "at"):
        outcome({"scenes": [TITLE], "poster": {key: value}})
    for key in HARD_LIMITS:
        outcome({"scenes": [TITLE], "limits": {key: value}})
    for key in ("voice", "language", "gap", "music_level", "read_screen"):
        outcome({"scenes": [TITLE], "voiceover": {key: value}})
    for key in ("text", "image", "position", "opacity", "size"):
        outcome({"scenes": [TITLE], "watermark": {"text": "x", key: value}})
    for key in ("bg", "extends", "display", "frame"):
        outcome({"scenes": [TITLE], "themes": {"t": {key: value}}})
    outcome({"scenes": [TITLE], "themes": {"t": value}})
    outcome({"scenes": [TITLE], "themes": {value if isinstance(value, (str, int)) else "t": {}}})
    outcome({"scenes": [TITLE, value]})
    outcome({"scenes": [TITLE], "formats": [value]})
    outcome({"scenes": [TITLE], "meta": {"tags": [value]}})


@pytest.mark.parametrize("value", VALUES, ids=repr)
def test_type_confusion_every_scene_field(value):
    for t, cls in SCENE_TYPES.items():
        for field in sorted(cls.FIELDS | Scene.COMMON_FIELDS):
            outcome({"scenes": [{**BASE_SCENES[t], field: value}]})
    d = BASE_SCENES["diagram"]
    for field in sorted(DiagramScene.NODE_FIELDS):
        outcome({"scenes": [{**d, "nodes": [{**NODES[0], field: value}, NODES[1]]}]})
    for field in sorted(DiagramScene.EDGE_FIELDS):
        outcome({"scenes": [{**d, "edges": [{"from": "a", "to": "b", field: value}]}]})
    for field in sorted(DiagramScene.FLASH_FIELDS):
        outcome({"scenes": [{**d, "flash": [{"node": "a", field: value}]}]})
    outcome({"scenes": [{**d, "nodes": [value]}]})
    outcome({"scenes": [{**d, "edges": [value]}]})
    outcome({"scenes": [{**d, "flash": [value]}]})


def test_every_table_field_has_a_checker_that_rejects_a_mapping():
    """`{}` is valid for no scene, node, edge or flash field, so a field without a real check would let it pass."""
    for t, cls in SCENE_TYPES.items():
        for field in cls.FIELDS | Scene.COMMON_FIELDS:
            assert field in FIELD_CHECKS, field
            with pytest.raises(SpecError):
                validate({"scenes": [{**BASE_SCENES[t], field: {}}]})
    d = BASE_SCENES["diagram"]
    for field in DiagramScene.NODE_FIELDS:
        with pytest.raises(SpecError):
            validate({"scenes": [{**d, "nodes": [{**NODES[0], field: {}}, NODES[1]]}]})
    for field in DiagramScene.EDGE_FIELDS:
        with pytest.raises(SpecError):
            validate({"scenes": [{**d, "edges": [{"from": "a", "to": "b", field: {}}]}]})
    for field in DiagramScene.FLASH_FIELDS:
        with pytest.raises(SpecError):
            validate({"scenes": [{**d, "flash": [{"node": "a", field: {}}]}]})
    assert set(TOP_CHECKS) == specs.SHARED_KEYS | {"scenes"}


def test_integer_node_ids_are_normalised_and_still_resolve():
    spec = validate({"scenes": [{"type": "diagram", "nodes": [{"id": 1, "label": "a", "x": 0, "y": 0},
                                                               {"id": 2, "label": "b", "x": 1, "y": 1}],
                                 "edges": [{"from": 1, "to": 2}], "flash": [{"node": 2}]}]})
    sc = spec["scenes"][0]
    assert [n["id"] for n in sc["nodes"]] == ["1", "2"]
    assert sc["edges"][0]["from"] == "1" and sc["flash"][0]["node"] == "2"
    with pytest.raises(SpecError, match="duplicate"):
        validate({"scenes": [{"type": "diagram", "nodes": [{"id": 1, "label": "a", "x": 0, "y": 0},
                                                            {"id": "1", "label": "b", "x": 1, "y": 1}]}]})


def test_validation_does_not_mutate_the_input():
    raw = {"scenes": [{"type": "diagram", "nodes": [{"id": 1, "label": 5, "x": 0, "y": 0}]}]}
    before = yaml.safe_dump(raw)
    validate(raw)
    assert yaml.safe_dump(raw) == before


# --- 4. shipped examples and templates ------------------------------------------------------------------

def test_examples_and_templates_validate():
    files = [*sorted((ROOT / "examples").glob("*.yaml")),
             *(p for p in sorted((ROOT / "yamlreel" / "templates").glob("*.yaml")) if not p.name.startswith("config"))]
    assert len(files) >= 4
    for p in files:
        load_spec(p)


def test_the_example_config_loads_and_a_spec_validates_over_it():
    cfg = load_config(explicit=ROOT / "yamlreel" / "templates" / "config.example.yaml")
    assert validate({"scenes": [TITLE]}, cfg)["music"]["style"] == "koto"


# --- 5. exact paths -------------------------------------------------------------------------------------

def _diagram(**over):
    return {"type": "diagram", "nodes": [dict(n) for n in NODES], **over}


EXACT_PATHS = [
    ({"scenes": [TITLE, TITLE, {"type": "bullets", "items": ["a", {"k": 1}]}]}, "scenes[2].items[1] must be text"),
    ({"scenes": [{"type": "bullets", "items": [[1]]}]}, "scenes[0].items[0] must be text"),
    ({"scenes": [TITLE, _diagram(nodes=[*NODES, {"id": "c", "label": "C", "x": 0, "y": 0},
                                        {"id": "d", "label": "D", "x": 5, "y": 0}])]},
     "scenes[1].nodes[3].x must be a number between 0 and 1"),
    ({"scenes": [_diagram(edges=[{"from": "a", "to": "b"}, {"from": "a", "to": "zzz"}])]},
     "scenes[0].edges[1].to: unknown node 'zzz'"),
    ({"scenes": [_diagram(flash=[{"node": "nope"}])]}, "scenes[0].flash[0].node: unknown node 'nope'"),
    ({"scenes": [TITLE], "meta": {"tags": ["a", "b", {}]}}, "meta.tags[2] must be text"),
    ({"scenes": [TITLE], "music": {"bpm": 999}}, "music.bpm must be a number between 30 and 240"),
    ({"scenes": [TITLE], "music": {"root": "H"}}, "music.root must be one of"),
    ({"scenes": [TITLE], "music": {"seed": 1.5}}, "music.seed must be an integer between 0 and 2147483648"),
    ({"scenes": [TITLE], "poster": {"scene": 1}}, "poster.scene must be an integer between 0 and 0"),
    ({"scenes": [TITLE], "voiceover": {"gap": 99}}, "voiceover.gap must be a number between 0 and 10"),
    ({"scenes": [TITLE], "watermark": {"text": "x", "size": 9999}}, "watermark.size must be a number between 8 and 200"),
    ({"scenes": [TITLE], "formats": ["square", "square"]}, "formats[1]: duplicate format 'square'"),
    ({"scenes": [TITLE], "meta": {"tagz": []}}, "meta.tagz: unknown field for meta"),
    ({"scenes": [TITLE], "voiceover": {"voic": "x"}}, "voiceover.voic: unknown field for voiceover"),
    ({"scenes": [_diagram(nodes=[{**NODES[0], "colour": 1}])]}, "scenes[0].nodes[0].colour: unknown field for a diagram node"),
    ({"scenes": [{"type": "shot", "source": "a.png", "zoom": [1, 2, 3]}]}, "scenes[0].zoom must be a number or [from, to]"),
    ({"scenes": [{"type": "code", "code": "a\nb", "highlight": [9]}]}, "scenes[0].highlight[0] must be a line number between 1 and 2"),
    ({"scenes": [{"type": "code", "code": "x" * 9000}]}, "scenes[0].code is 9000 characters; the limit is 8000 (MAX_CODE_CHARS)"),
    ({"scenes": [TITLE], "seed": 7}, "seed: unknown field for a spec"),
    ({"scenes": [TITLE], "music": {"seed": 7.0}}, "music.seed must be an integer"),
]


@pytest.mark.parametrize("raw,expected", EXACT_PATHS, ids=lambda x: x if isinstance(x, str) else "")
def test_errors_name_the_exact_path(raw, expected):
    with pytest.raises(SpecError) as exc:
        validate(raw)
    assert str(exc.value).startswith(expected)


# --- 6. limits after the merge --------------------------------------------------------------------------

def _wav(path: Path, seconds: float) -> Path:
    voice.write_wav(path, np.full(int(seconds * voice.SAMPLE_RATE), 0.1, dtype=np.float32))
    return path


def test_spec_limits_cannot_exceed_the_absolute_limits():
    two = [{**TITLE, "seconds": 2000}, {**TITLE, "seconds": 2000}]
    with pytest.raises(SpecError, match=r"total length 4000s; the limit is 3600s \(limits.max_seconds, capped at the absolute limit 3600\)"):
        validate({"limits": {"max_seconds": 10000}, "scenes": two})
    with pytest.raises(SpecError, match=r"scenes\[0\].seconds must be a number between 0.1 and 3600"):
        validate({"limits": {"max_seconds": 10000}, "scenes": [{**TITLE, "seconds": 4000}]})
    with pytest.raises(SpecError, match=r"1001 scenes; the limit is 1000 \(limits.max_scenes, capped"):
        validate({"limits": {"max_scenes": 10**6}, "scenes": [TITLE] * 1001})
    assert validate({"limits": {"max_seconds": 10000}, "scenes": [TITLE]})["limits"]["max_seconds"] == 3600


def test_clamp_limits():
    assert clamp_limits({"max_seconds": 10**9, "max_scenes": 5}) == {"max_seconds": 3600, "max_scenes": 5}
    assert clamp_limits({}) == {"max_seconds": 600, "max_scenes": 200}
    for bad in (None, [], "x", {"max_seconds": 0}, {"max_seconds": -1}, {"max_seconds": True}, {"max_seconds": "9"},
                {"max_seconds": float("nan")}, {"max_scenes": 2.5}, {"max_scenes": 0}, {"nope": 1}):
        with pytest.raises(ValueError):
            clamp_limits(bad)


def test_config_limits_are_clamped_and_checked_on_load(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("limits: {max_seconds: 99999, max_scenes: 99999}\n")
    assert load_config(explicit=cfg).data["limits"] == HARD_LIMITS
    cfg.write_text("limits: {max_seconds: soon}\n")
    with pytest.raises(ConfigError, match="limits.max_seconds must be a positive number"):
        load_config(explicit=cfg)


@pytest.mark.parametrize("body,msg", [
    ("url: 5", "voice_server.url must be a URL string"),
    ("url: ''", "voice_server.url must be a URL string"),
    ("client_id: ''", "voice_server.client_id must be a non-empty string"),
    ("client_id: " + "x" * 201, "of at most 200 characters"),
    ("client_id: [a]", "voice_server.client_id"),
    ("timeout: 0", "voice_server.timeout must be a number"),
    ("timeout: 99999", "voice_server.timeout must be a number"),
    ("timeout: fast", "voice_server.timeout must be a number"),
    ("timeout: true", "voice_server.timeout must be a number"),
])
def test_voice_server_values_are_checked(tmp_path, monkeypatch, body, msg):
    user = tmp_path / "user.yaml"
    user.write_text("voice_server: {" + body + "}\n")
    monkeypatch.setenv("YAMLREEL_CONFIG", str(user))
    with pytest.raises(ConfigError, match=msg):
        load_config()


def test_a_sane_voice_server_loads(tmp_path, monkeypatch):
    user = tmp_path / "user.yaml"
    user.write_text("voice_server: {url: 'http://127.0.0.1:3900/mcp/', client_id: me, timeout: 30}\n")
    monkeypatch.setenv("YAMLREEL_CONFIG", str(user))
    assert load_config().voice_server == {"url": "http://127.0.0.1:3900/mcp/", "client_id": "me", "timeout": 30}


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """The checks under test fail before ffmpeg would run; make render() get that far without it."""
    monkeypatch.setattr(render_mod, "ffmpeg_exe", lambda: str(Path(sys.executable).with_name("no-such-ffmpeg")))


def test_render_checks_the_length_after_narration_stretch(tmp_path, fake_ffmpeg):
    _wav(tmp_path / "v.wav", 5.0)
    f = spec_file(tmp_path, "limits: {max_seconds: 5}\nmusic: {style: none}\n"
                            "scenes:\n  - {type: title, title: x, seconds: 1, narration_audio: v.wav}\n")
    spec = load_spec(f)  # 1 s passes validation; the 5 s clip stretches the scene to 5.5 s
    with pytest.raises(RenderError, match=r"after narration and beat snapping the video is 5\.5s; the limit is 5s \(limits.max_seconds\)"):
        render(spec, "square", tmp_path / "o", fps=5, scale=0.1, runtime=Runtime(roots=[tmp_path]))


def test_render_checks_the_length_after_beat_snapping(tmp_path, fake_ffmpeg):
    f = spec_file(tmp_path, "limits: {max_seconds: 5}\nmusic: {style: none, bpm: 30, snap: true}\n"
                            "scenes:\n" + "  - {type: title, title: x, seconds: 0.2}\n" * 4)
    spec = load_spec(f)  # 0.8 s unsnapped; four 2 s beats once snapped
    with pytest.raises(RenderError, match=r"after narration and beat snapping the video is 8\.0s; the limit is 5s"):
        render(spec, "square", tmp_path / "o", fps=5, scale=0.1)


def test_render_applies_the_absolute_limit_even_if_the_spec_was_not_validated(tmp_path, fake_ffmpeg):
    spec = validate({"scenes": [TITLE], "music": {"style": "none"}})
    spec["limits"] = {"max_seconds": 10**6, "max_scenes": 10}
    spec["scenes"] = [{**spec["scenes"][0], "seconds": 4000.0}]
    with pytest.raises(RenderError, match=r"the limit is 3600s"):
        render(spec, "square", tmp_path / "o", fps=5, scale=0.1)


def test_frame_budget_is_checked_in_validate_the_cli_and_render(tmp_path, monkeypatch, fake_ffmpeg, capsys):
    monkeypatch.setattr(specs, "MAX_FRAMES", 100)
    with pytest.raises(SpecError, match=r"estimated 150 frames \(5s x 30 fps x 1 format\); the limit is 100 \(MAX_FRAMES\)"):
        validate({"scenes": [{**TITLE, "seconds": 5}]})
    f = spec_file(tmp_path, "music: {style: none}\nscenes:\n  - {type: title, title: x, seconds: 2}\n")
    assert main(["validate", str(f)]) == 0  # 60 frames
    lines = cli_error(capsys, "render", str(f), "--format", "all", "--out", str(tmp_path / "o"))
    assert len(lines) == 1 and "estimated 180 frames" in lines[0] and "MAX_FRAMES" in lines[0]
    lines = cli_error(capsys, "render", str(f), "--fps", "60", "--out", str(tmp_path / "o"))
    assert "estimated 120 frames" in lines[0] and "MAX_FRAMES" in lines[0]
    with pytest.raises(RenderError, match="MAX_FRAMES"):
        render(load_spec(f), "square", tmp_path / "o", fps=60, scale=0.1)


def test_the_real_frame_budget_rejects_an_hour_in_three_formats(tmp_path, capsys):
    f = spec_file(tmp_path, "limits: {max_seconds: 3600}\nformats: [landscape, vertical, square]\nfps: 60\n"
                            "scenes:\n  - {type: title, title: x, seconds: 3600}\n")
    lines = cli_error(capsys, "validate", str(f))
    assert "estimated 648000 frames (3600s x 60 fps x 3 formats)" in lines[0] and "(MAX_FRAMES)" in lines[0]
    ok = spec_file(tmp_path, "limits: {max_seconds: 3600}\nformats: [landscape, vertical]\nfps: 30\n"
                             "scenes:\n  - {type: title, title: x, seconds: 3600}\n", "ok.yaml")
    assert main(["validate", str(ok)]) == 0  # exactly MAX_FRAMES: allowed
    check_frame_budget(MAX_FRAMES / 60, 60, 1)
    with pytest.raises(SpecError):
        check_frame_budget(MAX_FRAMES / 60 + 1, 60, 1)


def test_cli_fps_flag_is_range_checked(tmp_path, capsys):
    f = spec_file(tmp_path, "music: {style: none}\nscenes:\n  - {type: title, title: x}\n")
    for bad in ("0", "-5", "61"):
        assert cli_error(capsys, "render", str(f), "--fps", bad, "--out", str(tmp_path / "o")) == [
            "error: --fps must be an integer between 1 and 60"]


def test_aliased_lists_are_length_checked_before_they_are_walked():
    bomb = [{"id": "a", "label": "A", "x": 0, "y": 0}] * 200_000
    with pytest.raises(SpecError, match=r"scenes\[0\].nodes has 200000 entries; the limit is 40 \(MAX_NODES\)"):
        validate({"scenes": [{"type": "diagram", "nodes": bomb}]})
    with pytest.raises(SpecError, match=r"200000 scenes; the limit is 1000"):
        validate({"limits": {"max_scenes": 1000}, "scenes": [TITLE] * 200_000})


# --- 7. capture size and diagram limits -----------------------------------------------------------------

def test_capture_pixel_bound_in_the_spec():
    with pytest.raises(SpecError, match=r"scenes\[0\]\.viewport: 3840x3840 at scale 3 is 132710400 pixels; "
                                        r"the limit is 36000000 \(MAX_CAPTURE_PIXELS\)"):
        validate({"scenes": [{"type": "shot", "source": "https://example.com", "viewport": [3840, 3840], "scale": 3}]})
    validate({"scenes": [{"type": "shot", "source": "https://example.com", "viewport": [3840, 2160], "scale": 2}]})  # 33.2M


def test_capture_pixel_bound_in_the_capture_itself(tmp_path):
    with pytest.raises(CaptureError, match="MAX_CAPTURE_PIXELS"):
        shots._capture("https://example.com/", {"viewport": [3840, 3840], "scale": 3}, tmp_path / "x.png", [], False, None)


def test_full_page_capture_is_clipped_to_the_pixel_bound():
    assert shots.full_page_clip_height(1920, 2, 1000) == 1000
    assert shots.full_page_clip_height(1920, 2, 10**7) == 36_000_000 // (1920 * 4) == 4687
    assert shots.full_page_clip_height(3840, 3, 10**7) == 1041
    assert shots.full_page_clip_height(3840, 3, 0) == 1
    assert 1920 * shots.full_page_clip_height(1920, 2, 10**7) * 4 <= shots.MAX_CAPTURE_PIXELS


def test_diagram_size_limits():
    many = [{"id": str(n), "label": "a", "x": 0, "y": 0} for n in range(41)]
    with pytest.raises(SpecError, match=r"scenes\[0\].nodes has 41 entries; the limit is 40 \(MAX_NODES\)"):
        validate({"scenes": [{"type": "diagram", "nodes": many}]})
    for bad in (15, 121):
        with pytest.raises(SpecError, match=r"scenes\[0\].node_size must be a number between 16 and 120"):
            validate({"scenes": [_diagram(node_size=bad)]})
    with pytest.raises(SpecError, match=r"\.id must be text or an integer of 1 to 100 characters"):
        validate({"scenes": [_diagram(nodes=[{**NODES[0], "id": "x" * 101}])]})
    with pytest.raises(SpecError, match=r"scenes\[0\].nodes\[0\].at must be a number between 0 and 600"):
        validate({"scenes": [_diagram(nodes=[{**NODES[0], "at": 601}])]})


def test_a_failing_shot_scene_names_its_path(tmp_path):
    spec = validate({"scenes": [TITLE, {"type": "shot", "source": "missing.png"}]})
    spec["_dir"] = str(tmp_path)
    with pytest.raises(CaptureError, match=r"^scenes\[1\]\.source: image not found"):
        Timeline(spec, "landscape", tmp_path, scale=0.1, runtime=Runtime(roots=[tmp_path]))


def test_watermark_image_limit_is_a_named_constant(tmp_path, monkeypatch):
    from PIL import Image
    Image.new("RGB", (100, 100)).save(tmp_path / "logo.png")
    monkeypatch.setattr(render_mod, "MAX_WATERMARK_PIXELS", 5000)
    spec = validate({"scenes": [TITLE], "watermark": {"image": "logo.png"}}, base_dir=tmp_path)
    spec["_dir"] = str(tmp_path)
    with pytest.raises(RenderError, match=r"100x100; the limit is 5000 pixels \(MAX_WATERMARK_PIXELS\)"):
        Timeline(spec, "landscape", tmp_path, scale=0.1, runtime=Runtime(roots=[tmp_path]))


# --- 8. the hint hook (issue #13) -----------------------------------------------------------------------

def test_default_unknown_key_message_has_no_hint():
    assert specs.suggest("titel", ["title"]) is None
    with pytest.raises(SpecError) as exc:
        validate({"scenes": [{**TITLE, "titel": "y"}]})
    assert not str(exc.value).endswith(")") and "(" not in str(exc.value)


def test_suggest_hook_text_is_appended(monkeypatch):
    seen = []
    monkeypatch.setattr(specs, "suggest", lambda key, allowed: seen.append((key, sorted(allowed))) or "hint")
    with pytest.raises(SpecError) as exc:
        validate({"scenes": [{**TITLE, "titel": "y"}]})
    assert str(exc.value).endswith(" (hint)")
    assert seen[0][0] == "titel" and "title" in seen[0][1]
