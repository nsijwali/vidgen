"""A tiny local forward proxy that closes the DNS-rebinding gap for URL capture.

Chromium is pointed at this proxy. For every connection it resolves the host name exactly once, validates the
address with the same rules as `safety.pinned_ips`, and connects to that address, so a hostile DNS server cannot
answer "public" to the check and "internal" to the browser. HTTPS passes through untouched (CONNECT tunnel).

Known and accepted: `Expect: 100-continue` is not special-cased (the client sends its body unprompted or waits and
retries), port 0 in a plain-HTTP URL is passed to the connect (it fails there), and host names use Python's IDNA 2003
codec rather than UTS 46 (the normalised name is the one that is judged and resolved)."""
from __future__ import annotations

import re
import select
import socket
import socketserver
import threading
import time
from urllib.parse import urlsplit

from .safety import UnsafeInput, pinned_ips

MAX_HEAD = 64 * 1024
IDLE_SECONDS = 30
RELAY_DEADLINE = 120.0  # seconds one response (head and body) may take in total


def _connect(host: str, port: int, allow_private: bool) -> socket.socket:
    last: Exception | None = None
    for ip in pinned_ips(host, allow_private):
        try:
            return socket.create_connection((ip, port), timeout=15)
        except OSError as e:
            last = e
    raise OSError(f"cannot connect to {host}:{port}: {last}")


def _parse_port(text: str) -> int:
    """A CONNECT port: ASCII digits only (int() also takes "+443" and "4_43"), 1..65535."""
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= 65535:
        raise ValueError(f"bad port {text!r}")
    return int(text)


_CHUNK_SIZE = re.compile(rb"[0-9a-fA-F]{1,16}")


class _Chunked:
    """Follows chunked framing just far enough to know where the body ends."""

    def __init__(self) -> None:
        self.state, self.line, self.left, self.done = "size", b"", 0, False

    def feed(self, data: bytes) -> int:
        """Consume `data` up to the end of the body; return how many bytes belong to it (the rest is not forwarded)."""
        i = 0
        while i < len(data) and not self.done:
            if self.state == "data":
                n = min(self.left, len(data) - i)
                self.left -= n
                i += n
                if not self.left:
                    self.state = "crlf"
                continue
            j = data.find(b"\n", i)
            end = len(data) if j < 0 else j + 1
            if len(self.line) + end - i > MAX_HEAD:
                raise ValueError("chunk header too long")
            self.line += data[i:end]
            i = end
            if j < 0:
                continue
            line, self.line = self.line.strip(), b""
            if self.state == "size":
                size = line.split(b";")[0].strip()
                if not _CHUNK_SIZE.fullmatch(size):
                    raise ValueError("bad chunk size")
                self.left = int(size, 16)
                self.state = "trailer" if self.left == 0 else "data"
            elif self.state == "crlf":
                self.state = "size"
            elif not line:
                self.done = True
        return i


def _relay_response(upstream: socket.socket, client: socket.socket, method: str) -> None:
    """Copy the upstream answer to the client with a rewritten head (`Connection: close`, no keep-alive), stopping as
    soon as the body is complete. Nothing flows client -> upstream here, so a second request on the same client
    connection is never forwarded unchecked. Interim 1xx heads are passed on unchanged until the final head. The
    whole relay is bounded by RELAY_DEADLINE and ends when the client goes away."""
    deadline = time.monotonic() + RELAY_DEADLINE
    watch = [upstream, client]
    started = False

    def read() -> bytes:
        nonlocal watch
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return b""
            ready, _, _ = select.select(watch, [], [], min(IDLE_SECONDS, left))
            if not ready:
                return b""
            if client in ready:
                if not client.recv(65536):  # the client left; extra bytes (a pipelined request) are dropped
                    return b""
                watch = [upstream]
                continue
            return upstream.recv(65536)

    def send(data: bytes) -> None:
        nonlocal started
        started = True
        client.sendall(data)

    try:
        buf = b""
        while True:
            while True:
                end = buf.find(b"\r\n\r\n")
                if end >= 0:
                    break
                if len(buf) > MAX_HEAD:
                    raise ValueError("upstream response head too large")
                data = read()
                if not data:
                    raise OSError("upstream closed without a complete response head")
                buf += data
            if end > MAX_HEAD:
                raise ValueError("upstream response head too large")
            head, body = buf[:end], buf[end + 4:]
            lines = head.decode("latin-1").split("\r\n")
            try:
                status = int(lines[0].split(" ", 2)[1])
            except (IndexError, ValueError):
                status = 0
            if 100 <= status < 200 and status != 101:
                send(buf[:end + 4])  # interim answer (100, 103, ...): unchanged, the final head follows
                buf = body
                continue
            break
        hdrs: dict[str, str] = {}
        for h in lines[1:]:
            hdrs.setdefault(h.split(":", 1)[0].strip().lower(), h.split(":", 1)[-1].strip())
        kept = [h for h in lines[1:]
                if h.split(":", 1)[0].strip().lower() not in ("connection", "keep-alive", "proxy-connection")]
        send("\r\n".join([lines[0], *kept, "Connection: close", "Proxy-Connection: close"]).encode("latin-1")
             + b"\r\n\r\n")
        if method == "HEAD" or status in (101, 204, 304):  # 101 is never relayed as an upgrade: final, then close
            return
        remaining: int | None = None  # None: close-delimited
        chunked: _Chunked | None = None
        length = hdrs.get("content-length", "")
        if "chunked" in hdrs.get("transfer-encoding", "").lower():
            chunked = _Chunked()
        elif length.isascii() and length.isdigit():
            remaining = int(length)
        while True:
            if remaining is not None:
                body = body[:remaining]
                remaining -= len(body)
            elif chunked is not None:
                body = body[:chunked.feed(body)]
            if body:
                send(body)
            if remaining == 0 or (chunked is not None and chunked.done):
                return
            body = read()
            if not body:
                return
    except (OSError, ValueError):
        if started:  # a 502 into a half-sent response would be garbage: just let the close truncate it
            return
        raise


def _splice(a: socket.socket, b: socket.socket) -> None:
    socks = [a, b]
    while True:
        r, _, _ = select.select(socks, [], [], IDLE_SECONDS)
        if not r:
            return
        for s in r:
            data = s.recv(65536)
            if not data:
                return
            (b if s is a else a).sendall(data)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(IDLE_SECONDS)
        allow_private: bool = self.server.allow_private  # type: ignore[attr-defined]
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(8192)
                if not chunk:
                    return
                head += chunk
                if b"\r\n\r\n" not in head and len(head) > MAX_HEAD:
                    return
            head, _, rest = head.partition(b"\r\n\r\n")
            if len(head) > MAX_HEAD:
                return
            if b"\n" in head.replace(b"\r\n", b"") or b"\r" in head.replace(b"\r\n", b""):
                client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                return  # a bare LF or CR could smuggle a header past the checks below
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            tunnel, first, need = False, b"", 0
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                try:
                    port_number = _parse_port(port)
                except ValueError:
                    client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                    return
                upstream = _connect(host.strip("[]"), port_number, allow_private)
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
                tunnel = True
            else:
                u = urlsplit(target)
                if u.scheme != "http" or not u.hostname:
                    client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                    return
                names = [h.split(":", 1)[0].strip().lower() for h in lines[1:]]
                headers = [h for h, n in zip(lines[1:], names)
                           if n not in ("connection", "proxy-connection", "keep-alive", "transfer-encoding")]
                if "transfer-encoding" in names:
                    client.sendall(b"HTTP/1.1 411 Length Required\r\nConnection: close\r\n\r\n")
                    return
                lengths = {h.split(":", 1)[1].strip() for h, n in zip(lines[1:], names) if n == "content-length"}
                if len(lengths) > 1 or any(not (v.isascii() and v.isdigit()) for v in lengths):
                    client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                    return  # RFC 9112 6.3: conflicting or malformed lengths are an error
                body_len = int(lengths.pop()) if lengths else 0
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                upstream = _connect(u.hostname, u.port or 80, allow_private)
                req = "\r\n".join([f"{method} {path} {version}", *headers,
                                    "Connection: close", "Proxy-Connection: close"]) + "\r\n\r\n"
                # Exactly one request goes upstream: its body, but never bytes of a pipelined next request.
                body = rest[:body_len]
                need = body_len - len(body)
                first = req.encode("latin-1") + body
            try:
                if tunnel:
                    _splice(client, upstream)
                else:
                    upstream.sendall(first)
                    while need > 0:
                        chunk = client.recv(min(65536, need))
                        if not chunk:
                            return
                        need -= len(chunk)
                        upstream.sendall(chunk)
                    _relay_response(upstream, client, method)
            finally:
                upstream.close()
        except UnsafeInput as e:
            msg = str(e).encode()
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nX-Yamlreel-Refused: 1\r\n"
                           b"Content-Length: %d\r\n\r\n" % len(msg) + msg)
        except (OSError, ValueError):
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
        finally:
            try:
                client.shutdown(socket.SHUT_RDWR)  # after any answer above, so a client never reuses the connection
            except OSError:
                pass


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class PinningProxy:
    """Use as a context manager; `.url` is the proxy address for the browser."""

    def __init__(self, allow_private: bool):
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.allow_private = allow_private  # type: ignore[attr-defined]
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "PinningProxy":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
