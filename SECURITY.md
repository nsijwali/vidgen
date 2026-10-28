# Security

vidgen turns a YAML spec into a video. A spec names files and URLs, so it is treated as untrusted input:

- Local images, pages, music and watermark logos must sit inside the spec's folder, the config folders, or a folder you allow with `--allow-path` (or `allow_paths` in your *user* config). Symlinks cannot escape.
- URL captures accept `http(s)` and in-folder `file://` only. Every request the captured page makes, including redirects and sub-resources, goes through the same check. Link-local and cloud-metadata addresses are always refused. Loopback and private-network hosts need `--allow-private-hosts`.
- `allow_paths`, `allow_private_hosts`, `chromium_path` and `font_dir` cannot be set by a spec or a project config, only by the user config or the command line.
- Specs, configs, text, scene counts, durations, image sizes and viewport sizes are bounded.

Not covered: DNS rebinding between the host check and the browser's own lookup, and a malicious `chromium_path` or font file you chose yourself. Render specs you do not trust inside a container or VM with no access to internal networks.

Report vulnerabilities privately to the repository owner.
