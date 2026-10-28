# vidgen

Turn a short YAML spec into a finished video: MP4, poster frame, a contact sheet to review it, and ready-to-paste title, description and hashtags. It is not tied to any one app. Use it for a project launch, a system-design explainer, a code tip or a feature demo, as a YouTube video, a YouTube Short or a LinkedIn clip.

```bash
pip install -e .                      # pillow, numpy, pyyaml
pip install playwright && playwright install chromium   # only if a scene captures a URL
vidgen init system-design cache.yaml  # or: project, code-tip
vidgen render cache.yaml --preview    # half size, 12 fps: check layout and timing fast
vidgen render cache.yaml --out out    # full quality, every format in the spec
vidgen render page.yaml --allow-private-hosts   # needed to capture localhost or LAN URLs
```

Output, per format (`landscape` 1920x1080, `vertical` 1080x1920, `square` 1080x1080):

```
out/vertical/video.mp4     H.264 + AAC, 30 fps, frame 0 is the poster so the thumbnail is never blank
out/vertical/poster.jpg    the settled hook frame
out/vertical/contact.png   two stills per scene, labelled: look at this before you publish
out/share-copy.txt         title, description, hashtags (adds #Shorts for vertical clips up to 60s; length is unlimited, only a vertical video over 3 minutes gets a warning)
```

## Spec

```yaml
meta: {title, description, tags: [], links: [], credits: []}
theme: sumi | washi | midnight
formats: [vertical, landscape]
music: {style: koto | none | path/to.mp3, bpm: 72, root: D, scale: hirajoshi, seed: 7, snap: false, volume: 0.9}
watermark: "Your Name"      # or {text|image: logo.png, position: top-left|top-right|bottom-left|bottom-right, opacity: 0.75, size: 30}
poster: {scene: 0, at: 2.6}      # optional; default is the first scene, just before it ends
scenes: [...]
```

Every scene takes `seconds` and an optional `caption` (a lower-third that fades in).

| type | what it shows | key fields |
| --- | --- | --- |
| `title` | eyebrow, title, subtitle, URL; eases in line by line | `title`, `eyebrow`, `subtitle`, `url` |
| `type` | one line typed out with a cursor: the hook | `text` |
| `shot` | screenshot of a URL or an image, slow camera move | `source`, `zoom: [from, to]`, `focus: [x, y]`, `focus_to`, `fit: cover\|card`, `click: [selectors]`, `scroll_y`, `wait_ms`, `viewport` |
| `diagram` | boxes and arrows that reveal in order, packets flow along arrows, nodes flash state | `nodes` (`id, label, kind: client\|service\|cache\|db, x, y, vx, vy`), `edges` (`from, to, label, bad, dashed, at`), `flash` (`node, at, text`), `title` |
| `bullets` | points that appear one at a time | `items`, `title` |
| `code` | code typed on screen, lines highlighted | `code`, `highlight: [line numbers]`, `title` |
| `stat` | one big number or phrase | `value`, `label`, `eyebrow` |

Coordinates `x, y` are fractions of the frame. In vertical frames, `vx, vy` override them so one diagram can be laid out for both shapes.

## Configuration

Defaults for everything live in a config file, so a spec only has to say what is different for that video. Anyone can adapt vidgen to their own name, colours and music without touching code.

```bash
vidgen config init            # writes ./vidgen.config.yaml (this project)
vidgen config init --user     # writes ~/.config/vidgen/config.yaml (every project)
vidgen config show            # the effective, merged settings and where each layer came from
```

Precedence, lowest to highest: built-in defaults, user config, project config, the spec, command-line flags. Nested settings merge (set only `music.bpm` and the rest stay), and `meta.tags`, `links` and `credits` are added together across layers.

What you can set: `theme`, your own `themes` (colours, `extends`, serif or sans, frame), `formats`, `fps`, `output_dir`, `watermark` (text or logo), `music` (style, bpm, root, scale, seed, volume, snap), `meta` defaults and `limits`. See `vidgen/templates/config.example.yaml`.

A few settings widen what a spec can touch, so they are only read from the **user** config or the command line: `allow_paths`, `allow_private_hosts`, `chromium_path`, `font_dir`. A project config or a spec you downloaded cannot set them. See [SECURITY.md](SECURITY.md).

## Voiceover

Add `narration` to any scene and vidgen speaks it, mixes it over the music (which dips under the voice), and makes the
scene as long as the speech needs.

```yaml
voiceover: {language: en}          # optional: voice profile, language, gap, music_level
scenes:
  - {type: title, title: Cache stampede, narration: "When a hot key expires, every request misses at once."}
```

Speech is made by a local text-to-speech server you run, such as [VoiceStudio](https://github.com/debpalash/VoiceStudio)
(open source, runs on your machine). Start it, then:

```bash
vidgen render spec.yaml --voice-url http://127.0.0.1:3900/mcp/
# or set it once in ~/.config/vidgen/config.yaml:  voice_server: {url: http://127.0.0.1:3900/mcp/}
```

vidgen talks MCP "Streamable HTTP" and calls the server's `generate_speech` tool, which must return audio inline (VoiceStudio's
default). Audio is cached per text and voice, so re-renders do not re-speak. Narration text is sent only to the server you
configured; the server address can be set only in the user config or on the command line, never by a spec. Prefer recording
your own? Use `narration_audio: clip.wav` instead (PCM WAV, inside the spec folder or an allowed folder).
VoiceStudio is AGPL-3.0 and its models have their own licences; check them before commercial use.

## Notes

- **Personal details stay in config.** Nothing about any author is built in: put your watermark and default tags in `vidgen.config.yaml` (git-ignored by default).
- **Watermark**: on every frame and the poster. Default corner is bottom-right for landscape and top-left for vertical, because the Shorts buttons cover the right edge and the bottom. Use `image:` for a logo.
- **Music** is generated here (plucked strings in a Japanese scale, drone, reverb), so there is nothing to license or credit. `seed` changes the melody. `snap: true` rounds scene lengths to beats. Point `music.style` at an audio file to use your own track; then credit it in `meta.credits`.
- **Text never silently overflows**: it wraps, then shrinks, and `render` prints a warning when it had to.
- **Screenshots** are cached in `<out>/.cache`; delete it to recapture.
- **Captions sit clear of the Shorts UI** in vertical frames (content is centred above the lower third).
- Fonts: DejaVu (and Noto CJK for Japanese text) from the system; set `font_dir` in your user config (or `VIDGEN_FONT_DIR`) to use others. `chromium_path` (or `VIDGEN_CHROMIUM`) picks the browser for captures.
- Review before publishing: a 15-second video carries one idea, so check anything you simplified.

## Tests

```bash
pip install -e '.[dev]' && pytest
```
