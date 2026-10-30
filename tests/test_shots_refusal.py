"""Capture-side network guard (docs/specs/batch1-trust-the-input.md, follow-up PR): the pinning proxy's refusal fails
the capture, and Chromium is launched so WebRTC cannot bypass the proxy.

Playwright and the proxy are replaced by fakes injected into `sys.modules` / `shots`, so nothing here needs Playwright
installed, a browser, or the network. Real Chromium behaviour is not exercised."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from yamlreel import shots
from yamlreel.shots import CaptureError

REFUSAL_BODY = "intranet.test resolves to a private or loopback address (10.0.0.5). To capture pass --allow-private-hosts"


class FakeResponse:
    def __init__(self, url: str, status: int, headers: dict[str, str], body: str = "") -> None:
        self.url, self.status, self.headers, self._body = url, status, headers, body

    def text(self) -> str:
        return self._body


class FakeProxy:
    url = "http://127.0.0.1:1"

    def __init__(self, allow_private: bool) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass


class FakePlaywright:
    """Records launch kwargs; every goto() emits `responses` to the context's response listener."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.launch_kwargs: dict = {}
        self.screenshots = 0
        self.listeners: list = []
        outer = self

        class Page:
            def goto(self, url, **kw):
                for r in outer.responses:
                    for fn in outer.listeners:
                        fn(r)

            def click(self, *a, **kw): ...
            def evaluate(self, *a, **kw): return 0
            def wait_for_timeout(self, ms): ...

            def screenshot(self, path, **kw):
                outer.screenshots += 1
                Path(path).write_bytes(b"png")

        class Ctx:
            def route(self, pattern, fn): ...
            def on(self, event, fn):
                assert event == "response"
                outer.listeners.append(fn)
            def new_page(self): return Page()

        class Browser:
            def new_context(self, **kw): return Ctx()
            def close(self): ...

        class Chromium:
            def launch(self, **kw):
                outer.launch_kwargs = kw
                return Browser()

        self.chromium = Chromium()


@pytest.fixture
def run_capture(monkeypatch, tmp_path):
    def run(responses: list[FakeResponse]) -> FakePlaywright:
        pw = FakePlaywright(responses)

        class Manager:
            def __enter__(self): return pw
            def __exit__(self, *exc): ...

        mod = types.ModuleType("playwright.sync_api")
        mod.Error = type("Error", (Exception,), {})
        mod.sync_playwright = lambda: Manager()
        monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
        monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
        monkeypatch.setattr(shots, "PinningProxy", FakeProxy)
        shots._capture("http://intranet.test/", {}, tmp_path / "out.png", [], False, None)
        return pw

    return run


REFUSED_HEADERS = {"x-yamlreel-refused": "1"}


def test_proxy_refusal_fails_capture_with_reason(run_capture):
    with pytest.raises(CaptureError) as e:
        run_capture([FakeResponse("http://intranet.test/", 403, REFUSED_HEADERS, REFUSAL_BODY)])
    msg = str(e.value)
    assert "refused by the network policy" in msg
    assert "--allow-private-hosts" in msg  # relayed from the proxy's reason for an allowed private range


def test_always_denied_refusal_does_not_suggest_the_flag(run_capture):
    body = "169.254.169.254 resolves to 169.254.169.254, a cloud-metadata, link-local or reserved address; always refused"
    with pytest.raises(CaptureError) as e:
        run_capture([FakeResponse("http://metadata.test/", 403, REFUSED_HEADERS, body)])
    assert "always refused" in str(e.value)
    assert "--allow-private-hosts" not in str(e.value)


def test_refused_subresource_fails_capture(run_capture):
    ok = FakeResponse("http://intranet.test/", 200, {})
    bad = FakeResponse("http://10.0.0.5/x.js", 403, REFUSED_HEADERS, REFUSAL_BODY)
    with pytest.raises(CaptureError, match="10.0.0.5"):
        run_capture([ok, bad])


def test_plain_403_is_not_a_refusal(run_capture):
    pw = run_capture([FakeResponse("http://intranet.test/", 403, {}, "Forbidden")])
    assert pw.screenshots == 1


def test_refusal_header_on_other_status_is_not_a_refusal():
    assert not shots.is_proxy_refusal(200, REFUSED_HEADERS)
    assert not shots.is_proxy_refusal(403, {})
    assert not shots.is_proxy_refusal(403, {"x-yamlreel-refused": "0"})
    assert shots.is_proxy_refusal(403, REFUSED_HEADERS)


def test_launch_disables_non_proxied_webrtc_and_keeps_proxy(run_capture):
    pw = run_capture([FakeResponse("http://intranet.test/", 200, {})])
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in pw.launch_kwargs["args"]
    assert pw.launch_kwargs["proxy"] == {"server": FakeProxy.url, "bypass": "<-loopback>"}
