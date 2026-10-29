# Contributing to yamlreel

Thanks for helping. Bug reports, new scene types, examples and docs are all welcome. Please read the
[Code of Conduct](CODE_OF_CONDUCT.md) first.

## Dev setup

You need Python 3.10 or newer.

```bash
git clone https://github.com/nsijwali/yamlreel.git
cd yamlreel
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -e '.[dev,ffmpeg]'
```

yamlreel draws text with the DejaVu fonts. On Debian/Ubuntu run `sudo apt install fonts-dejavu-core`, on macOS
`brew install --cask font-dejavu`, on Windows install them from [dejavu-fonts.github.io](https://dejavu-fonts.github.io).
The README has the details for other systems. Web-capture tests need `pip install -e '.[capture]'` and
`playwright install chromium`; they are skipped when those are missing.

## Running the tests

```bash
pytest -q
```

Tests never read your own user or project config, and checks that need `ffprobe`, Playwright or Chromium are
skipped when those tools are missing. CI runs the suite on Ubuntu with Python 3.10 and 3.12, so please make sure
it passes locally before you open a pull request.

## Adding a scene type

1. Write a scene class in `yamlreel/scenes.py` that renders a PIL image for a local time `t`, like the existing
   ones, and register it in the `SCENE_TYPES` dictionary at the bottom of the file.
2. Teach `yamlreel/specs.py` about it: add the type to `DEFAULT_SECONDS` and `REQUIRED`, and validate its fields in
   `_check_scene` with clear `SpecError` messages. Bound every size, count and string length.
3. Add tests in `tests/test_yamlreel.py`: a valid spec renders at the right size in every format, and invalid
   specs are rejected with a clear message.
4. Document the scene in the README table and, if useful, add it to an example under `examples/`.

## Branches and pull requests

- `main` is protected. Work on a branch (`fix/short-name`, `feat/short-name`) and open a pull request.
- Both CI checks must pass before a pull request can merge.
- Pull requests are squash-merged, so the pull request title becomes the commit message on `main`.
- Keep pull requests focused: one change, with tests, and the README updated if behaviour changed.

## Commit style

Write the subject in the imperative, under about 70 characters, with no trailing full stop ("Add a stat scene
unit option", "Fix caption overflow in vertical frames"). Add a short body explaining why when it is not obvious.

## Security

Specs are untrusted input: a downloaded spec must not be able to read arbitrary files, reach internal networks or
send text to a server of its choosing. Any change that touches file access, URL capture, configuration that
widens access, or the voice server needs tests that show the new behaviour stays inside those limits. See
[SECURITY.md](SECURITY.md) for the model.

Do not report vulnerabilities in a public issue. Use the Security tab ("Report a vulnerability") as described in
[SECURITY.md](SECURITY.md).
