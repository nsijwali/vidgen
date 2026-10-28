"""Timeline assembly and ffmpeg encoding."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from . import music as music_mod
from . import voice as voice_mod
from .safety import UnsafeInput, resolve_local
from .scenes import Ctx, Scene, build_scene, make_caption
from .specs import FORMATS
from .textkit import blit, ease_out, make_block
from .theme import font

FADE_IN = 0.25
YT_TITLE_MAX = 100
YT_DESC_MAX = 5000
SHORTS_MAX_SECONDS = 180


class RenderError(RuntimeError):
    pass


@dataclass
class Runtime:
    """What this run is allowed to read and reach (never taken from the spec)."""
    roots: list[Path] = field(default_factory=list)
    allow_private_hosts: bool = False
    chromium: str | None = None
    voice: "voice_mod.VoiceServer | None" = None  # where narration is synthesised (trusted setting)

    @classmethod
    def for_spec(cls, spec: dict[str, Any]) -> "Runtime":
        return cls(roots=[Path(spec.get("_dir", "."))])


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "video"


class Timeline:
    def __init__(self, spec: dict[str, Any], fmt: str, workdir: Path, scale: float = 1.0,
                 runtime: Runtime | None = None):
        W, H = FORMATS[fmt]
        W, H = int(W * scale), int(H * scale)
        self.fmt = fmt
        self.spec = spec
        rt = runtime or Runtime.for_spec(spec)
        self.rt = rt
        self.ctx = Ctx(W, H, spec["_theme_obj"], workdir, Path(spec.get("_dir", ".")), roots=list(rt.roots),
                       allow_private_hosts=rt.allow_private_hosts, chromium=rt.chromium)
        scenes_spec = [dict(s) for s in spec["scenes"]]
        music = spec["music"]
        if music.get("snap") and music.get("bpm"):
            beat = 60.0 / float(music["bpm"])
            for s in scenes_spec:
                s["seconds"] = max(beat, round(s["seconds"] / beat) * beat)
        self.scenes: list[Scene] = [build_scene(s, self.ctx) for s in scenes_spec]
        self.starts: list[float] = []
        t = 0.0
        for sc in self.scenes:
            self.starts.append(t)
            t += sc.duration
        self.total = t
        self._caps = {i: make_caption(self.ctx, sc.caption) for i, sc in enumerate(self.scenes) if sc.caption}
        self._last: dict[int, Image.Image] = {}
        self._wm = self._make_watermark(spec.get("watermark"))

    def _make_watermark(self, wm: dict[str, Any] | None) -> tuple[Image.Image, Image.Image, tuple[int, int]] | None:
        """Pre-render the watermark once: (rgb, mask, top-left position)."""
        if not wm:
            return None
        c = self.ctx
        u = c.u
        if wm.get("image"):
            try:
                p = resolve_local(wm["image"], c.base_dir, c.roots)
            except UnsafeInput as e:
                raise RenderError(f"watermark image: {e}") from e
            if not p.is_file():
                raise RenderError(f"watermark image not found: {p}")
            try:
                with Image.open(p) as im:
                    if im.width * im.height > 16_000_000:
                        raise RenderError("watermark image is too large (16 megapixel limit)")
                    logo = im.convert("RGBA")
            except OSError as e:
                raise RenderError(f"cannot read watermark image: {e}") from e
            h = int(wm.get("size", 56) * u)
            logo = logo.resize((max(1, int(logo.width * h / logo.height)), h), Image.LANCZOS)
            rgb, mask = logo.convert("RGB"), logo.getchannel("A")
        else:
            blk = make_block(str(wm["text"]), c.body, wm.get("size", 30) * u * (1.1 if c.vertical else 1.0),
                             c.W * 0.5, tracking=1.5 * u)
            rgb = Image.new("RGB", (blk.width, blk.mask.height), c.theme.fg)
            mask = blk.mask
        mask = mask.point(lambda v: int(v * wm["opacity"]))
        pos = wm.get("position") or ("top-left" if c.vertical else "bottom-right")
        # vertical: the Shorts UI covers the right edge and the bottom, so default to the top-left
        inset = int(60 * u)
        x = inset if pos.endswith("left") else c.W - inset - rgb.width
        y = inset if pos.startswith("top") else c.H - int(48 * u) - rgb.height
        return rgb, mask, (x, y)

    @property
    def warnings(self) -> list[str]:
        return self.ctx.warnings

    def locate(self, t: float) -> tuple[int, float]:
        t = min(max(t, 0.0), self.total - 1e-6)
        for i in range(len(self.scenes) - 1, -1, -1):
            if t >= self.starts[i]:
                return i, t - self.starts[i]
        return 0, 0.0

    def _last_frame(self, i: int) -> Image.Image:
        if i not in self._last:
            sc = self.scenes[i]
            self._last[i] = self._scene_frame(i, max(0.0, sc.duration - 1 / 30))
        return self._last[i]

    def _scene_frame(self, i: int, lt: float) -> Image.Image:
        sc = self.scenes[i]
        img = sc.frame(lt)
        if i in self._caps:
            pill, mask = self._caps[i]
            a = ease_out((lt - 0.35) / 0.25)
            if a > 0:
                m = mask if a >= 1 else mask.point(lambda v: int(v * a))
                img.paste(pill, (int((self.ctx.W - pill.width) / 2), int(self.ctx.caption_y - pill.height / 2)), m)
        return img

    def frame_at(self, t: float) -> Image.Image:
        i, lt = self.locate(t)
        img = self._scene_frame(i, lt)
        if i > 0 and lt < FADE_IN:
            a = lt / FADE_IN
            img = Image.blend(self._last_frame(i - 1), img, a)
        if self._wm:  # constant on every frame, including transitions and the poster
            rgb, mask, xy = self._wm
            img.paste(rgb, xy, mask)
        return img

    def poster_time(self) -> float:
        p = self.spec.get("poster", {}) or {}
        idx = int(p.get("scene", 0))
        if not 0 <= idx < len(self.scenes):
            raise RenderError(f"poster.scene {idx} out of range")
        sc = self.scenes[idx]
        at = float(p.get("at", sc.duration * 0.92))
        return self.starts[idx] + min(at, sc.duration - 0.05)


def contact_sheet(tl: Timeline, path: Path, cols: int = 4, tile_w: int = 480) -> Path:
    stills: list[tuple[str, Image.Image]] = []
    for i, sc in enumerate(tl.scenes):
        for frac in (0.35, 0.92):
            t = tl.starts[i] + sc.duration * frac
            im = tl.frame_at(t)
            stills.append((f"{i}:{sc.spec['type']} {t:.1f}s", im.resize((tile_w, int(tile_w * im.height / im.width)))))
    th = tile_h = stills[0][1].height
    rows = (len(stills) + cols - 1) // cols
    label_h = 24
    sheet = Image.new("RGB", (cols * tile_w, rows * (tile_h + label_h)), (16, 16, 16))
    d = ImageDraw.Draw(sheet)
    f = font("sans", 15)
    for n, (label, im) in enumerate(stills):
        x, y = (n % cols) * tile_w, (n // cols) * (tile_h + label_h)
        sheet.paste(im, (x, y + label_h))
        d.text((x + 6, y + 4), label, font=f, fill=(220, 220, 220))
    sheet.save(path)
    return path


def _prepare_audio(spec: dict[str, Any], total: float, workdir: Path, override: str | None,
                   rt: Runtime) -> Path | None:
    m = dict(spec["music"])
    style = override or m["style"]
    if style == "none":
        return None
    if style == "koto":
        samples = music_mod.compose(total, bpm=float(m.get("bpm", 72)), root=str(m.get("root", "D")),
                                    scale=str(m.get("scale", "hirajoshi")), seed=int(m.get("seed", 7)))
        return music_mod.write_wav(workdir / "music.wav", samples)
    try:
        p = resolve_local(style, Path(spec.get("_dir", ".")), rt.roots)
    except UnsafeInput as e:
        raise RenderError(f"music file: {e}") from e
    if not p.is_file():
        raise RenderError(f"music file not found: {p}")
    return p


def share_copy(spec: dict[str, Any], total: float, fmt_list: list[str]) -> tuple[str, list[str]]:
    meta = spec.get("meta", {})
    warns: list[str] = []
    title = str(meta.get("title", "")).strip()
    if not title:
        warns.append("meta.title is empty")
    if len(title) > YT_TITLE_MAX:
        warns.append(f"title is {len(title)} chars; YouTube allows {YT_TITLE_MAX}")
    tags = [str(t).lstrip("#") for t in meta.get("tags", [])]
    if "vertical" in fmt_list and total <= 60 and "Shorts" not in tags:
        tags = ["Shorts", *tags]
    lines = [str(meta.get("description", "")).strip()]
    for link in meta.get("links", []):
        lines.append(str(link))
    for credit in meta.get("credits", []):
        lines.append(str(credit))
    lines.append(" ".join("#" + t for t in tags))
    desc = "\n".join(l for l in lines if l)
    if len(desc) > YT_DESC_MAX:
        warns.append(f"description is {len(desc)} chars; YouTube allows {YT_DESC_MAX}")
    if "vertical" in fmt_list and total > SHORTS_MAX_SECONDS:
        warns.append(f"vertical video is {total:.0f}s; YouTube Shorts allow up to {SHORTS_MAX_SECONDS}s, "
                     "beyond that it is a normal video")
    return f"Title: {title}\n\nDescription:\n{desc}\n", warns


AUDIO_FORMATS = "wav,mp3,ogg,flac,mov,aac,matroska,webm"


def render(spec: dict[str, Any], fmt: str, out_dir: Path, fps: int = 30, scale: float = 1.0,
           music_override: str | None = None, contact: bool = True,
           runtime: Runtime | None = None) -> dict[str, Any]:
    if shutil.which("ffmpeg") is None:
        raise RenderError("ffmpeg not found on PATH")
    if not 1 <= fps <= 60:
        raise RenderError("fps must be between 1 and 60")
    rt = runtime or Runtime.for_spec(spec)
    spec = {**spec, "scenes": [dict(sc) for sc in spec["scenes"]]}  # narration stretches scene lengths: work on a copy
    out = out_dir / fmt
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vidgen-") as td:
        work = Path(td)
        cache = out_dir / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        try:
            clips = voice_mod.prepare_narration(spec, rt.voice, cache, lambda p: resolve_local(p, Path(spec.get("_dir", ".")), rt.roots))
        except (voice_mod.VoiceError, UnsafeInput) as e:
            raise RenderError(f"voiceover: {e}") from e
        limit = spec["limits"]["max_seconds"]
        if sum(sc["seconds"] for sc in spec["scenes"]) > limit:
            raise RenderError(f"with narration the video is longer than limits.max_seconds ({limit}s)")
        tl = Timeline(spec, fmt, cache, scale, rt)
        total = tl.total
        narration = voice_mod.write_wav(work / "narration.wav", voice_mod.build_track(spec, clips, total)) if clips else None
        audio = _prepare_audio(spec, total, work, music_override, rt)
        poster = tl.frame_at(tl.poster_time())
        poster.convert("RGB").save(out / "poster.jpg", quality=92)

        W, H = tl.ctx.W, tl.ctx.H
        cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{W}x{H}", "-r", str(fps), "-i", "-"]
        # Audio inputs, in order: music (optional), narration (optional). Input 0 is the video.
        inputs: list[Path] = [a for a in (audio, narration) if a]
        for a in inputs:
            # ffmpeg sniffs the format from content, so a file named .mp3 could really be a concat/playlist
            # script that pulls in other files. Only accept real audio containers, local files only.
            cmd += ["-protocol_whitelist", "file", "-format_whitelist", AUDIO_FORMATS, "-i", str(a)]
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p"]
        if inputs:
            vol = float(spec["music"].get("volume", 0.9))
            fade_out = max(0.0, total - 2.0)
            music_f = (f"atrim=0:{total:.3f},asetpts=PTS-STARTPTS,volume={{vol}},"
                       f"afade=t=in:st=0:d=0.8,afade=t=out:st={fade_out:.3f}:d=2.0")
            if audio and narration:
                level = float(spec["voiceover"].get("music_level", 0.35))  # music sits under the voice
                graph = (f"[1:a]{music_f.format(vol=vol * level)}[m];"
                         f"[2:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS[n];"
                         "[m][n]amix=inputs=2:duration=longest:normalize=0[a]")
                cmd += ["-filter_complex", graph, "-map", "0:v", "-map", "[a]"]
            elif audio:
                cmd += ["-af", music_f.format(vol=vol)]
            else:
                cmd += ["-af", f"atrim=0:{total:.3f},asetpts=PTS-STARTPTS,afade=t=out:st={fade_out:.3f}:d=1.0"]
            cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        cmd += ["-movflags", "+faststart", "-t", f"{total:.3f}", str(out / "video.mp4")]
        errlog = open(work / "ffmpeg.log", "wb")
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errlog)
        try:
            n_frames = int(round(total * fps))
            for n in range(n_frames):
                img = poster if n == 0 else tl.frame_at(n / fps)  # poster doubles as frame 0 (thumbnail)
                proc.stdin.write(img.convert("RGB").tobytes())
            proc.stdin.close()
        except BrokenPipeError:
            pass
        rc = proc.wait()
        errlog.close()
        if rc != 0:
            raise RenderError("ffmpeg failed:\n" + (work / "ffmpeg.log").read_text(errors="replace")[-1500:])
        sheet = contact_sheet(tl, out / "contact.png") if contact else None
    return {"video": out / "video.mp4", "poster": out / "poster.jpg", "contact": sheet,
            "seconds": total, "warnings": tl.warnings, "size": (W, H)}
