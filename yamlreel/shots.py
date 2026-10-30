"""Get a still image for a `shot` scene: a local image, or a screenshot of a URL."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

from .pinproxy import PinningProxy
from .safety import UnsafeInput, check_http_url, file_url_to_path, resolve_local

MAX_IMAGE_PIXELS = 64_000_000  # refuse decompression bombs
MAX_CAPTURE_PIXELS = 36_000_000  # viewport width x height x scale^2 of one capture
DEFAULT_VIEWPORT = (1920, 1080)
DEFAULT_SCALE = 2


def capture_size_problem(vw: int, vh: int, scale: float) -> str | None:
    """Why a capture of this size is refused, or None."""
    pixels = vw * vh * scale * scale
    if pixels > MAX_CAPTURE_PIXELS:
        return (f"{vw}x{vh} at scale {scale:g} is {int(pixels)} pixels; "
                f"the limit is {MAX_CAPTURE_PIXELS} (MAX_CAPTURE_PIXELS)")
    return None


class CaptureError(RuntimeError):
    pass


def is_url(source: str) -> bool:
    return source.startswith(("http://", "https://", "file://"))


def _open_image(p: Path) -> Image.Image:
    try:
        with Image.open(p) as im:
            if im.width * im.height > MAX_IMAGE_PIXELS:
                raise CaptureError(f"{p.name}: image is {im.width}x{im.height}; the limit is {MAX_IMAGE_PIXELS} pixels")
            return im.convert("RGB")
    except (OSError, Image.DecompressionBombError) as e:
        raise CaptureError(f"cannot read image {p.name}: {e}") from e


def load_shot(source: str, opts: dict, workdir: Path, base_dir: Path, roots: list[Path],
              allow_private: bool = False, chromium: str | None = None) -> Image.Image:
    try:
        if not is_url(source):
            p = resolve_local(source, base_dir, roots)
            if not p.is_file():
                raise CaptureError(f"image not found: {p}")
            return _open_image(p)

        if source.startswith("file://"):
            local = resolve_local(file_url_to_path(source), base_dir, roots)
            if not local.is_file():
                raise CaptureError(f"page not found: {local}")
            source = local.as_uri()
            stamp = local.stat().st_mtime_ns  # recapture when the page changes
        else:
            check_http_url(source, allow_private)
            stamp = 0
    except UnsafeInput as e:
        raise CaptureError(str(e)) from e

    key = hashlib.sha1(json.dumps([source, opts, stamp], sort_keys=True, default=str).encode()).hexdigest()[:16]
    cache = workdir / "shots" / f"{key}.png"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        _capture(source, opts, cache, roots, allow_private, chromium)
    return _open_image(cache)


def full_page_clip_height(vw: int, scale: float, page_height: float) -> int:
    """CSS pixels of a full-page capture to keep, so that width x height x scale^2 stays within MAX_CAPTURE_PIXELS."""
    most = max(1, int(MAX_CAPTURE_PIXELS // (vw * scale * scale)))
    return max(1, min(int(page_height), most))


# Chromium flags: without the WebRTC policy, STUN/ICE UDP traffic would go around the pinning proxy. Headless
# shell reads the first spelling, full Chrome/Chromium the second.
CHROMIUM_ARGS = ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                 "--webrtc-ip-handling-policy=disable_non_proxied_udp"]


def _clean(text: str, limit: int = 300) -> str:
    """Printable characters only, bounded: anything shown to the user that a remote party could influence."""
    return "".join(c if c.isprintable() else " " for c in text)[:limit]


def _refusal_error(url: str, refusals: list[tuple[str, str]]) -> CaptureError:
    host, reason = refusals[0]  # the reason comes only from the proxy's own record, never from a response
    return CaptureError(f"cannot capture {_clean(url)}: a request to {_clean(host, 100)} was refused by the "
                        f"network policy: {_clean(reason)}")


def _request_allowed(url: str, roots: list[Path], allow_private: bool) -> bool:
    scheme = urlparse(url).scheme
    if scheme in ("data", "blob", "about"):
        return True
    try:
        if scheme == "file":
            resolve_local(file_url_to_path(url), Path("/"), roots)
        elif scheme in ("http", "https"):
            check_http_url(url, allow_private)
        else:
            return False
    except UnsafeInput:
        return False
    return True


def _capture(url: str, opts: dict, dest: Path, roots: list[Path], allow_private: bool, chromium: str | None) -> None:
    vw, vh = opts.get("viewport", list(DEFAULT_VIEWPORT))
    scale = opts.get("scale", DEFAULT_SCALE)
    problem = capture_size_problem(vw, vh, scale)  # a spec is checked on load; this covers callers that skip that
    if problem:
        raise CaptureError(f"cannot capture {url}: {problem}")
    try:
        from playwright.sync_api import Error as PwError
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover
        raise CaptureError("capturing a URL needs playwright: pip install playwright") from e

    blocked: list[str] = []

    def guard(route) -> None:  # every request the page makes, including redirects and sub-resources
        if _request_allowed(route.request.url, roots, allow_private):
            route.continue_()
        else:
            blocked.append(route.request.url)
            route.abort()

    proxy = PinningProxy(allow_private)

    def check_refused() -> None:
        """The proxy sees every http(s) request, CONNECT tunnels included, so its own record is the only evidence."""
        refusals = proxy.refusals
        if refusals:
            raise _refusal_error(url, refusals)

    try:
        with sync_playwright() as p, proxy:
            # Everything http(s) goes through the pinning proxy ("<-loopback>" stops Chromium bypassing it for
            # localhost names), so the address checked is the address connected to. file:// stays local.
            browser = p.chromium.launch(proxy={"server": proxy.url, "bypass": "<-loopback>"}, args=CHROMIUM_ARGS,
                                        **({"executable_path": chromium} if chromium else {}))
            try:
                ctx = browser.new_context(viewport={"width": vw, "height": vh}, device_scale_factor=scale,
                                          accept_downloads=False, service_workers="block")
                ctx.route("**/*", guard)
                page = ctx.new_page()
                try:
                    page.goto(url, wait_until="networkidle", timeout=30000)
                except PwError:
                    check_refused()
                    if blocked and blocked[0] == url:
                        raise
                    page.goto(url, wait_until="load", timeout=30000)
                check_refused()
                for sel in opts.get("click", []):
                    page.click(sel, timeout=5000)
                if "scroll_y" in opts:
                    page.evaluate("y => window.scrollTo(0, y)", int(opts["scroll_y"]))
                page.wait_for_timeout(int(opts.get("wait_ms", 800)))
                check_refused()  # also catches a sub-resource refused while the page settled
                if opts.get("full_page", False):
                    # a very tall page would exceed MAX_CAPTURE_PIXELS: keep only the top of it
                    height = page.evaluate("Math.max(document.documentElement.scrollHeight, "
                                           "document.body ? document.body.scrollHeight : 0)")
                    clip = {"x": 0, "y": 0, "width": vw, "height": full_page_clip_height(vw, scale, height or vh)}
                    page.screenshot(path=str(dest), full_page=True, clip=clip)
                else:
                    page.screenshot(path=str(dest))
                try:
                    check_refused()  # a request refused while the screenshot was taken
                except CaptureError:
                    dest.unlink(missing_ok=True)
                    raise
            finally:
                browser.close()
    except PwError as e:
        check_refused()
        extra = f" (blocked request to {blocked[0]})" if blocked else ""
        raise CaptureError(f"could not capture {_clean(url)}{extra}: {e}") from e
