# Security

yamlreel turns a YAML spec into a video. A spec names files and URLs, so it is treated as untrusted input:

- Local images, pages, music and watermark logos must sit inside the spec's folder, the config folders, or a folder you allow with `--allow-path` (or `allow_paths` in your *user* config). Symlinks cannot escape.
- URL captures accept `http(s)` and in-folder `file://` only. Every request the captured page makes, including redirects and sub-resources, goes through the same check. Link-local and cloud-metadata addresses are always refused. Loopback and private-network hosts need `--allow-private-hosts`.
- `allow_paths`, `allow_private_hosts`, `chromium_path`, `font_dir` and `voice_server` cannot be set by a spec or a project config, only by the user config or the command line.
- The user config, at its current or pre-rename location (see the README), is the only file that can set trusted keys.
- URL captures run through a small local pinning proxy: each host name is resolved once, every address is checked, and the browser is connected to that exact address, so DNS rebinding cannot swap in an internal address after the check.
- Music files are only read as real audio containers (wav, mp3, ogg, flac, mp4/m4a, aac, mkv/webm), never as playlists or scripts that point at other files.
- Without `--out`, `output_dir` from a spec or project config must stay inside the spec folder, the current folder, or an `--allow-path` folder.
- Voiceover: the text-to-speech server address is a user-config / command-line setting only, so a spec cannot make yamlreel send its text elsewhere. Server replies are size-capped and must be valid PCM WAV before they are used.
- Specs, configs, text, scene counts, durations, image sizes and viewport sizes are bounded.

Not covered: a malicious `chromium_path`, ffmpeg binary or font file you chose yourself (yamlreel runs the `ffmpeg` on your PATH, else the one from the `imageio-ffmpeg` package or its `IMAGEIO_FFMPEG_EXE` variable; none of these can come from a spec), and a spec kept in a very broad folder (your home directory, `/tmp`), because the spec's own folder is readable. Render specs you do not trust inside a container or VM with no access to internal networks.

Report vulnerabilities privately through GitHub's "Report a vulnerability" button on the Security tab (private vulnerability reporting), or to the repository owner.
