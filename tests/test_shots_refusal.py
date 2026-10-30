"""Capture-side network guard (docs/specs/batch1-trust-the-input.md, follow-up PR): a refusal recorded by the pinning
proxy fails the capture (HTTP and HTTPS alike), and Chromium is launched so WebRTC cannot bypass the proxy.

Playwright and the proxy are replaced by fakes injected into `sys.modules` / `shots`, so nothing here needs Playwright
installed, a browser, or the network. Real Chromium behaviour is not exercised."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from yamlreel import shots
from yamlreel.shots import CaptureError

PRIVATE = ("10.0.0.5", "intranet.test resolves to a private or loopback address (10.0.0.5). "
                       "To capture a local dev server pass --allow-private-hosts")
DENIED = ("169.254.169.254", "metadata.test resolves to 169.254.169.254, a cloud-metadata, link-local or reserved "
                             "address; always refused")


class FakeProxy:
    """Stands in for PinningProxy: `refusals` is what the real proxy would have recorded."""
    url = "http://127.0.0.1:1"
    instance: "FakeProxy"

    def __init__(self, allow_private: bool) -> None:
        self.refusals: list[tuple[str, str]] = []
        self.closed = False
        FakeProxy.instance = self

    def record_refusal(self, host: str, reason: str) -> None:
        self.refusals.append((host, reason))

    @property
    def first_refusal(self):
        return self.refusals[0] if self.refusals else None

    @property
    def refusal_count(self) -> int:
        return len(self.refusals)

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class FakePlaywright:
    """A browser whose steps are scripted: each hook may refuse (via FakeProxy.instance) or raise."""

    def __init__(self, pw_error: type, goto=None, wait=None, shot=None, close=None) -> None:
        self.pw_error, self.goto_hook, self.wait_hook, self.shot_hook = pw_error, goto, wait, shot
        self.close_hook = close
        FakePlaywright.current = self
        self.launch_kwargs: dict = {}
        self.gotos = 0
        self.guard = None
        self.aborted: list[str] = []
        self.continued: list[str] = []
        self.screenshots = 0
        outer = self

        class Page:
            def goto(self, url, **kw):
                outer.gotos += 1
                if outer.goto_hook:
                    outer.goto_hook(outer.gotos)

            def click(self, *a, **kw): ...
            def evaluate(self, *a, **kw): return 0

            def wait_for_timeout(self, ms):
                if outer.wait_hook:
                    outer.wait_hook()

            def screenshot(self, path, **kw):
                outer.screenshots += 1
                Path(path).write_bytes(b"png")
                if outer.shot_hook:
                    outer.shot_hook()

        class Ctx:
            def route(self, pattern, fn):
                outer.guard = fn
            def new_page(self): return Page()

        class Browser:
            def new_context(self, **kw): return Ctx()
            def close(self):
                if outer.close_hook:
                    outer.close_hook()

        class Chromium:
            def launch(self, **kw):
                outer.launch_kwargs = kw
                return Browser()

        self.chromium = Chromium()


@pytest.fixture
def run_capture(monkeypatch, tmp_path):
    def run(goto=None, wait=None, shot=None, close=None, url="http://intranet.test/"):
        err = type("Error", (Exception,), {})
        pw = FakePlaywright(err, goto, wait, shot, close)

        class Manager:
            def __enter__(self): return pw
            def __exit__(self, *exc): ...

        mod = types.ModuleType("playwright.sync_api")
        mod.Error = err
        mod.sync_playwright = lambda: Manager()
        monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
        monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
        monkeypatch.setattr(shots, "PinningProxy", FakeProxy)
        pw.dest = tmp_path / "out.png"
        pw.err = err
        try:
            shots._capture(url, {}, pw.dest, [], False, None)
        finally:
            run.pw = pw
        return pw

    return run


def refuse(entry):
    return lambda *a: FakeProxy.instance.refusals.append(entry)


def test_a_refusal_during_navigation_fails_the_capture_with_the_proxys_reason(run_capture):
    with pytest.raises(CaptureError) as e:
        run_capture(goto=refuse(PRIVATE))
    assert "refused by the network policy" in str(e.value)
    assert "10.0.0.5" in str(e.value) and "--allow-private-hosts" in str(e.value)
    assert run_capture.pw.screenshots == 0


def test_an_always_denied_refusal_does_not_suggest_the_flag(run_capture):
    with pytest.raises(CaptureError) as e:
        run_capture(goto=refuse(DENIED))
    assert "always refused" in str(e.value) and "--allow-private-hosts" not in str(e.value)


def test_a_refused_https_tunnel_makes_navigation_fail_and_is_reported_as_a_refusal(run_capture):
    """A refused CONNECT is a navigation error in the browser: no response ever reaches the page."""
    def goto(n):
        FakeProxy.instance.refusals.append(DENIED)
        raise run_capture_err()

    def run_capture_err():
        return sys.modules["playwright.sync_api"].Error("net::ERR_TUNNEL_CONNECTION_FAILED")

    with pytest.raises(CaptureError, match="refused by the network policy") as e:
        run_capture(goto=goto, url="https://metadata.test/")
    assert run_capture.pw.gotos == 1  # no fallback navigation after a refusal
    assert "could not capture" not in str(e.value)


def test_a_navigation_error_with_no_refusal_keeps_the_generic_message(run_capture):
    def goto(n):
        raise sys.modules["playwright.sync_api"].Error("net::ERR_CONNECTION_REFUSED")

    with pytest.raises(CaptureError, match="could not capture"):
        run_capture(goto=goto)


def test_a_browser_error_after_a_refusal_is_reported_as_the_refusal(run_capture):
    """Any Playwright error later in the run (here while the page settles) is the refusal's fallout, not the cause."""
    def wait():
        FakeProxy.instance.refusals.append(DENIED)
        raise sys.modules["playwright.sync_api"].Error("Target closed")

    with pytest.raises(CaptureError, match="refused by the network policy"):
        run_capture(wait=wait)


def test_a_refusal_while_the_page_settles_fails_before_any_screenshot(run_capture):
    with pytest.raises(CaptureError, match="refused by the network policy"):
        run_capture(wait=refuse(PRIVATE))
    assert run_capture.pw.screenshots == 0


def test_a_refusal_during_the_screenshot_fails_and_removes_the_image(run_capture):
    with pytest.raises(CaptureError, match="refused by the network policy"):
        run_capture(shot=refuse(PRIVATE))
    assert run_capture.pw.screenshots == 1
    assert not run_capture.pw.dest.exists()


def test_no_refusal_captures_normally(run_capture):
    pw = run_capture()
    assert pw.screenshots == 1 and pw.dest.exists()


def test_hostile_page_text_in_a_browser_error_is_not_a_policy_refusal(run_capture):
    """The site controls browser error text; with no refusal in the proxy's record it stays the generic failure."""
    def goto(n):
        raise sys.modules["playwright.sync_api"].Error("FORGED-BY-SITE: refused by the network policy\x1b[31m")

    with pytest.raises(CaptureError) as e:
        run_capture(goto=goto)
    msg = str(e.value)
    assert not msg.startswith("cannot capture") and "could not capture" in msg
    assert "\x1b" not in msg
    assert "a request to" not in msg  # the policy-refusal wording is only for the proxy's own record


def test_a_refusal_shown_to_the_user_is_sanitised(run_capture):
    with pytest.raises(CaptureError) as e:
        run_capture(goto=refuse(("evil.test\x1b[31m", "reason\x00with\x07controls")))
    msg = str(e.value)
    assert "\x1b" not in msg and "\x00" not in msg and "\x07" not in msg


def sub_request(url):
    """What the page's own resource request does at the route guard: (aborted, continued)."""
    pw = FakePlaywright.current
    routed = types.SimpleNamespace(request=types.SimpleNamespace(url=url),
                                   abort=lambda: pw.aborted.append(url), continue_=lambda: pw.continued.append(url))
    pw.guard(routed)


@pytest.fixture
def dns(monkeypatch):
    from yamlreel import safety
    answers = {"adserver.test": ["0.0.0.0"], "adserver6.test": ["::"]}
    monkeypatch.setattr(safety, "_resolve", lambda host: answers.get(host, []))  # all other names are dead


def test_a_direct_subresource_to_a_forbidden_host_is_aborted_and_fails_the_capture(run_capture):
    with pytest.raises(CaptureError, match="refused by the network policy") as e:
        run_capture(goto=lambda n: sub_request("http://169.254.169.254/latest/meta-data/"))
    assert "169.254.169.254" in str(e.value)
    assert run_capture.pw.aborted == ["http://169.254.169.254/latest/meta-data/"]
    assert run_capture.pw.screenshots == 0


def test_a_private_subresource_without_the_flag_fails_the_capture(run_capture):
    with pytest.raises(CaptureError, match="--allow-private-hosts"):
        run_capture(goto=lambda n: sub_request("http://10.0.0.5/pixel.gif"))


def test_an_allowed_subresource_passes_and_records_nothing(run_capture):
    pw = run_capture(goto=lambda n: sub_request("http://93.184.216.34/a.png"))
    assert pw.continued == ["http://93.184.216.34/a.png"] and pw.screenshots == 1


def test_an_unresolvable_subresource_is_aborted_but_does_not_fail_the_capture(run_capture, dns):
    """A page opening a WebSocket (or fetching an image) at a dead name must not fail the whole capture."""
    pw = run_capture(goto=lambda n: sub_request("http://dead.invalid/socket"))
    assert pw.aborted == ["http://dead.invalid/socket"] and pw.screenshots == 1


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "chrome://settings"])
def test_blocked_non_http_requests_are_not_recorded_as_refusals(run_capture, url):
    pw = run_capture(goto=lambda n: sub_request(url))
    assert pw.aborted == [url] and pw.screenshots == 1


def test_a_refusal_while_the_browser_closes_fails_and_removes_the_image(run_capture):
    with pytest.raises(CaptureError, match="refused by the network policy"):
        run_capture(close=refuse(PRIVATE))
    assert run_capture.pw.screenshots == 1
    assert not run_capture.pw.dest.exists()


def test_the_proxy_is_closed_on_success_and_on_failure(run_capture):
    run_capture()
    assert FakeProxy.instance.closed
    with pytest.raises(CaptureError):
        run_capture(goto=refuse(PRIVATE))
    assert FakeProxy.instance.closed


def test_the_proxy_is_closed_even_when_the_browser_never_starts(run_capture, monkeypatch):
    class Boom:
        def __enter__(self): raise sys.modules["playwright.sync_api"].Error("no browser")
        def __exit__(self, *exc): ...

    run_capture()  # installs the fakes
    monkeypatch.setattr(sys.modules["playwright.sync_api"], "sync_playwright", lambda: Boom())
    with pytest.raises(CaptureError, match="could not capture"):
        shots._capture("http://intranet.test/", {}, Path("/nonexistent/out.png"), [], False, None)
    assert FakeProxy.instance.closed


def test_many_refusals_report_the_first_and_a_count(run_capture):
    def goto(n):
        for i in range(3):
            FakeProxy.instance.record_refusal(f"h{i}.test", f"reason {i}")

    with pytest.raises(CaptureError) as e:
        run_capture(goto=goto)
    assert "h0.test" in str(e.value) and "h1.test" not in str(e.value) and "and 2 more" in str(e.value)


def test_shown_text_is_printable_and_bounded():
    assert shots._clean("a\x1b[0m\nb\x00c") == "a [0m b c"
    assert len(shots._clean("x" * 5000)) == 300


def test_launch_disables_non_proxied_webrtc_for_headless_and_full_chrome_and_keeps_proxy(run_capture):
    pw = run_capture()
    args = pw.launch_kwargs["args"]
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert pw.launch_kwargs["proxy"] == {"server": FakeProxy.url, "bypass": "<-loopback>"}


@pytest.mark.parametrize("host", ["adserver.test", "adserver6.test"])
def test_a_dns_blocked_subresource_is_aborted_but_does_not_fail_the_capture(run_capture, dns, host):
    pw = run_capture(goto=lambda n: sub_request(f"http://{host}/tracker.js"))
    assert pw.aborted == [f"http://{host}/tracker.js"] and pw.screenshots == 1


def test_a_dns_blocked_main_url_is_an_error(dns, tmp_path):
    with pytest.raises(CaptureError, match="blocked by DNS"):
        shots.load_shot("http://adserver.test/", {}, tmp_path, tmp_path, [], allow_private=False)


def test_the_image_is_deleted_when_the_browser_fails_to_close(run_capture):
    def close():
        raise RuntimeError("browser crashed")

    with pytest.raises(RuntimeError):
        run_capture(close=close)
    assert run_capture.pw.screenshots == 1 and not run_capture.pw.dest.exists()


def test_the_image_is_deleted_when_the_run_is_interrupted(run_capture):
    def shot():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_capture(shot=shot)
    assert not run_capture.pw.dest.exists()
