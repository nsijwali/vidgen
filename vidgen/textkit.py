"""Text measuring, wrapping, fitting and drawing helpers."""
from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw

from .theme import Color, font, has_cjk


@dataclass
class TextBlock:
    mask: Image.Image  # 'L' coverage mask
    width: int
    height: int
    size: int
    shrunk: bool


def wrap(text: str, fnt, max_w: float) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        tokens = list(para) if has_cjk(para) else para.split(" ")
        sep = "" if has_cjk(para) else " "
        cur = ""
        for tok in tokens:
            trial = tok if not cur else cur + sep + tok
            if fnt.getlength(trial) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = tok
        lines.append(cur)
    return lines


def _tracked_length(s: str, fnt, tracking: float) -> float:
    return sum(fnt.getlength(c) + tracking for c in s) - (tracking if s else 0)


def make_block(text: str, kind: str, size: float, max_w: float, max_h: float | None = None,
               min_size: float = 18, spacing: float = 1.28, align: str = "left",
               tracking: float = 0.0) -> TextBlock:
    """Wrap text to max_w and shrink until it also fits max_h. Never overflows silently:
    `shrunk` reports when the requested size could not be kept."""
    requested = size
    while True:
        fnt = font(kind, size, text)
        lines = wrap(text, fnt, max_w) if not tracking else [text]
        lh = int(size * spacing)
        h = lh * len(lines)
        widest = max((_tracked_length(l, fnt, tracking) if tracking else fnt.getlength(l)) for l in lines)
        fits = (max_h is None or h <= max_h) and widest <= max_w + 1
        if fits or size <= min_size:
            break
        size -= 2
    w = int(widest) + 4
    mask = Image.new("L", (max(w, 1), max(h + int(size * 0.3), 1)), 0)
    d = ImageDraw.Draw(mask)
    for i, line in enumerate(lines):
        lw = _tracked_length(line, fnt, tracking) if tracking else fnt.getlength(line)
        x = {"left": 0, "center": (w - lw) / 2, "right": w - lw}[align]
        y = i * lh
        if tracking:
            for ch in line:
                d.text((x, y), ch, font=fnt, fill=255)
                x += fnt.getlength(ch) + tracking
        else:
            d.text((x, y), line, font=fnt, fill=255)
    return TextBlock(mask, w, h, int(size), size < requested - 1)


def blit(canvas: Image.Image, block: TextBlock, xy: tuple[float, float], color: Color,
         alpha: float = 1.0, anchor: str = "tl") -> None:
    x, y = xy
    if anchor[0] == "m":
        y -= block.height / 2
    elif anchor[0] == "b":
        y -= block.height
    if anchor[1] == "c":
        x -= block.width / 2
    elif anchor[1] == "r":
        x -= block.width
    # anchor[1] == "l" (or "tl"/"ml" style) keeps x as the left edge
    if alpha <= 0:
        return
    mask = block.mask if alpha >= 1 else block.mask.point(lambda v: int(v * alpha))
    canvas.paste(color, (int(x), int(y)), mask)


def lerp_color(a: Color, b: Color, t: float) -> Color:
    t = max(0.0, min(1.0, t))
    return (int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t), int(a[2] + (b[2] - a[2]) * t))


def ease_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 3 * t * t - 2 * t * t * t
