"""Compatibility with the pre-0.2 name. Delete this file together with yamlreel/legacy.py in 0.3.0."""
import importlib.metadata

import pytest
import yaml

from yamlreel import legacy
from yamlreel.cli import main
from yamlreel.config import ConfigError, load_config


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    for prefix in ("YAMLREEL", "VIDGEN"):
        for name in ("CONFIG", "FONT_DIR", "CHROMIUM"):
            monkeypatch.delenv(f"{prefix}_{name}", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    legacy._warned.clear()
    return tmp_path


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def lines(capsys):
    return [ln for ln in capsys.readouterr().err.splitlines() if ln]


@pytest.mark.parametrize("name,check", [
    ("FONT_DIR", lambda c: c.font_dirs == ["/fonts"]),
    ("CHROMIUM", lambda c: c.chromium_path == "/fonts"),
])
def test_legacy_env_vars_are_used_with_one_warning(name, check, monkeypatch, capsys):
    monkeypatch.setenv(f"VIDGEN_{name}", "/fonts")
    cfg = load_config()
    assert check(cfg)
    out = lines(capsys)
    assert len(out) == 1 and f"VIDGEN_{name} is deprecated, use YAMLREEL_{name}" in out[0]


def test_legacy_config_env_var(env, monkeypatch, capsys):
    p = write(env / "c.yaml", {"voice_server": {"url": "http://127.0.0.1:1/mcp/"}, "theme": "washi"})
    monkeypatch.setenv("VIDGEN_CONFIG", str(p))
    cfg = load_config()
    assert cfg.voice_server["url"] == "http://127.0.0.1:1/mcp/" and cfg.sources == [p]
    out = lines(capsys)
    assert len(out) == 1 and "VIDGEN_CONFIG is deprecated, use YAMLREEL_CONFIG" in out[0]


@pytest.mark.parametrize("name", ["FONT_DIR", "CHROMIUM"])
def test_new_env_var_wins_and_is_silent(name, monkeypatch, capsys):
    monkeypatch.setenv(f"VIDGEN_{name}", "/old")
    monkeypatch.setenv(f"YAMLREEL_{name}", "/new")
    cfg = load_config()
    assert (cfg.font_dirs == ["/new"]) if name == "FONT_DIR" else (cfg.chromium_path == "/new")
    assert lines(capsys) == []


def test_legacy_config_env_new_wins(env, monkeypatch, capsys):
    new = write(env / "new.yaml", {"theme": "washi"})
    old = write(env / "old.yaml", {"theme": "sumi"})
    monkeypatch.setenv("YAMLREEL_CONFIG", str(new))
    monkeypatch.setenv("VIDGEN_CONFIG", str(old))
    assert load_config().sources == [new]
    assert lines(capsys) == []


def test_legacy_user_config_is_loaded_as_trusted(env, capsys):
    old = write(env / "xdg" / "vidgen" / "config.yaml",
                {"voice_server": {"url": "http://127.0.0.1:1/mcp/"}, "allow_paths": ["/data"], "theme": "washi"})
    cfg = load_config()
    assert cfg.sources == [old]
    assert cfg.voice_server["url"] == "http://127.0.0.1:1/mcp/" and [str(p) for p in cfg.allow_paths] == ["/data"]
    out = lines(capsys)
    assert len(out) == 1 and "deprecated" in out[0] and str(old) in out[0]


def test_both_user_configs_new_wins_without_merge(env, capsys):
    new = write(env / "xdg" / "yamlreel" / "config.yaml", {"theme": "washi"})
    old = write(env / "xdg" / "vidgen" / "config.yaml", {"watermark": "Old", "allow_paths": ["/data"]})
    cfg = load_config()
    assert cfg.sources == [new]
    assert "watermark" not in cfg.data and cfg.allow_paths == []
    out = lines(capsys)
    assert len(out) == 1 and "ignoring legacy config" in out[0] and str(old) in out[0]


def test_symlinked_legacy_dir_is_not_reported_as_ignored(env, capsys):
    new = write(env / "xdg" / "yamlreel" / "config.yaml", {"theme": "washi"})
    (env / "xdg" / "vidgen").symlink_to(env / "xdg" / "yamlreel")
    assert load_config().sources == [new]
    assert lines(capsys) == []


def test_legacy_project_config_loads_but_stays_untrusted(env, capsys):
    old = write(env / "vidgen.config.yaml", {"theme": "washi"})
    cfg = load_config()
    assert cfg.sources == [old] and cfg.data["theme"] == "washi"
    assert len(lines(capsys)) == 1
    for key, val in (("allow_paths", ["/"]), ("voice_server", {"url": "http://127.0.0.1:1/mcp/"})):
        write(old, {key: val})
        with pytest.raises(ConfigError):
            load_config()


def test_both_project_configs_new_wins_without_merge(env, capsys):
    new = write(env / "yamlreel.config.yaml", {"theme": "washi"})
    write(env / "vidgen.config.yaml", {"watermark": "Old"})
    cfg = load_config()
    assert cfg.sources == [new] and "watermark" not in cfg.data
    assert len(lines(capsys)) == 1


def test_warnings_are_printed_once_per_item(env, monkeypatch, capsys):
    write(env / "xdg" / "vidgen" / "config.yaml", {"theme": "washi"})
    write(env / "vidgen.config.yaml", {"theme": "sumi"})
    monkeypatch.setenv("VIDGEN_FONT_DIR", "/f")
    load_config()
    first = lines(capsys)
    load_config()
    assert len(first) == 3 and len(set(first)) == 3
    assert lines(capsys) == []


def test_legacy_script_warns_and_delegates(env, capsys):
    from yamlreel.cli import main as cli_main
    spec = env / "s.yaml"
    assert cli_main(["init", "code-tip", str(spec)]) == 0
    capsys.readouterr()
    assert legacy.main(["validate", str(spec)]) == 0
    assert "`vidgen` command is now `yamlreel`" in capsys.readouterr().err


def test_console_scripts_are_registered():
    eps = {e.name: e.value for e in importlib.metadata.entry_points(group="console_scripts")
           if e.name in ("yamlreel", "vidgen")}
    assert eps == {"yamlreel": "yamlreel.cli:main", "vidgen": "yamlreel.legacy:main"}


@pytest.mark.parametrize("var", ["YAMLREEL_CONFIG", "VIDGEN_CONFIG"])
def test_explicit_config_env_has_no_path_fallback(env, monkeypatch, var):
    write(env / "xdg" / "vidgen" / "config.yaml", {"allow_paths": ["/data"]})
    monkeypatch.setenv(var, str(env / "missing.yaml"))
    cfg = load_config()
    assert cfg.sources == [] and cfg.allow_paths == []


def test_empty_new_env_var_means_unset(monkeypatch, capsys):
    monkeypatch.setenv("YAMLREEL_FONT_DIR", "")
    monkeypatch.setenv("VIDGEN_FONT_DIR", "/old")
    assert load_config().font_dirs == ["/old"]
    assert len(lines(capsys)) == 1


def test_legacy_project_config_error_and_all_trusted_keys(env):
    old = env / "vidgen.config.yaml"
    for key, val in (("allow_paths", ["/"]), ("allow_private_hosts", True), ("chromium_path", "/bin/sh"),
                     ("font_dir", "/"), ("voice_server", {"url": "http://127.0.0.1:1/mcp/"})):
        write(old, {key: val})
        with pytest.raises(ConfigError, match="only read from the user config"):
            load_config()


def test_ignoring_warning_names_the_legacy_path(env, capsys):
    write(env / "xdg" / "yamlreel" / "config.yaml", {"theme": "washi"})
    old = write(env / "xdg" / "vidgen" / "config.yaml", {"theme": "sumi"})
    load_config()
    assert f"ignoring legacy config {old}" in capsys.readouterr().err


def test_legacy_script_exit_codes(env):
    assert legacy.main(["validate", "missing.yaml"]) == 1
    with pytest.raises(SystemExit) as e:
        legacy.main(["nope"])
    assert e.value.code == 2


def test_relative_xdg_config_home_is_ignored(env, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "rel")
    monkeypatch.setenv("HOME", str(env / "fakehome"))
    for d in ("yamlreel", "vidgen"):
        write(env / "rel" / d / "config.yaml", {"allow_paths": ["/data"]})
        write(env / d / "config.yaml", {"allow_paths": ["/data"]})
    cfg = load_config()
    assert cfg.sources == [] and cfg.allow_paths == []


def test_config_init_user_refuses_to_shadow_a_legacy_config(env, capsys):
    old = write(env / "xdg" / "vidgen" / "config.yaml", {"theme": "washi"})
    assert main(["config", "init", "--user"]) == 2
    err = capsys.readouterr().err
    assert str(old) in err and "mv " in err
    assert not (env / "xdg" / "yamlreel" / "config.yaml").exists()
    assert main(["config", "init", "--user", "--force"]) == 0
    assert (env / "xdg" / "yamlreel" / "config.yaml").exists()
