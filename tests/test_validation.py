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
