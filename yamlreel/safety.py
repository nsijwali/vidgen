"""Guards for everything a spec can make yamlreel read or fetch.

A spec is data, but it names files and URLs. These checks keep a spec you did not write from reading
arbitrary files on your machine or reaching internal network services.
"""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname


class UnsafeInput(ValueError):
    pass


def resolve_local(path: str | Path, base: Path, roots: list[Path]) -> Path:
    """Resolve `path` (relative to `base`) and require it to sit inside one of `roots`.

    The check is done on the fully resolved path, so symlinks cannot be used to escape."""
    p = Path(path).expanduser()
    p = p if p.is_absolute() else base / p
    real = p.resolve()
    for root in roots:
        try:
            real.relative_to(root.expanduser().resolve())
            return real
        except ValueError:
            continue
    allowed = ", ".join(str(r) for r in roots) or "(none)"
    raise UnsafeInput(
        f"{path}: outside the folders this run may read ({allowed}). "
        "Move the file next to the spec, or pass --allow-path DIR."
    )


def file_url_to_path(url: str) -> str:
    u = urlparse(url)
    if u.netloc not in ("", "localhost"):
        raise UnsafeInput(f"file URL with a host is not allowed: {url}")
    return url2pathname(unquote(u.path))


def _ips_for(host: str) -> list[ipaddress._BaseAddress]:
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0].split("%")[0]))
        except ValueError:
            continue
    return out


def pinned_ips(host: str, allow_private: bool) -> list[str]:
    """Resolve `host` once, validate every address, and return the addresses that may be connected to.

    Callers must connect to a returned address, not re-resolve the name (that is the DNS-rebinding gap)."""
    if host.lower() in ("metadata.google.internal", "metadata"):
        raise UnsafeInput(f"cloud metadata host refused: {host}")
    ips = _ips_for(host)
    if not ips:
        raise UnsafeInput(f"cannot resolve host: {host}")
    out: list[str] = []
    for ip in ips:
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise UnsafeInput(f"{host} resolves to a link-local or unspecified address ({ip}); refused")
        if ip.is_loopback or ip.is_private:  # checked before "reserved": ::1 counts as reserved in Python
            if not allow_private:
                raise UnsafeInput(
                    f"{host} resolves to a private or loopback address ({ip}). "
                    "To capture a local dev server pass --allow-private-hosts (or set it in your user config)."
                )
        elif ip.is_reserved:
            raise UnsafeInput(f"{host} resolves to a reserved address ({ip}); refused")
        out.append(str(ip))
    return out


def check_http_url(url: str, allow_private: bool) -> None:
    """Allow http(s) URLs. Link-local and cloud-metadata addresses are always refused; loopback and
    private ranges only with allow_private (a local dev server is a normal thing to capture)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise UnsafeInput(f"only http(s) URLs can be captured: {url}")
    if not u.hostname:
        raise UnsafeInput(f"URL has no host: {url}")
    pinned_ips(u.hostname, allow_private)
