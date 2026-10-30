"""A tiny local forward proxy that closes the DNS-rebinding gap for URL capture.

Chromium is pointed at this proxy. For every connection it resolves the host name exactly once, validates the
address with the same rules as `safety.pinned_ips`, and connects to that address, so a hostile DNS server cannot
answer "public" to the check and "internal" to the browser. HTTPS passes through untouched (CONNECT tunnel)."""
from __future__ import annotations

import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit

from .safety import UnsafeInput, pinned_ips

MAX_HEAD = 64 * 1024
IDLE_SECONDS = 30


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


def _relay_response(upstream: socket.socket, client: socket.socket) -> None:
    """Copy the upstream answer to the client until the upstream closes. Nothing flows client -> upstream here, so
    a second request on the same client connection is never forwarded unchecked."""
    while True:
        r, _, _ = select.select([upstream], [], [], IDLE_SECONDS)
        if not r:
            return
        data = upstream.recv(65536)
        if not data:
            return
        client.sendall(data)


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
                if not chunk or len(head) > MAX_HEAD:
                    return
                head += chunk
            head, _, rest = head.partition(b"\r\n\r\n")
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
                body_len = 0
                for h, n in zip(lines[1:], names):
                    if n == "content-length":
                        value = h.split(":", 1)[1].strip()
                        if not (value.isascii() and value.isdigit()):
                            client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                            return
                        body_len = int(value)
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
                    _relay_response(upstream, client)
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
