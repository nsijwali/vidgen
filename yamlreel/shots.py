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


# Chromium flags: without the WebRTC policy, STUN/ICE UDP traffic would go around the pinning proxy.
CHROMIUM_ARGS = ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
REFUSED_HEADER = "x-yamlreel-refused"  # set by the pinning proxy on its 403 refusals (lower-case: Playwright's form)


def is_proxy_refusal(status: int, headers: dict[str, str]) -> bool:
    """True only for the pinning proxy's own refusal. A plain 403 from a site is just a page."""
    return status == 403 and headers.get(REFUSED_HEADER) == "1"


def _refusal_error(url: str, response) -> CaptureError:
    try:
        reason = response.text().strip()[:300]
    except Exception:  # noqa: BLE001 - the body is only a hint; the refusal itself is what matters
        reason = ""
    detail = f": {reason}" if reason else ""
    return CaptureError(f"cannot capture {url}: a request to {response.url} was refused by the network policy{detail}")


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
    refused: list = []  # the pinning proxy's refusals: a hop, redirect or sub-resource the policy denied

    def on_response(response) -> None:
        if is_proxy_refusal(response.status, response.headers):
            refused.append(response)

    def check_refused() -> None:
        if refused:
            raise _refusal_error(url, refused[0])

    def guard(route) -> None:  # every request the page makes, including redirects and sub-resources
        if _request_allowed(route.request.url, roots, allow_private):
            route.continue_()
        else:
            blocked.append(route.request.url)
            route.abort()

    try:
        with sync_playwright() as p, PinningProxy(allow_private) as proxy:
            # Everything http(s) goes through the pinning proxy ("<-loopback>" stops Chromium bypassing it for
            # localhost names), so the address checked is the address connected to. file:// stays local.
            browser = p.chromium.launch(proxy={"server": proxy.url, "bypass": "<-loopback>"}, args=CHROMIUM_ARGS,
                                        **({"executable_path": chromium} if chromium else {}))
            try:
                ctx = browser.new_context(viewport={"width": vw, "height": vh}, device_scale_factor=scale,
                                          accept_downloads=False, service_workers="block")
                ctx.route("**/*", guard)
                ctx.on("response", on_response)
                page = ctx.new_page()
                try:
                    page.goto(url, wait_until="networkidle", timeout=30000)
                except PwError:
                    if blocked and blocked[0] == url:
                        raise
                    check_refused()
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
            finally:
                browser.close()
    except PwError as e:
        extra = f" (blocked request to {blocked[0]})" if blocked else ""
        raise CaptureError(f"could not capture {url}{extra}: {e}") from e
