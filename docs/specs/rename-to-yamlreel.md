# Spec: rename vidgen to yamlreel (0.2.0)

Status: implemented in 0.2.0 (released 2026-10-29); yamlreel is now on PyPI. Kept as a design record. Owner: nsijwali.

## 1. Problem and requirements
"vidgen" collides with ~343 GitHub repos and an unrelated PyPI package. "yamlreel" is free on both.
- F1: every user-visible name becomes `yamlreel`: package dir, imports, distribution name, console script,
  argparse `prog`, env vars, config paths, default output dir, MCP client id, temp-dir prefix, docs, comments, badge/clone URLs.
- F2: existing installs keep working during a compatibility window and print a clear one-line deprecation warning.
- Q1: the trust model in SECURITY.md stays exactly the same. Trusted keys come only from the user config or the CLI.
  That includes the legacy user-config path. A legacy project config stays untrusted.
- Q2: all compatibility code lives in one module plus one test file, so 0.3.0 can delete it in one commit.

## 2. Constraints and assumptions
- Not on PyPI. Users install with `pip install -e '.[ffmpeg]'` from a checkout. Nobody imports `vidgen` from outside the repo (assumed: it is a CLI tool with no documented API).
- The owner has `~/.config/vidgen/config.yaml` (with `voice_server` and `voiceover.voice`) and a script that calls `.venv/bin/vidgen`.
- An existing editable install still points at the `vidgen/` dir. After `git pull` it breaks (ImportError) until
  the user reinstalls. No code can fix that, so the migration note must say to reinstall.
- `docs/preview.jpg` contains no "vidgen" text (checked). `.github/workflows/ci.yml` has no references.

## 3. Design

### 3.1 Decisions
1. **Rename fully, with a compatibility window until 0.3.0.** Fallbacks: the `vidgen` console script, `VIDGEN_*` env vars,
   the legacy user and project config paths. Each one warns once per process on stderr:
   `warning: <old> is deprecated, use <new> (support ends in yamlreel 0.3.0)`.
   **No `vidgen` import shim** (no external importers; it would ship a second top-level package).
2. **Configs are never merged across names.** For each slot (user, project), the new path is used if it exists. Otherwise
   the legacy path is used if it exists (warn). If both exist, the new one is used and a warning names the ignored legacy file.
   There is no warning when both resolve to the same file (symlink). Explicit `YAMLREEL_CONFIG` / `VIDGEN_CONFIG` / `--config`
   are used as given, with no path fallback. That matches today's `VIDGEN_CONFIG` behaviour and keeps the test fixture isolated.
3. **Env vars:** `YAMLREEL_X` wins when set and non-empty. Otherwise `VIDGEN_X` is used (warn). X in {CONFIG, FONT_DIR, CHROMIUM}.
4. **Default `output_dir` becomes `yamlreel-output`.** There is no fallback to an existing `vidgen-output/`. `.gitignore` keeps
   `vidgen-output/` and `vidgen.config.yaml` next to the new entries, so old checkouts never commit them.
   Side effect: the narration cache in `vidgen-output/.cache` is not reused. Users can avoid that with `mv vidgen-output yamlreel-output` (migration note).
5. **MCP client id and `clientInfo.name` become `yamlreel`.** VoiceStudio bindings keyed on `vidgen` only apply when a
   render sends no `voiceover.voice`. The owner sets `voice` explicitly, so nothing changes for them. Anyone else
   can keep the old binding with one line, `voice_server: {client_id: vidgen}` (an existing user-config key),
   or rebind in VoiceStudio. The migration note says this. We do not auto-inject the old id, because that would be magic and permanent.
6. **Version 0.2.0** (pre-1.0 breaking change: import and distribution names). The legacy shims are removed in 0.3.0.
7. `git mv vidgen yamlreel` and `git mv tests/test_vidgen.py tests/test_yamlreel.py`, as separate commits, to keep history.

### 3.2 New module `yamlreel/legacy.py` (the only code that may say "vidgen")
```
LEGACY = "vidgen"; REMOVE_IN = "0.3.0"
LEGACY_PROJECT_FILENAME = "vidgen.config.yaml"
_warned: set[str]                                   # warn-once registry (tests clear it)
def warn_once(key: str, msg: str) -> None           # print(f"warning: {msg}", file=sys.stderr) once per key
def env(suffix: str) -> str | None                  # YAMLREEL_<suffix> or VIDGEN_<suffix> (+warn)
def legacy_user_config(base: Path) -> Path          # base / "vidgen" / "config.yaml"
def pick(new: Path, old: Path) -> Path              # rule 3.1(2); returns new when neither exists
def main(argv: list[str] | None = None) -> int      # warn "the `vidgen` command is now `yamlreel`", return cli.main(argv)
```

### 3.3 File-by-file changes
| File | Change |
|---|---|
| `vidgen/` -> `yamlreel/` | `git mv`, no content changes in that commit. |
| `pyproject.toml` | `name = "yamlreel"`, `version = "0.2.0"`, scripts `yamlreel = "yamlreel.cli:main"` and `vidgen = "yamlreel.legacy:main"`, `include = ["yamlreel*"]`, package-data key `yamlreel`. |
| `yamlreel/config.py` | `CONFIG_FILENAME = "yamlreel.config.yaml"`, `DEFAULTS["output_dir"] = "yamlreel-output"`. `user_config_path()` (the **write** target) returns `legacy.env("CONFIG")` or `$XDG_CONFIG_HOME/yamlreel/config.yaml`. New `find_user_config()` (the **read** path) uses that same path when the env var is set, else `legacy.pick(new_default, legacy.legacy_user_config(base))`. `load_config`: `user = find_user_config()`. The default project path is `legacy.pick(cwd/CONFIG_FILENAME, cwd/legacy.LEGACY_PROJECT_FILENAME)`, still `trusted=False`. `legacy.env("CHROMIUM")` and `legacy.env("FONT_DIR")`. Update the docstring. |
| `yamlreel/cli.py` | `prog="yamlreel"`, `resources.files("yamlreel")` (x2), `Path(CONFIG_FILENAME)` for `config init`, help strings, module docstring. |
| `yamlreel/voice.py` | `client_id: str = "yamlreel"`, `clientInfo.name "yamlreel"`, message texts (`yamlreel config init --user`). |
| `yamlreel/render.py` | `tempfile.TemporaryDirectory(prefix="yamlreel-")`. |
| `yamlreel/{__init__,safety,theme}.py` | Docstring and message text (`YAMLREEL_FONT_DIR`). |
| `yamlreel/templates/*.yaml` | Config filenames and paths, `output_dir: yamlreel-output`, `client_id: yamlreel`. |
| `examples/cache-stampede-45s.yaml` | Commands in the comments. |
| `.gitignore` | Add `yamlreel-output/` and `yamlreel.config.yaml`. Keep both vidgen lines under `# pre-0.2 names`. |
| `README.md` | Title, badge, clone URLs and `cd yamlreel`, all commands, paths and env vars. The PyPI note becomes "not on PyPI; install from this repository". New section "Renamed from vidgen" (3.4). |
| `SECURITY.md` | Name only. Add: "the user config, at its current or pre-rename location (see README), is the only file that can set trusted keys." Keep it vidgen-free. |
| `tests/test_vidgen.py` -> `tests/test_yamlreel.py` | `git mv`, then imports, `TEMPLATES` path, filenames. Fixtures `no_personal_config` and `home` set `YAMLREEL_CONFIG`, set `XDG_CONFIG_HOME` to a tmp dir, delenv all six `{YAMLREEL,VIDGEN}_{CONFIG,FONT_DIR,CHROMIUM}`, and clear `legacy._warned`. |
| `tests/test_legacy_names.py` (new) | The tests in section 7 that mention the old names. |

### 3.4 README migration note (the only vidgen prose)
> **Renamed from vidgen (0.2.0).** `git pull`, then `pip uninstall -y vidgen && rm -rf vidgen.egg-info && pip install -e '.[ffmpeg]'`.
> Move `~/.config/vidgen/` to `~/.config/yamlreel/` and `vidgen.config.yaml` to `yamlreel.config.yaml`, rename `VIDGEN_*` env vars to `YAMLREEL_*`, and optionally `mv vidgen-output yamlreel-output` to keep the narration cache.
> The old names keep working with a warning until 0.3.0. If VoiceStudio binds a voice to client `vidgen`, set `voice_server.client_id: vidgen` or rebind it.

## 4. Alternatives rejected
- **Hard rename, no compatibility:** it would break the owner's script and voice setup on the next pull. The shims are about 50 isolated lines that can be deleted later.
- **Merging legacy and new configs:** precedence would be unclear, and trusted keys could come from two files. One file per slot is easier to audit.
- **Auto-migrating files** (moving `~/.config/vidgen` on first run): it writes to the user's home without asking, and is hard to undo.
- **A `vidgen` import shim package:** it has no consumers and would add a second top-level package.
- **Keeping client id `vidgen`:** it leaves the old name in the protocol forever. The opt-in config line covers anyone who needs it.

## 5. Rollout and backward compatibility (in order)
1. Merge-ready PR on `rename/yamlreel` with CI green (3.10, 3.12).
2. **Rename the GitHub repo** to `nsijwali/yamlreel` right before merging. Open PRs survive a rename, and main's new badge and clone URLs then work from the start. GitHub redirects old URLs. Never create a new `nsijwali/vidgen`, because that would break the redirect.
3. Merge to main.
4. Locally: `git remote set-url origin https://github.com/nsijwali/yamlreel.git` (or the SSH form), then `git pull`, then reinstall per 3.4.
5. Owner migration: `mv ~/.config/vidgen ~/.config/yamlreel`; in the script, change `.venv/bin/vidgen` to `.venv/bin/yamlreel`; rename any `VIDGEN_*` exports; `mv vidgen-output yamlreel-output` where wanted. Verify that `yamlreel config show` lists `~/.config/yamlreel/config.yaml` in `_sources` and prints no warning.
6. Optional: rename the local checkout dir and recreate `.venv`, because its scripts embed absolute paths. Reserve `yamlreel` on PyPI to prevent squatting.
7. In 0.3.0: delete `legacy.py`, the `vidgen` script line and `test_legacy_names.py`, and shorten the README note to one line. Keep the `.gitignore` lines.

## 6. Risks, failure modes, edge cases, observability
- **Stale editable install:** after the pull, `vidgen` and `yamlreel` both fail until reinstall. Mitigation: the note puts the reinstall command first.
- **A legacy project file gaining trust:** it must load with `trusted=False`. A test covers it.
- **Silent shadowing:** `config init --user` writes the new path while the legacy one still exists. Mitigation: the "both exist, ignoring X" warning on every later run.
- **The same file via symlink:** no ignoring-warning, and the file loads once (the existing `resolve()` check for project vs user stays).
- **`XDG_CONFIG_HOME` set:** the legacy path uses the same base. An empty env var counts as unset (as today).
- **Scripts that parse stderr:** at most one line per legacy name per process, and stdout is unchanged.
- **Observability:** `config show` `_sources` shows the file actually used. The deprecation warnings name both the old and the new name.

## 7. Test plan (all in CI, no network)
In `tests/test_legacy_names.py`, with `XDG_CONFIG_HOME` set to tmp, both `*_CONFIG` vars unset, and cwd set to tmp:
1. Only `VIDGEN_FONT_DIR` / `VIDGEN_CHROMIUM` / `VIDGEN_CONFIG` set: each value is used and stderr has one deprecation line per var. Both prefixes set: the YAMLREEL values win and nothing about that var is printed.
2. Only `$XDG/vidgen/config.yaml` exists, with `voice_server` and `allow_paths`: they are loaded (trusted) and `_sources` is the legacy path. stderr warns.
3. Both user paths exist, with different keys: only the new file's keys are present (no merge), and the warning names the ignored legacy file.
4. Only `./vidgen.config.yaml` exists: it loads. If it contains `allow_paths` or `voice_server`, it raises `ConfigError` (untrusted).
5. Both project files exist: the new one wins and there is no merge.
6. Two `load_config()` calls in one process produce exactly one warning per legacy item.
7. `legacy.main(["validate", spec])` returns 0 and stderr contains "`vidgen` command is now `yamlreel`". `importlib.metadata.entry_points(group="console_scripts")` maps `yamlreel` to `yamlreel.cli:main` and `vidgen` to `yamlreel.legacy:main`.

In `tests/test_yamlreel.py`:

8. With only new names in use, stderr has no "deprecated".
9. The default `output_dir` ends with `yamlreel-output`. `VoiceServer().client_id == "yamlreel"`. `config init` writes `./yamlreel.config.yaml`. `config init --user` writes `$XDG/yamlreel/config.yaml`.
10. The whole existing suite passes unchanged in substance.

**Acceptance:**
- `git grep -il vidgen` lists only `yamlreel/legacy.py`, `tests/test_legacy_names.py`, `pyproject.toml` (the scripts line only), `.gitignore` (2 lines), `README.md` (the 3.4 section only) and this spec.
- `pytest -q` is green.
- In a fresh venv: `pip install -e '.[ffmpeg]'`, then `yamlreel --help`, `yamlreel init system-design s.yaml`, `yamlreel render s.yaml --preview --no-voice` (which produces `yamlreel-output/landscape/video.mp4`), and `vidgen --help` (help plus one warning).
- The upgrade path in 3.4 works in an existing venv.

## 8. Implementation steps (one reviewable commit each)
1. `git mv vidgen yamlreel` and `git mv tests/test_vidgen.py tests/test_yamlreel.py`. Fix `pyproject.toml` and the test imports and `TEMPLATES` so the suite passes.
2. Rename the code strings: cli, voice, render, `__init__`, safety, theme, and `DEFAULTS` / `CONFIG_FILENAME`. Update the fixtures to `YAMLREEL_*`.
3. Add `yamlreel/legacy.py`, wire it into config.py (`env`, `find_user_config`, `pick`), and add the `vidgen` script line. Add `tests/test_legacy_names.py`.
4. Templates, example, and `.gitignore`.
5. README (including 3.4), SECURITY.md, version 0.2.0. Run the `git grep` and fresh-venv acceptance checks.
