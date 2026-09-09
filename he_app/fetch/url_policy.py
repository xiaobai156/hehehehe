"""Origin and DNS policy for every discovered or configured network target.

The policy rejects local/special addresses before a request and verifies the
actual connected peer when the transport exposes it.  Worker-process hard
limits provide the deadline for DNS and socket operations themselves.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlsplit

from he_app.domain.errors import SiteScrapeFailure


@dataclass(frozen=True, slots=True)
class ResolvedOrigin:
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]

    @property
    def origin(self) -> tuple[str, str, int]:
        return self.scheme, self.host, self.port


def _validated_public_ip(raw: str, *, context: str) -> str:
    try:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError as exc:
        raise SiteScrapeFailure("站点身份错误", f"{context}不是有效IP地址: {raw}") from exc
    if not address.is_global:
        raise SiteScrapeFailure("站点身份错误", f"{context}解析到非公网地址: {address}")
    return address.compressed


def url_origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("only absolute HTTP(S) URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URL credentials are not allowed")
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home", ".lan")):
            raise ValueError("local host is not allowed")
        if host.endswith(".arpa"):
            raise ValueError("reverse/special-use host is not allowed")
        try:
            _validated_public_ip(host, context="URL")
        except SiteScrapeFailure:
            # A syntactically valid non-public literal must remain rejected;
            # a hostname simply continues to DNS validation later.
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise
        return parsed.scheme, host, parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise SiteScrapeFailure("站点身份错误", f"非法来源URL: {url}: {exc}") from exc


def same_origin_url(base: str, href: str) -> str:
    target = urljoin(base, href.strip())
    if url_origin(base) != url_origin(target):
        raise SiteScrapeFailure("站点身份错误", f"拒绝未授权跨源地址: {target}")
    return target


def resolve_public_origin(url: str) -> ResolvedOrigin:
    scheme, host, port = url_origin(url)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        address = _validated_public_ip(host, context="URL")
        return ResolvedOrigin(scheme, host, port, (address,))

    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SiteScrapeFailure("请求失败", f"DNS解析失败: {host}: {exc}") from exc

    addresses: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in records:
        raw = str(sockaddr[0])
        address = _validated_public_ip(raw, context=f"DNS {host}")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise SiteScrapeFailure("请求失败", f"DNS没有返回可用公网地址: {host}")
    return ResolvedOrigin(scheme, host, port, tuple(addresses))


def _iter_response_sockets(response: object) -> Iterable[object]:
    raw = getattr(response, "raw", None)
    if raw is None:
        return ()
    candidates = [
        getattr(getattr(raw, "_connection", None), "sock", None),
        getattr(getattr(raw, "connection", None), "sock", None),
        getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None),
    ]
    sockets: list[object] = []
    for candidate in candidates:
        if candidate is None:
            continue
        candidate = getattr(candidate, "_sock", candidate)
        if hasattr(candidate, "getpeername") and candidate not in sockets:
            sockets.append(candidate)
    return sockets


def connected_peer_ip(response: object) -> str | None:
    for sock in _iter_response_sockets(response):
        try:
            peer = sock.getpeername()
        except OSError:
            continue
        if isinstance(peer, tuple) and peer:
            return str(peer[0]).split("%", 1)[0]
    return None


def verify_public_peer(response: object, resolved: ResolvedOrigin, *, require_peer: bool = True) -> str | None:
    peer = connected_peer_ip(response)
    if peer is None:
        if require_peer:
            raise SiteScrapeFailure("站点身份错误", f"无法验证实际连接地址: {resolved.host}")
        return None
    verified = _validated_public_ip(peer, context=f"连接 {resolved.host}")
    if verified not in resolved.addresses:
        raise SiteScrapeFailure(
            "站点身份错误",
            f"实际连接地址不在请求前DNS结果中: {resolved.host} -> {verified}",
        )
    return verified


class StrictNetworkPolicy:
    """Small injectable policy used by requests and Selenium clients."""

    def __init__(self, *, require_peer: bool = True):
        self.require_peer = require_peer

    def resolve(self, url: str) -> ResolvedOrigin:
        return resolve_public_origin(url)

    def verify_response(self, response: object, resolved: ResolvedOrigin) -> str | None:
        return verify_public_peer(response, resolved, require_peer=self.require_peer)

    def verify_address(
        self,
        raw: str,
        *,
        context: str = "浏览器连接",
        resolved: ResolvedOrigin | None = None,
    ) -> str:
        verified = _validated_public_ip(raw, context=context)
        if resolved is not None and verified not in resolved.addresses:
            raise SiteScrapeFailure(
                "站点身份错误",
                f"实际连接地址不在请求前DNS结果中: {resolved.host} -> {verified}",
            )
        return verified
