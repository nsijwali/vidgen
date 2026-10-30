# Batch 1 "Trust the input": 0.2.2 hardening

Status: proposed. Base: `main` @ e444b45 (v0.2.1). Source: `../yamlreel-review-plan.md` section 1 (findings 1-5, 9, 10).
Four independent PRs (A, B, C, D), merged in that order, then a release PR. Version bump only in the release PR.

## 0. Shared constraints and assumptions
- Python 3.10-3.12 (CI matrix 3.10 + 3.12). The `ipaddress` `is_global`/`is_private` tables differ between patch
  releases (3.11.10/3.12.4/3.13 changed IPv6 and mapped-address handling), so PR A uses explicit tables and uses
  `is_global` only as the last step.
- No new dependencies. CLI contract: every user error exits 1 with exactly one line `error: ...` on stderr.
- Protected files (the daily PR job will not merge these, the owner reviews them): `safety.py` (A), `config.py` (B),
  `voice.py` (C), `.github/` (D). `pyproject.toml` is not touched: the version is `yamlreel/__init__.py`
  (`dynamic = ["version"]`).
- Each PR adds its own test file to keep merges conflict-free: `tests/test_network_guard.py`, `tests/test_validation.py`,
  `tests/test_narration.py`. Existing tests must pass unchanged unless this spec names the edit.
- Why A, B, C, D: A is the only P1 (someone else's spec can reach a tailnet) and is a launch blocker; it is small and
  touches one file. B is the largest and changes error wording, so it lands before C so that C's tests use the final
  messages. B's render-time limit check works on `tl.total` today, and C then moves it onto the shared durations. D
  documents the behaviour A to C actually shipped.

## PR A: network guard (`yamlreel/safety.py`)
**Problem.** On main, `check_http_url(u, allow_private=False)` accepts `http://100.64.0.1/`, `http://100.100.100.200/`
(Alibaba metadata) and `http://[::ffff:100.64.0.1]/`, because 100.64.0.0/10 is neither `is_private` nor `is_reserved`.
`metadata.google.internal.` (trailing dot) also skips the name check. It is still caught, but only because it resolves to
169.254/16. Reproduce with `python -c "from yamlreel.safety import check_http_url as c; c('http://100.64.0.1/', False)"`: no error.
**Verified call sites.** `shots.load_shot` and `shots._request_allowed` call `check_http_url`, and `pinproxy._connect`
calls `pinned_ips`. Both end in one function, so the fix goes in one place. `voice.py` server URLs are trusted and out of scope.
**Decision.** Replace the predicate chain with `classify(ip) -> Verdict` ("allow" | "private" | "deny", plus a reason),
evaluated in this order:
0. Normalise the host: strip brackets; a host with `:` must be an IPv6 literal (anything else is refused without a DNS
   query) and its scope id (`%lo0`) is dropped; otherwise `.encode("idna")` (UnicodeError → refused), lower-case,
   strip trailing dots. Fullwidth digits and ideographic full stops are therefore judged like their ASCII forms, and the
   normalised name is what is resolved. `check_http_url` turns any `urlparse` `ValueError` into `UnsafeInput`.
1. Host name in `METADATA_NAMES = {metadata, metadata.google.internal, metadata.goog, instance-data,
   instance-data.ec2.internal, metadata.tencentyun.com}` → deny. This is defence in depth: the IP rule already
   covers them.
2. Numeric-host forms: if the last dot-label is all digits or `0x...` (the WHATWG "ends in a number" rule), the host
   must parse as a strict `IPv4Address`. Otherwise deny, so `2130706433`, `0x7f.1`, `0177.0.0.1` and `127.1` are refused
   before `getaddrinfo`/`inet_aton` can reinterpret them in a platform-specific way.
3. `::1` → private. Unwrap embedded IPv4 and classify the embedded address instead of the wrapper:
   `ipv4_mapped` (::ffff:0:0/96), `sixtofour` (2002::/16), `teredo` (client address), NAT64 `64:ff9b::/96` (last 32 bits).
   Only `ipv4_mapped` connects to the embedded v4 (as today). The other wrappers keep the original v6 as the pinned address.
4. `ALWAYS_DENY`, which holds even with `--allow-private-hosts`:
   | Network | Why |
   |---|---|
   | 0.0.0.0/8, ::/128 | "this host": 0.0.0.0 reaches local listeners on Linux/macOS |
   | 169.254.0.0/16, fe80::/10 | link-local: AWS/GCP/Azure/OCI/DO metadata 169.254.169.254, ECS creds 169.254.170.2 |
   | fd00:ec2::/32 | AWS IPv6 IMDS (fd00:ec2::254), DNS, NTP: inside ULA, so carve it out before the private tier |
   | fd20:ce::254/128, fd00:c1::a9fe:a9fe/128 | GCP and OCI IPv6 metadata, inside ULA (addresses from memory of the vendors' docs, not re-verified offline; an extra deny is harmless) |
   | 100.100.100.200/32 | Alibaba Cloud metadata, inside CGNAT. The rest of 100.64/10 (Tailscale) stays opt-in |
   | 168.63.129.16/32 | Azure WireServer / host agent |
   | 192.0.0.0/24 | IETF protocol assignments, incl. Oracle Cloud metadata 192.0.0.192. Nothing legitimate to capture |
   | 224.0.0.0/4, ff00::/8, 240.0.0.0/4 (incl. 255.255.255.255) | multicast, reserved, broadcast |
   | ::/96 (IPv4-compatible, after the ::1 check), fec0::/10, 64:ff9b:1::/48 | deprecated or local-NAT64 forms with no fixed embedding |
5. `PRIVATE_OPT_IN` (allowed only with the flag): 127.0.0.0/8, ::1, 10/8, 172.16/12, 192.168/16, 100.64.0.0/10, fc00::/7.
6. Otherwise allow if `ip.is_global`, an IPv6 target is inside `2000::/3` (global unicast; everything else, e.g.
   `4000::1` or `::ffff:0:a9fe:a9fe`, is denied), and not in `NOT_GLOBAL_EXTRA` (192.0.2/24, 198.18/15, 198.51.100/24,
   203.0.113/24, 2001:db8::/32, 3fff::/20, 100::/64, 2001::/23, 5f00::/16). This keeps the result the same on every
   supported Python. Anything else → deny ("not a public internet address").
- **DNS.** `_ips_for(host)` keeps its name and signature (an existing test patches it). Resolution moves into a new seam
  `_resolve(host) -> list[str]` that wraps `getaddrinfo`. Every resolved address must pass: one denied or private-without-flag
  address refuses the whole host, and an empty answer is refused. `pinned_ips` returns the de-duplicated checked
  addresses, and the proxy connects only to those (unchanged).
- **Redirects.** Do not rely on the Playwright `route` guard for redirect hops (Playwright documents that route handlers
  may see only the first URL of a redirect chain). The pinning proxy is the enforcement point. Every hop is a new proxy
  request or `CONNECT`, which re-runs `pinned_ips`. The route guard stays as a second layer. The proxy forwards exactly one request per
  client connection (`Connection: close` and `Proxy-Connection: close` upstream, the body limited to `Content-Length`,
  the response relayed back one-way), so a reused or pipelined connection can never carry an unchecked second request;
  `CONNECT` tunnels are spliced after the check. A `CONNECT` port must be ASCII digits in 1..65535 (else 400). Every
  403 refusal carries `X-Yamlreel-Refused: 1`.
  The upstream response head is rewritten (`Connection`/`Keep-Alive`/`Proxy-Connection` dropped, `Connection: close` and
  `Proxy-Connection: close` added; body framing headers untouched), the body is relayed until its framing says it is
  complete (HEAD/1xx/204/304 none, `Content-Length`, chunked, else upstream close) and the client socket is then closed.
  A request with a malformed or conflicting `Content-Length`, or a bare LF/CR in its head, gets 400.
  Known and accepted: `Expect: 100-continue` is not special-cased, port 0 fails at connect, and IDNA 2003 (Python's
  codec) is used instead of UTS 46.
- **Follow-up PR (shots.py, after PR B):** the capture fails with `CaptureError` when the network policy refused any
  request (so a refused redirect hop or sub-resource fails the capture instead of rendering a partial page). The
  evidence is the proxy's own refusal record, not a response header or body (an HTTPS CONNECT refusal never reaches the
  page, and a site can forge a header): `PinningProxy` keeps the first 16 `(host, reason)` entries plus a count, and the
  capture's route guard adds direct sub-resources it aborts before they reach the proxy. A host that does not resolve
  (`Unresolvable`) is still refused with 403 but is not a policy decision and is not recorded. The check runs after
  navigation errors, before the fallback navigation, after the settle wait, after the screenshot and after the browser
  closes (the image is deleted on failure). `X-Yamlreel-Refused` stays on the proxy's 403s for humans and is stripped from
  relayed upstream responses; nothing trusts it. Chromium launches with `--force-webrtc-ip-handling-policy=` and
  `--webrtc-ip-handling-policy=disable_non_proxied_udp` so WebRTC cannot bypass the proxy.
- Rejected: `is_global` only (version-dependent, and loses the "metadata even with the flag" rule). Blocking 100.64/10
  even with the flag (breaks legitimate Tailscale dev servers). Resolve-and-allow numeric forms (platform-dependent parsing).
- **Messages.** Keep `cloud metadata host refused: {host}`, `cannot resolve host: {host}` and the existing private
  message (it contains `--allow-private-hosts`; tests match it). New messages:
  `{host} resolves to {ip}[ (embeds {v4})], a cloud-metadata, link-local or reserved address; always refused`;
  `{host} resolves to {ip}, which is not a public internet address; refused`;
  `URL host {host!r} is a non-standard numeric address; write it as a dotted quad such as 127.0.0.1`.
  The `scenes[i].source:` prefix is added by PR B.
- **Tests (fail before / pass after)**, all in `tests/test_network_guard.py`, using a fake `_resolve`:
  - flag off, refused: 100.64.0.1, 100.100.100.200, 100.127.255.254, [::ffff:100.64.0.1], [::ffff:127.0.0.1],
    [64:ff9b::a9fe:a9fe], [2002:a9fe:a9fe::1], a Teredo address embedding 10.0.0.1, 192.0.0.192, 198.18.0.1, 240.0.0.1,
    2130706433, 0x7f.1, 0177.0.0.1, 127.1.
  - flag on, still refused: 169.254.169.254, 169.254.170.2, [fd00:ec2::254], 100.100.100.200, 192.0.0.192,
    `METADATA.GOOGLE.INTERNAL.`, [::ffff:169.254.169.254], [64:ff9b::a9fe:a9fe], 0.0.0.0, [::], 224.0.0.1.
  - flag on, allowed: 100.64.0.1, [::ffff:10.0.0.5], [fd12:3456::1], 172.16.0.1, 127.0.0.1, localhost, [::1].
    Flag off, allowed: 93.184.216.34, [2606:4700:4700::1111].
  - Fake resolver: `mixed.test` → [93.184.216.34, 100.64.0.1] refused with the flag off and allowed with it on.
    `meta-mixed.test` → [93.184.216.34, 169.254.169.254] refused with the flag on. `mapped.test` → [::ffff:100.64.0.1]
    refused with the flag off. `empty.test` → [] gives "cannot resolve".
  - Redirect through the proxy: a local server answers `302 Location: http://169.254.169.254/`. Following the hop via
    `PinningProxy(allow_private=True)` returns 403, and a `create_connection` spy never sees 169.254.169.254. Same with
    the flag off and `Location: http://100.64.0.1/`. `CONNECT 100.64.0.1:443` with the flag off → 403.
  - Optional (skips without Chromium, like the existing capture test): browser redirect to [::ffff:169.254.169.254] → `CaptureError`.

## PR B: strict validation and absolute limits (`specs.py`, `scenes.py`, `config.py`, `cli.py`, `render.py`, `shots.py`)
**Problem (reproduced).** `limits: null` and `formats: [{}]` raise `TypeError` (in `limits["max_seconds"]` and
`f not in FORMATS`). `fps: "oops"` validates. `{type: title, title: x, titel: y}` validates. `limits: {max_seconds: 10000}`
plus a 4000 s scene validates, because `HARD_LIMITS` is only applied in `load_config`, before the spec merges. Also:
`highlight: [true]` passes (`bool` is `int`), a list-valued node `id` raises `TypeError` in `set(ids)`, `limits: null`
in a config raises `AttributeError`, and YAML parse errors print several lines.
**Decision.** One validator in `specs.py` checks the type and range of every top-level key and every scene field after
the final merge. Unknown keys at any level are errors. No escape hatch: YAML comments carry notes.
- **Field tables live on the scene classes** (`scenes.py`, data only, no behaviour change): `Scene.COMMON_FIELDS =
  {type, seconds, caption, narration, narration_audio}`, plus `FIELDS: ClassVar[frozenset[str]]` per class:
  title {title, eyebrow, subtitle, url, rule}; type {text}; shot {source, title, fit, zoom, focus, focus_to, viewport,
  scale, click, scroll_y, wait_ms, full_page} (`title` is spoken by `read_screen`); bullets {items, title};
  stat {value, label, eyebrow}; code {code, highlight, title}; diagram {nodes, edges, flash, title, node_size}.
  `DiagramScene.NODE_FIELDS = {id, label, x, y, vx, vy, at, kind, sub}`, `EDGE_FIELDS = {from, to, label, at, color,
  bad, dashed, flow}`, `FLASH_FIELDS = {node, at, text, color}`. `ShotScene` builds `opts` from a module constant
  `CAPTURE_OPTS`. `specs.py` imports `SCENE_TYPES` (no import cycle: scenes → shots/textkit/theme) and keeps one checker
  per field in `FIELD_CHECKS`.
- **Top level:** theme str. themes is a mapping, and every entry goes through `get_theme`. formats is a list of 1-3
  unique names. fps is an int from 1 to 60. output_dir is a non-empty str. music is a str or {style, bpm 30-240,
  root ∈ `music.NOTE_OFFSETS`, scale ∈ `music.SCALES`, seed int 0..2^31, volume 0-2, snap bool}. meta is {title,
  description, tags, links, credits}. poster is {scene int < len(scenes), at}. limits is {max_seconds, max_scenes}.
  watermark and voiceover keep their current rules. List lengths are checked before iterating (alias-bomb safety).
- **Limits.** New `config.clamp_limits(raw) -> dict` (raises `ValueError`, wrapped as `ConfigError` or `SpecError`):
  the value must be a mapping of positive numbers (max_scenes an int), then `min(value, HARD_LIMITS[k])`. Called in
  `load_config` (replaces the inline loop) and again in `validate` after `merge`. So `max_seconds: 10000` is clamped to
  3600 and a 4000 s scene fails (clamping matches today's config behaviour; rejected: an error on any value above the
  cap, which would break configs that work today). `render()` checks again after narration stretch and beat snap, using
  `min(spec limit, HARD_LIMITS)` in case a caller skips `validate`.
- **Named bounds** (new unless noted): `specs.MAX_FRAMES = 216_000` (seconds × fps × formats; one hour at 60 fps, or two
  formats at 30 fps), checked in `validate`, again in `cli.cmd_render` once `--fps/--format` are known
  (`specs.check_frame_budget`), and per format in `render`. Existing `MAX_NODES = 40`, `MAX_EDGES = 80`, `MAX_FLASH = 40`.
  `MAX_ID_CHARS = 100` (a node id is a str or int, normalised to str). `NODE_SIZE = (16, 120)`. `MAX_CUE_SECONDS = 600`
  (replaces the literal 600). `MAX_CODE_CHARS = 8000` (fixes the dead `MAX_TEXT*4` branch). `shots.MAX_CAPTURE_PIXELS =
  36_000_000` (viewport w×h×scale²; a `full_page` capture is clipped to `MAX_CAPTURE_PIXELS // (w·scale²)` CSS px of
  height through Playwright's `clip`). Existing `shots.MAX_IMAGE_PIXELS`. `render.MAX_WATERMARK_PIXELS = 16_000_000`
  (names the literal).
- **Message grammar:** `<path> must be <expectation>`, `<path>: <problem>`. Paths look like `scenes[2].items[0]`,
  `scenes[1].nodes[3].x`, `scenes[0].edges[1].to`, `formats[0]`, `music.bpm`, `limits.max_seconds`. Kept phrases that
  existing tests match: "seconds must be", "unknown type", "missing title", "unknown node", "needs x and y",
  "limit is 2", "total length", "not settable from a spec". Examples:
  `scenes[0].titel: unknown field for a title scene; allowed: caption, eyebrow, narration, ...` ·
  `limits must be a mapping` · `formats[0] must be one of landscape, vertical, square` ·
  `fps must be an integer between 1 and 60` · `total length 4000s; the limit is 3600s (limits.max_seconds, capped at the
  absolute limit 3600)` · `scenes[0].viewport: 3840x3840 at scale 3 is 132710400 pixels; the limit is 36000000
  (MAX_CAPTURE_PIXELS)` · `estimated 432000 frames (3600s x 60 fps x 2 formats); the limit is 216000 (MAX_FRAMES)` ·
  render: `after narration and beat snapping the video is 612.0s; the limit is 600s (limits.max_seconds)` ·
  `cannot parse spec x.yaml: <problem> at line 3, column 5` (one line).
- **Hook for issue #13** (do not implement hints): `specs.suggest(key: str, allowed: Iterable[str]) -> str | None`
  returns None. `_unknown_key(path, key, allowed, what)` appends `f" ({hint})"` when it returns text.
- `render.Timeline` wraps per-scene `CaptureError` as `CaptureError(f"scenes[{i}].source: {e}")`.
  `poster.scene` out of range becomes a `SpecError` (the render check stays as a fallback).
- **Strictness cost/benefit.** Cost: a 0.2.1 spec with a misspelled or extra key, a float seed, an unknown `meta` key or
  `highlight: [true]` now fails with exit 1. Benefit: today `narraton:` silently drops the voice and `titel:` silently
  renders an empty title, and SECURITY.md's "bounded" claim becomes true. Verified against the field tables, with no
  changes needed: `examples/cache-stampede-45s.yaml`, `templates/{system-design,project,code-tip}.yaml`,
  `templates/config.example.yaml`, `~/vidgen-render/shorts/2026-10-29-idempotency-keys.yaml`,
  `2026-10-29-yamlreel-intro.yaml` (uses `music.scale: yo`, `root: G`, a shot with `fit/zoom/focus`), and the owner's
  user config (`voiceover.voice`, `voice_server.url`).
  Every entry under `themes:` is validated whether or not a scene uses it, so an unused theme with a bad `extends` or colour is an error too.
- `config.py` also: a `voice_server` value must be url str / client_id non-empty str of at most 200 chars / timeout
  number 1-3600, else `ConfigError`.
- **Tests** (`tests/test_validation.py`):
  1. The four reproductions raise `SpecError` with the paths above. `main(["validate", f])` returns 1 and stderr is one
     `error:` line.
  2. Type-confusion matrix: for every top-level key and every field in the tables (generated from `FIELDS`), each of
     `None, True, "xyz", 0, -1, 1e12, [], {}, [{}], [[]]` either validates or raises `SpecError`, never anything else.
  3. Drift guard: an AST walk of each `SCENE_TYPES` class `__init__` (and `Scene.__init__`) collects literal keys read
     from `spec`/`s` (`.get("k")`, `["k"]`) plus `CAPTURE_OPTS`, and asserts they are a subset of FIELDS ∪ COMMON.
     Keys read from `n`/`e`/`f` in `DiagramScene.__init__` must be in the NODE/EDGE/FLASH sets. Keys `voice.screen_text`
     reads must be in the union. `REQUIRED[t] ⊆ FIELDS[t]`. Every table field has a `FIELD_CHECKS` entry.
  4. Every `examples/*.yaml` and every non-config `yamlreel/templates/*.yaml` passes `load_spec`. `config.example.yaml`
     passes `load_config(explicit=...)` plus `validate` of a one-scene spec. This replaces `test_every_template_validates`.
  5. Exact-path cases: `scenes[2].items[0]`, `scenes[1].nodes[3].x`, `scenes[0].edges[1].to`, `scenes[0].flash[0].node`,
     `meta.tags[2]`, `music.bpm`, `poster.scene`, `voiceover.gap`, `watermark.size`, `formats[1]` (duplicate).
  6. Limits after merge: spec `max_seconds: 10000` with a 4000 s scene fails, naming 3600. Render: narration
     (fake server) pushes past `max_seconds: 5` → `RenderError` naming `limits.max_seconds`. Snap pushes `tl.total`
     past the limit → same. With `MAX_FRAMES` monkeypatched small, validate, CLI `--format all --fps 60` and render
     each fail naming `MAX_FRAMES`.
  7. viewport [3840, 3840] scale 3 → error names `MAX_CAPTURE_PIXELS`. 41 nodes → `scenes[0].nodes`, limit 40.
     `limits: null` in a project config → `ConfigError`.
  8. Hook: a monkeypatched `specs.suggest` returning `"hint"` shows in the message, and the default message has no hint.
- **Manual acceptance:** `yamlreel validate` passes on both shorts files above.

## PR C: narration cache key and timing (`voice.py`, `render.py`)
**Problem 1 (cache).** Key = `sha256([text, voice, language])`, so a different server or URL reuses old clips.
VoiceStudio (`backend/services/mcp_bindings.resolve_voice`) uses the `X-OmniVoice-Client-Id` binding only when no
`profile_id` is passed, and the bound `default_engine` is not used by `generate_speech`. So `client_id` affects the voice
only when `voice` is unset.
**Decision.** `VoiceServer.identity() -> str` = `scheme://host:port/path`, with scheme and host lower-cased, IPv6 in
brackets, an explicit default port (80/443), and path `"/"` if empty. Userinfo, query and fragment are never included.
`__post_init__` turns a bad port (`u.port` raises `ValueError`) into `VoiceError`. `CACHE_KEY_VERSION = 2`.
`narration_cache_key(server, text, voice, language) = sha256(json.dumps({"v": 2, "server": identity, "client":
None if voice else client_id, "voice", "language", "text"}, sort_keys=True))[:20]`, file `narration-v2-<key>.wav`.
One-time effect: every existing clip is a cache miss once. Old files are never read again and are not auto-deleted.
Rejected: hashing the full URL (leaks credentials into a stable identifier, and query churn invalidates).
Known gap: an engine switch inside the same server at the same URL (the OmniVoice to CSM case) still reuses clips.
For 0.3.0, consider `serverInfo` or a trusted `voice_server.cache_tag`. Until then, D documents the `rm`.
**Problem 2 (timing), reproduced by reading the code.** `Timeline.__init__` snaps copies (`round(s/beat)*beat`, which
can round down below the speech), while `voice.build_track` walks the unsnapped `spec["scenes"][i]["seconds"]`.
Two 1.1 s scenes at 72 bpm (beat 0.8333): the visual starts are 0 and 0.833, and narration is placed at 0.3 and 1.4.
**Decision (minimal).**
- `voice.NARRATION_LEAD = 0.3`. `voice.min_scene_seconds(samples, gap) = len/SR + max(gap, NARRATION_LEAD)`. The speech
  then always ends inside its scene, which is the same as today for the default gap 0.5. `prepare_narration` stretches
  using this helper.
- `render.plan_durations(spec, floors: dict[int, float] | None = None) -> list[float]` is the one place durations are
  decided. Snap on: `d = max(beat, round(s/beat)*beat)`, and if `d < floor` then `d = ceil(floor/beat - 1e-9)*beat`, so
  silent scenes keep today's nearest rounding (`test_beat_snap_rounds_durations` unchanged) and narrated scenes round up.
  Snap off: `max(s, floor)`.
- `Timeline(..., durations: list[float] | None = None)` sets each scene's `seconds` from `durations`, and
  `plan_durations(spec)` when the list is None. `render()`: clips → floors → `durations = plan_durations(...)` → limit
  and frame checks on `sum(durations)` → `Timeline(durations=...)` → `build_track(clips, tl.starts, tl.total)`.
  The new `build_track(clips, starts, total, lead=NARRATION_LEAD)` has `render.py` as its only caller.
- The 0.3.0 seam: `plan_durations` becomes the compiled integer-frame timeline. Frame quantisation (at most one
  frame of drift) is deferred to it.
- **Tests** (`tests/test_narration.py`; move `_tone_wav_bytes`/`_fake_voice_server` to `tests/fakes.py`, add a
  constant-amplitude `dc=` option so sample 0 is non-zero):
  1. `plan_durations` for the reproduction: no floors → [0.833, 0.833]. floors {1: 1.0} → second scene 1.667.
  2. Deterministic render, parametrised over snap on and off (bpm 72): two 1.1 s narrated scenes, fake server returns
     a 0.4 s DC clip, fps 5, scale 0.1, music none. Spy on `Timeline` and `voice_mod.write_wav`. For each scene i, the
     first non-zero narration sample is `round((tl.starts[i] + lead) * SR)` ± 1, the duration is at least clip + gap,
     and the clip ends at or before the next start. This fails on main when snap is on.
  3. Key: identities differ across host, port and path, and match across userinfo and query. The identity string
     contains neither `user`, `secret` nor `token`. `client_id` changes the key only when `voice` is None.
     Monkeypatching `CACHE_KEY_VERSION` changes the key. Two renders against two fake servers → 2 synth calls
     (1 on main). `test_narration_is_cached_between_renders` stays at 1.

## PR D: docs and Dependabot (`README.md`, `SECURITY.md`, `templates/config.example.yaml`, `.github/dependabot.yml`)
- README line 180 "on your own machine" becomes "on a server you choose: local (e.g. VoiceStudio) or remote".
  Line 227 Privacy: "narration text goes to the configured server; a remote URL sends it to that host". Line 294:
  rendering is local, and narration is local only if your voice server is.
- Caching (README line 229 and troubleshooting line 271): the cache is `<output folder>/.cache/narration-v2-*.wav`, keyed
  by server address (no credentials), voice, language and text. It is never pruned. Clear it with
  `rm <out>/.cache/narration-*.wav`. Upgrading to 0.2.2 re-voices once. An engine switch on the same server needs this `rm`.
- SECURITY.md: replace line 6 with the PR A policy (public unicast only by default; with `--allow-private-hosts`:
  loopback, RFC 1918, 100.64/10, ULA; never the always-deny table; numeric-host forms refused; redirects checked by the
  proxy). Line 12: privacy depends on the server you configure. Line 13: list the absolute limits (3600 s, 1000 scenes,
  `MAX_FRAMES`, capture pixels) and say they hold whatever a spec or config sets. Note that unknown spec keys are errors.
- config.example: next to `limits`, add "capped at 3600 s / 1000 scenes".
- dependabot.yml: add `- package-ecosystem: pip`, `directory: /`, `schedule: {interval: weekly}`.
- Acceptance: every command and path in the docs matches the shipped code. The Dependabot config parses (GitHub shows
  no config error after merge).

## Release PR (after A to D)
Bump `yamlreel/__init__.py` to `0.2.2` (`test_cli_version...` reads `__version__`). The GitHub release notes cover the
new address policy, strict keys (with an example error), and the one-time re-voicing.

## Risks, failure modes, rollback
- A: Tailscale or other CGNAT dev servers now need the flag (intended, documented). A split-horizon name that resolves
  to both public and private addresses is refused without the flag. NAT64-only networks keep working for public targets.
- B: user specs with typos break on upgrade. Mitigation: the exact path and allowed list in the error. The clamp is
  silent for configs (as before). The error path names the merged key, not which layer set it.
- C: a slower first render after upgrade (expected). Narrated snapped scenes can grow by up to one beat.
  The `build_track` signature changes (internal).
- Observability: every refusal names the host and address, every validation error the path, every limit error the
  constant or key.
- Rollback: each PR is one `git revert`. Reverting C makes v1 keys live again, which is harmless. If 0.2.2 regresses,
  yank it on PyPI and ship 0.2.3 with the revert. A must not be reverted without a replacement (P1).

## Builder checklist (one commit per line, each reviewable)
- [ ] A1 `safety.py`: tables, `_resolve` seam, numeric-host rule, unwrap, `classify`, `pinned_ips` on top; messages.
- [ ] A2 `tests/test_network_guard.py` (all the cases above, plus the proxy redirect and CONNECT cases).
- [ ] B1 `scenes.py`: field sets and `CAPTURE_OPTS` (no behaviour change), plus the drift-guard test.
- [ ] B2 `config.py`: `clamp_limits`, `limits` and `voice_server` type checks.
- [ ] B3 `specs.py`: path-aware checkers, unknown-key errors and the `suggest` hook, clamp after merge, frame budget,
      one-line YAML errors.
- [ ] B4 `shots.py` capture-pixel bound and full-page clip. `render.py`: post-snap total/frame checks, scene-path
      prefix, watermark constant. `cli.py`: frame budget after the flags.
- [ ] B5 `tests/test_validation.py`, examples/templates test, manual validate of both shorts files.
- [ ] C1 `voice.py`: `identity`, `narration_cache_key`, `NARRATION_LEAD`, `min_scene_seconds`, `build_track(starts)`.
- [ ] C2 `render.py`: `plan_durations`, `Timeline(durations=)`, wiring. Then `tests/fakes.py` and `tests/test_narration.py`.
- [ ] D1 README and SECURITY and config.example. D2 dependabot `pip`.
- [ ] R `__version__ = "0.2.2"`. Full `pytest -q` on 3.10 and 3.12. Render both shorts with voice once.
