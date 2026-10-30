"""The network guard: which addresses a spec's URLs may reach (see docs/specs/batch1-trust-the-input.md, PR A).

Nothing here resolves a real name or connects to a non-loopback address: `safety._resolve` is replaced by a fake
table, and the proxy tests spy on `socket.create_connection` and fail the test on any non-loopback connect."""
from __future__ import annotations

import http.server
import ipaddress
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest

from yamlreel import safety
from yamlreel.safety import UnsafeInput, check_http_url, pinned_ips

FAKE_DNS: dict[str, list[str]] = {
    "localhost": ["127.0.0.1"],
    "mixed.test": ["93.184.216.34", "100.64.0.1"],
    "meta-mixed.test": ["93.184.216.34", "169.254.169.254"],
    "mapped.test": ["::ffff:100.64.0.1"],
    "dupes.test": ["93.184.216.34", "93.184.216.34"],
    "empty.test": [],
}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    def resolve(host: str) -> list[str]:
        assert host in FAKE_DNS, f"unexpected name resolution for {host!r}"
        return list(FAKE_DNS[host])

    monkeypatch.setattr(safety, "_resolve", resolve)


def _teredo(client: str) -> str:
    """A Teredo address (2001::/32) whose obfuscated client field encodes `client`."""
    c = int(ipaddress.IPv4Address(client)) ^ 0xFFFFFFFF
    addr = ipaddress.IPv6Address((0x20010000 << 96) | (0x01020304 << 64) | c)
    assert addr.teredo == (ipaddress.IPv4Address("1.2.3.4"), ipaddress.IPv4Address(client))
    return str(addr)


def _url(host: str) -> str:
    return f"http://[{host}]/" if ":" in host else f"http://{host}/"


REFUSED_WITHOUT_FLAG = [
    "100.64.0.1", "100.100.100.200", "100.127.255.254", "::ffff:100.64.0.1", "::ffff:127.0.0.1",
    "64:ff9b::a9fe:a9fe", "2002:a9fe:a9fe::1", _teredo("10.0.0.1"), "192.0.0.192", "198.18.0.1", "240.0.0.1",
    "2130706433", "0x7f.1", "0177.0.0.1", "127.1",
]


@pytest.mark.parametrize("host", REFUSED_WITHOUT_FLAG)
def test_refused_without_the_private_flag(host):
    with pytest.raises(UnsafeInput):
        check_http_url(_url(host), allow_private=False)


ALWAYS_REFUSED = [
    "169.254.169.254", "169.254.170.2", "fd00:ec2::254", "100.100.100.200", "192.0.0.192",
    "METADATA.GOOGLE.INTERNAL.", "::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe", "0.0.0.0", "::", "224.0.0.1",
    "fec0::1", "ff02::1", "0.1.2.3", "64:ff9b:1::a9fe:a9fe", "instance-data.", "metadata.goog",
    "2002:a9fe:a9fe::1", _teredo("169.254.169.254"), "fd20:ce::254", "fd00:c1::a9fe:a9fe", "168.63.129.16",
    "metadata.tencentyun.com", "\uff4d\uff45\uff54\uff41\uff44\uff41\uff54\uff41.google.internal",
    "\uff11\uff12\uff17\uff0e\uff11",
]
ALWAYS_MESSAGE = r"always refused|cloud metadata host refused|non-standard numeric address"
# Refused with and without the flag, but as "not a public internet address" rather than as metadata/reserved.
NOT_PUBLIC = ["3fff::1", "5f00::1", "4000::1", "200::1", "::ffff:0:a9fe:a9fe", "192.0.2.1", "198.51.100.7",
              "203.0.113.9", "2001:db8::1", "100::1", "2001:1::1", "198.18.0.1"]


@pytest.mark.parametrize("host", ALWAYS_REFUSED)
@pytest.mark.parametrize("flag", [True, False])
def test_refused_even_with_the_private_flag(host, flag):
    with pytest.raises(UnsafeInput, match=ALWAYS_MESSAGE):
        check_http_url(_url(host), allow_private=flag)


@pytest.mark.parametrize("host", NOT_PUBLIC)
@pytest.mark.parametrize("flag", [True, False])
def test_non_public_ranges_are_refused_with_and_without_the_flag(host, flag):
    with pytest.raises(UnsafeInput, match="not a public internet address"):
        check_http_url(_url(host), allow_private=flag)


@pytest.mark.parametrize("flag", [True, False])
def test_verdicts_come_from_the_tables_not_from_is_global(flag, monkeypatch):
    """An interpreter whose is_global says True for everything (older tables) must not change any verdict."""
    monkeypatch.setattr(ipaddress.IPv4Address, "is_global", property(lambda self: True))
    monkeypatch.setattr(ipaddress.IPv6Address, "is_global", property(lambda self: True))
    for host in ALWAYS_REFUSED:
        with pytest.raises(UnsafeInput, match=ALWAYS_MESSAGE):
            check_http_url(_url(host), allow_private=flag)
    for host in NOT_PUBLIC:
        with pytest.raises(UnsafeInput, match="not a public internet address"):
            check_http_url(_url(host), allow_private=flag)
    check_http_url(_url("93.184.216.34"), allow_private=flag)
    if not flag:
        for host in ("100.64.0.1", "10.0.0.1", "127.0.0.1", "fd12:3456::1"):
            with pytest.raises(UnsafeInput, match="--allow-private-hosts"):
                check_http_url(_url(host), allow_private=False)


def test_six_to_four_is_judged_by_the_embedded_ipv4_and_keeps_the_v6_address():
    assert pinned_ips("2002:808:808::1", False) == ["2002:808:808::1"]


def test_unwrapped_wrapper_message_names_the_embedded_address():
    with pytest.raises(UnsafeInput, match=r"\(embeds 169\.254\.169\.254\), a cloud-metadata"):
        check_http_url("http://[2002:a9fe:a9fe::1]/", True)


def test_bad_urls_and_hosts_are_unsafe_input_not_other_exceptions():
    for url in ("http://[::ffff:127.1]/", "http://[abc]/", "http://[::1/", "http://[::g]/", "http://a..b/",
                "http://" + "a" * 70 + ".test/"):
        with pytest.raises(UnsafeInput):
            check_http_url(url, allow_private=True)
    with pytest.raises(UnsafeInput, match="not a valid IPv6 address"):
        pinned_ips("::ffff:127.1", True)


def test_scope_ids_are_stripped_and_judged_as_the_address():
    assert pinned_ips("::1%lo0", True) == ["::1"]
    with pytest.raises(UnsafeInput, match=r"::1%lo0 resolves to a private or loopback address \(::1\).*--allow-private-hosts"):
        pinned_ips("::1%lo0", False)
    with pytest.raises(UnsafeInput, match="always refused"):
        pinned_ips("FE80::1%LO0", True)


@pytest.mark.parametrize("host", ["100.64.0.1", "::ffff:10.0.0.5", "fd12:3456::1", "172.16.0.1", "127.0.0.1",
                                  "localhost", "::1"])
def test_private_ranges_are_allowed_with_the_flag(host):
    check_http_url(_url(host), allow_private=True)


@pytest.mark.parametrize("host", ["93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed_without_the_flag(host):
    check_http_url(_url(host), allow_private=False)


def test_mixed_answers_are_judged_as_a_whole():
    with pytest.raises(UnsafeInput, match="--allow-private-hosts"):
        check_http_url("http://mixed.test/", allow_private=False)
    assert pinned_ips("mixed.test", True) == ["93.184.216.34", "100.64.0.1"]
    for flag in (False, True):
        with pytest.raises(UnsafeInput, match="always refused"):
            check_http_url("http://meta-mixed.test/", allow_private=flag)
    with pytest.raises(UnsafeInput, match="--allow-private-hosts"):
        check_http_url("http://mapped.test/", allow_private=False)
    with pytest.raises(UnsafeInput, match="cannot resolve host: empty.test"):
        check_http_url("http://empty.test/", allow_private=True)


def test_pinned_addresses_are_deduplicated_and_mapped_ones_connect_to_the_ipv4():
    assert pinned_ips("dupes.test", False) == ["93.184.216.34"]
    assert pinned_ips("::ffff:10.0.0.5", True) == ["10.0.0.5"]
    assert pinned_ips("64:ff9b::808:808", False) == ["64:ff9b::808:808"]  # other wrappers keep the v6 address


def test_messages():
    with pytest.raises(UnsafeInput, match=r"169\.254\.169\.254 .*a cloud-metadata, link-local or reserved address; always refused"):
        check_http_url("http://169.254.169.254/", True)
    with pytest.raises(UnsafeInput, match=r"resolves to ::ffff:169\.254\.169\.254 \(embeds 169\.254\.169\.254\), a cloud-metadata"):
        check_http_url("http://[::ffff:169.254.169.254]/", True)
    with pytest.raises(UnsafeInput, match=r"resolves to 198\.18\.0\.1, which is not a public internet address; refused"):
        check_http_url("http://198.18.0.1/", False)
    with pytest.raises(UnsafeInput, match="cloud metadata host refused: metadata.google.internal."):
        check_http_url("http://metadata.google.internal./", True)
    with pytest.raises(UnsafeInput, match=r"non-standard numeric address; write it as a dotted quad such as 127\.0\.0\.1"):
        check_http_url("http://2130706433/", True)


def test_numeric_forms_are_refused_before_any_resolution(monkeypatch):
    def boom(host):
        raise AssertionError("must not resolve")

    monkeypatch.setattr(safety, "_resolve", boom)
    for host in ("2130706433", "0x7f.1", "0177.0.0.1", "127.1", "0x7f000001", "1.2.3.4.5", "127.0.0.1.", "10.0.0"):
        if host == "127.0.0.1.":  # a trailing dot on a proper dotted quad is the same address
            assert pinned_ips(host, True) == ["127.0.0.1"]
            continue
        with pytest.raises(UnsafeInput, match="non-standard numeric address"):
            pinned_ips(host, True)


# ---------- the pinning proxy enforces the policy on every redirect hop and CONNECT ----------

def _redirecting_server(location: str):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _raw(proxy_url: str, request: bytes) -> bytes:
    ph = urlsplit(proxy_url)
    with socket.create_connection((ph.hostname, ph.port), timeout=5) as s:
        s.sendall(request)
        data = b""
        while chunk := s.recv(4096):
            data += chunk
    return data


def _get(proxy_url: str, target: str) -> bytes:
    return _raw(proxy_url, f"GET {target} HTTP/1.1\r\nHost: x\r\n\r\n".encode())


@pytest.fixture
def connect_spy(monkeypatch):
    """Record every address the proxy connects to; a connection to anything but loopback fails the test."""
    seen: list[tuple[str, int]] = []
    real = socket.create_connection

    def spy(addr, *args, **kwargs):
        seen.append((addr[0], addr[1]))
        assert addr[0] in ("127.0.0.1", "::1"), f"test tried to reach {addr[0]}"
        return real(addr, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", spy)
    return seen


@pytest.mark.parametrize("location,allow_private", [
    ("http://169.254.169.254/latest/meta-data/", True),
    ("http://100.100.100.200/", True),
    ("http://100.64.0.1/", False),
    ("http://[::ffff:169.254.169.254]/", True),
])
def test_a_redirect_hop_to_a_forbidden_address_gets_403_and_no_connection(location, allow_private, connect_spy):
    from yamlreel.pinproxy import PinningProxy
    srv = _redirecting_server(location)
    try:
        with PinningProxy(allow_private=True) as px_first, PinningProxy(allow_private=allow_private) as px:
            first = _get(px_first.url, f"http://127.0.0.1:{srv.server_address[1]}/")
            assert first.startswith(b"HTTP/1.0 302") or first.startswith(b"HTTP/1.1 302")
            hop = [h for h in first.decode("latin-1").split("\r\n") if h.lower().startswith("location:")][0]
            assert hop.split(":", 1)[1].strip() == location
            resp = _get(px.url, location)  # what the browser does for the next hop
        assert resp.split(b"\r\n")[0].startswith(b"HTTP/1.1 403")
        forbidden = {"169.254.169.254", "100.100.100.200", "100.64.0.1"}
        assert not forbidden & {a for a, _ in connect_spy}
    finally:
        srv.shutdown()


def test_the_proxy_serves_the_first_hop_then_refuses_the_redirect_target(connect_spy):
    """One proxy with the flag on: loopback is served, the metadata target it redirects to is refused."""
    from yamlreel.pinproxy import PinningProxy
    srv = _redirecting_server("http://169.254.169.254/")
    try:
        with PinningProxy(allow_private=True) as px:
            assert b" 302 " in _get(px.url, f"http://127.0.0.1:{srv.server_address[1]}/").split(b"\r\n")[0] + b" "
            assert _get(px.url, "http://169.254.169.254/").split(b"\r\n")[0].startswith(b"HTTP/1.1 403")
        assert {a for a, _ in connect_spy} == {"127.0.0.1"}
    finally:
        srv.shutdown()


@pytest.mark.parametrize("target,allow_private", [
    ("100.64.0.1:443", False), ("169.254.169.254:443", True), ("[::ffff:100.64.0.1]:443", False),
    ("[fd00:ec2::254]:443", True), ("2130706433:443", True),
])
def test_connect_to_a_forbidden_address_gets_403(target, allow_private, connect_spy):
    from yamlreel.pinproxy import PinningProxy
    with PinningProxy(allow_private=allow_private) as px:
        resp = _raw(px.url, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    assert resp.split(b"\r\n")[0].startswith(b"HTTP/1.1 403")
    assert [c for c in connect_spy if c[1] == 443] == []  # the only connection is the test client to the proxy


@pytest.mark.parametrize("target", ["100.64.0.1:+443", "100.64.0.1:4_43", "127.0.0.1:0", "127.0.0.1:65536",
                                    "127.0.0.1:"])
def test_connect_ports_must_be_plain_ascii_digits_in_range(target, connect_spy):
    from yamlreel.pinproxy import PinningProxy
    with PinningProxy(allow_private=True) as px:
        resp = _raw(px.url, f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
    assert resp.split(b"\r\n")[0].startswith(b"HTTP/1.1 400")
    assert b"X-Yamlreel-Refused" not in resp
    assert [c for c in connect_spy if c[1] in (0, 443, 65536)] == []


def test_proxy_refusals_carry_the_refused_header(connect_spy):
    from yamlreel.pinproxy import PinningProxy
    with PinningProxy(allow_private=False) as px:
        plain = _get(px.url, "http://100.64.0.1/")
        tunnel = _raw(px.url, b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n")
    for resp in (plain, tunnel):
        head = resp.split(b"\r\n\r\n")[0].decode("latin-1").lower().split("\r\n")
        assert head[0].startswith("http/1.1 403")
        assert "x-yamlreel-refused: 1" in head


def _keepalive_upstream(received: list[bytes]):
    """A loopback server that ignores `Connection: close` and answers every request it sees on a connection."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return

            def one(c=c):
                buf = b""
                while data := c.recv(65536):
                    buf += data
                    while b"\r\n\r\n" in buf:
                        head, _, buf = buf.partition(b"\r\n\r\n")
                        received.append(head.split(b"\r\n")[0])
                        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello")

            threading.Thread(target=one, daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return srv


@pytest.mark.parametrize("pipelined", [False, True])
def test_a_second_request_on_the_same_proxy_connection_is_never_forwarded(pipelined, connect_spy):
    """The upstream ignores `Connection: close`; the proxy still ends the response and closes the client socket."""
    import time
    from yamlreel.pinproxy import PinningProxy
    received: list[bytes] = []
    up = _keepalive_upstream(received)
    port = up.getsockname()[1]
    first = f"GET http://127.0.0.1:{port}/one HTTP/1.1\r\nHost: x\r\n\r\n".encode()
    second = b"GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\nHost: 169.254.169.254\r\n\r\n"
    try:
        with PinningProxy(allow_private=True) as px:
            ph = urlsplit(px.url)
            t0 = time.monotonic()
            with socket.create_connection((ph.hostname, ph.port), timeout=10) as s:
                s.sendall(first + second if pipelined else first)
                data = b""
                while chunk := s.recv(65536):  # ends by a clean close right after the body, not after 30 s
                    data += chunk
                head = data.split(b"\r\n\r\n")[0].lower()
                assert data.endswith(b"hello") and b"connection: close" in head and b"proxy-connection: close" in head
                assert b"keep-alive" not in head and data.count(b"HTTP/1.1") == 1
                if not pipelined:  # the connection is gone: nothing may be forwarded on it
                    try:
                        s.sendall(second)
                        assert s.recv(65536) == b""
                    except OSError:
                        pass
            assert time.monotonic() - t0 < 5
        assert received == [b"GET /one HTTP/1.1"]  # only the checked request reached the upstream
        assert {a for a, _ in connect_spy} == {"127.0.0.1"}
    finally:
        up.close()


def _one_shot_upstream(response: bytes, close: bool = True):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        c, _ = srv.accept()
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += c.recv(65536)
        c.sendall(response)
        if close:
            c.close()
        else:
            time.sleep(8)

    threading.Thread(target=serve, daemon=True).start()
    return srv


@pytest.mark.parametrize("method,response,tail", [
    ("GET", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: keep-alive\r\n\r\n"
            b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n", b"0\r\n\r\n"),
    ("GET", b"HTTP/1.0 200 OK\r\nKeep-Alive: timeout=5\r\n\r\nclose-delimited body", b"close-delimited body"),
    ("HEAD", b"HTTP/1.1 200 OK\r\nContent-Length: 99\r\nConnection: keep-alive\r\n\r\n", b"\r\n\r\n"),
    ("GET", b"HTTP/1.1 204 No Content\r\nConnection: keep-alive\r\n\r\n", b"\r\n\r\n"),
    ("GET", b"HTTP/1.1 304 Not Modified\r\nConnection: keep-alive\r\n\r\n", b"\r\n\r\n"),
])
@pytest.mark.parametrize("hold_open", [False, True])
def test_response_framing_survives_the_head_rewrite(method, response, tail, hold_open, connect_spy):
    """Bodies are forwarded as the upstream framed them; with a framed body the proxy closes without waiting for an
    upstream that keeps the connection open (close-delimited bodies legitimately end only when it closes)."""
    import time
    from yamlreel.pinproxy import PinningProxy
    if hold_open and response.startswith(b"HTTP/1.0"):
        pytest.skip("a close-delimited body ends only with the upstream close")
    up = _one_shot_upstream(response, close=not hold_open)
    try:
        with PinningProxy(allow_private=True) as px:
            t0 = time.monotonic()
            got = _raw(px.url, f"{method} http://127.0.0.1:{up.getsockname()[1]}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            assert time.monotonic() - t0 < 5
        head, _, body = got.partition(b"\r\n\r\n")
        low = head.lower()
        assert b"connection: close" in low and b"proxy-connection: close" in low and b"keep-alive" not in low
        assert body == response.partition(b"\r\n\r\n")[2] if method == "GET" and b"204" not in response else True
        if b"chunked" in response:
            assert b"transfer-encoding: chunked" in low and got.endswith(tail)
        if b"content-length: 99" in low:
            assert b"content-length: 99" in low and body == b""  # HEAD: framing header kept, no body
    finally:
        up.close()


@pytest.mark.parametrize("headers", [
    "Content-Length: abc", "Content-Length: -1", "Content-Length: 1\r\nContent-Length: 2", "Content-Length: 1 2",
])
def test_bad_or_conflicting_content_length_is_a_400(headers, connect_spy):
    from yamlreel.pinproxy import PinningProxy
    with PinningProxy(allow_private=True) as px:
        resp = _raw(px.url, f"POST http://127.0.0.1:9/ HTTP/1.1\r\nHost: x\r\n{headers}\r\n\r\nab".encode())
    assert resp.split(b"\r\n")[0].startswith(b"HTTP/1.1 400")
    assert [c for c in connect_spy if c[1] == 9] == []


def test_identical_duplicate_content_length_is_accepted(connect_spy):
    from yamlreel.pinproxy import PinningProxy
    up = _one_shot_upstream(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    try:
        with PinningProxy(allow_private=True) as px:
            resp = _raw(px.url, (f"POST http://127.0.0.1:{up.getsockname()[1]}/ HTTP/1.1\r\nHost: x\r\n"
                                 "Content-Length: 1\r\nContent-Length: 1\r\n\r\nA").encode())
        assert resp.endswith(b"ok")
    finally:
        up.close()


@pytest.mark.parametrize("raw", [
    b"GET http://127.0.0.1:9/ HTTP/1.1\r\nHost: x\nX-Evil: 1\r\n\r\n",
    b"GET http://127.0.0.1:9/ HTTP/1.1\r\nHost: x\rX-Evil: 1\r\n\r\n",
    b"CONNECT 127.0.0.1:9 HTTP/1.1\r\nHost: x\nX: 1\r\n\r\n",
])
def test_a_bare_lf_or_cr_in_the_head_is_a_400(raw, connect_spy):
    from yamlreel.pinproxy import PinningProxy
    with PinningProxy(allow_private=True) as px:
        resp = _raw(px.url, raw)
    assert resp.split(b"\r\n")[0].startswith(b"HTTP/1.1 400")
    assert [c for c in connect_spy if c[1] == 9] == []


def test_the_forwarded_request_asks_for_connection_close_and_keeps_its_body(connect_spy):
    from yamlreel.pinproxy import PinningProxy
    seen: list[bytes] = []
    up = socket.socket()
    up.bind(("127.0.0.1", 0))
    up.listen(1)

    def serve():
        c, _ = up.accept()
        data = b""
        while b"ABCDE" not in data:
            data += c.recv(65536)
        seen.append(data)
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        with PinningProxy(allow_private=True) as px:
            resp = _raw(px.url, (f"POST http://127.0.0.1:{up.getsockname()[1]}/p HTTP/1.1\r\nHost: x\r\n"
                                 "Connection: keep-alive\r\nContent-Length: 5\r\n\r\nABCDE").encode())
        assert resp.endswith(b"ok")
        low = seen[0].lower()
        assert b"connection: close" in low and b"proxy-connection: close" in low and b"keep-alive" not in low
        assert seen[0].endswith(b"ABCDE")
    finally:
        up.close()


def test_browser_redirect_to_a_mapped_metadata_address_is_a_capture_error(tmp_path):
    """Optional: needs playwright and a Chromium build; skips cleanly otherwise (like the capture test)."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from yamlreel.shots import CaptureError, load_shot
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as e:  # noqa: BLE001 - any launch failure means no usable Chromium here
        pytest.skip(f"chromium unavailable: {e}")
    srv = _redirecting_server("http://[::ffff:169.254.169.254]/")
    try:
        with pytest.raises(CaptureError):
            load_shot(f"http://127.0.0.1:{srv.server_address[1]}/", {"viewport": [400, 300], "scale": 1},
                      tmp_path, tmp_path, [tmp_path], allow_private=True)
    finally:
        srv.shutdown()
