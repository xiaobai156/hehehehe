from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests


_REDIRECT_CODES = {301, 302, 303, 307, 308}
_FORBIDDEN_HOST_SUFFIXES = (
    ".local",
    ".localhost",
    ".internal",
    ".home",
    ".lan",
)


class NetworkPolicyError(requests.RequestException):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]

    @property
    def origin(self) -> tuple[str, str, int]:
        return self.scheme, self.host, self.port


def strict_network_policy_enabled() -> bool:
    return os.environ.get("HE_NETWORK_POLICY", "strict").strip().lower() not in {
        "0",
        "false",
        "off",
        "disabled",
    }


def _allow_loopback_for_tests() -> bool:
    return os.environ.get("HE_ALLOW_LOOPBACK_TESTS", "").strip() == "1"


def _is_allowed_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if _allow_loopback_for_tests() and ip.is_loopback:
        return True
    return ip.is_global


def validate_http_url(url: str) -> tuple[str, str, int]:
    parsed = urlparse(str(url))
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise NetworkPolicyError(f"unsupported URL scheme: {scheme or 'empty'}")
    if parsed.username is not None or parsed.password is not None:
        raise NetworkPolicyError("URLs containing credentials are forbidden")
    host = (parsed.hostname or "").rstrip(".").casefold()
    if not host:
        raise NetworkPolicyError("URL has no host")
    if host == "localhost" or host.endswith(_FORBIDDEN_HOST_SUFFIXES):
        if not _allow_loopback_for_tests():
            raise NetworkPolicyError(f"local host is forbidden: {host}")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise NetworkPolicyError(f"invalid URL port: {url}") from exc
    if not 1 <= port <= 65535:
        raise NetworkPolicyError(f"invalid URL port: {port}")

    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None and not _is_allowed_address(str(literal)):
        raise NetworkPolicyError(f"non-public literal IP is forbidden: {literal}")
    return scheme, host, port


def resolve_public_target(url: str) -> ResolvedTarget:
    scheme, host, port = validate_http_url(url)
    try:
        records = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise NetworkPolicyError(f"DNS resolution failed for {host}: {exc}") from exc

    addresses: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in records:
        address = str(sockaddr[0]).split("%", 1)[0]
        try:
            normalized = str(ipaddress.ip_address(address))
        except ValueError as exc:
            raise NetworkPolicyError(f"DNS returned an invalid IP for {host}: {address}") from exc
        if normalized not in addresses:
            addresses.append(normalized)
    if not addresses:
        raise NetworkPolicyError(f"DNS returned no usable addresses for {host}")
    forbidden = [address for address in addresses if not _is_allowed_address(address)]
    if forbidden:
        raise NetworkPolicyError(
            f"DNS for {host} returned forbidden address(es): {', '.join(forbidden)}"
        )
    return ResolvedTarget(scheme, host, port, tuple(addresses))


def _response_socket(response: requests.Response):
    candidates = [
        getattr(getattr(response.raw, "_connection", None), "sock", None),
        getattr(getattr(getattr(response.raw, "_fp", None), "fp", None), "raw", None),
    ]
    raw_candidate = candidates[1]
    if raw_candidate is not None:
        candidates.append(getattr(raw_candidate, "_sock", None))
    for candidate in candidates:
        if candidate is not None and hasattr(candidate, "getpeername"):
            return candidate
    return None


def response_peer_ip(response: requests.Response) -> str:
    sock = _response_socket(response)
    if sock is None:
        raise NetworkPolicyError("unable to verify the connected peer address")
    try:
        peer = sock.getpeername()[0]
        return str(ipaddress.ip_address(str(peer).split("%", 1)[0]))
    except (OSError, ValueError, TypeError, IndexError) as exc:
        raise NetworkPolicyError(f"unable to read the connected peer address: {exc}") from exc


def verify_response_peer(response: requests.Response, target: ResolvedTarget) -> str:
    peer = response_peer_ip(response)
    if not _is_allowed_address(peer):
        raise NetworkPolicyError(f"connected peer is not public: {peer}")
    if peer not in target.addresses:
        raise NetworkPolicyError(
            f"connected peer {peer} was not in the pre-resolved DNS set for {target.host}"
        )
    return peer


def same_origin(left: str, right: str) -> bool:
    try:
        return validate_http_url(left) == validate_http_url(right)
    except NetworkPolicyError:
        return False


def resolve_browser_pin(url: str) -> tuple[str, str]:
    target = resolve_public_target(url)
    preferred = next(
        (address for address in target.addresses if ipaddress.ip_address(address).version == 4),
        target.addresses[0],
    )
    return target.host, preferred


class NetworkPolicySession(requests.Session):
    """A requests session that fails closed on redirects, DNS rebinding and size.

    The host is resolved before connecting.  The actual socket peer must be one
    of those addresses, every redirect is resolved again, and response bodies
    are capped before callers receive them.
    """

    def __init__(self, *, max_response_bytes: int | None = None, max_redirects: int = 3):
        super().__init__()
        self.trust_env = False
        configured_limit = os.environ.get("HE_MAX_RESPONSE_BYTES", "").strip()
        if max_response_bytes is None and configured_limit.isdigit():
            max_response_bytes = int(configured_limit)
        self.max_response_bytes = max_response_bytes or 8 * 1024 * 1024
        self.policy_max_redirects = max(0, max_redirects)
        self.last_network_evidence: dict[str, object] = {}

    def request(self, method: str, url: str, **kwargs):  # type: ignore[override]
        if not strict_network_policy_enabled():
            return super().request(method, url, **kwargs)

        requested_url = str(url)
        current_url = requested_url
        original_origin: tuple[str, str, int] | None = None
        redirect_chain: list[str] = []
        kwargs.pop("allow_redirects", None)
        kwargs["stream"] = True

        for redirect_index in range(self.policy_max_redirects + 1):
            target = resolve_public_target(current_url)
            if original_origin is None:
                original_origin = target.origin
            elif target.origin != original_origin:
                raise NetworkPolicyError(
                    f"cross-origin redirect is forbidden: {requested_url} -> {current_url}"
                )

            response = super().request(
                method,
                current_url,
                allow_redirects=False,
                **kwargs,
            )
            try:
                peer = verify_response_peer(response, target)
                if response.status_code in _REDIRECT_CODES:
                    if redirect_index >= self.policy_max_redirects:
                        raise NetworkPolicyError(
                            f"redirect limit exceeded for {requested_url}"
                        )
                    location = response.headers.get("Location", "").strip()
                    if not location:
                        raise NetworkPolicyError(
                            f"redirect response has no Location header: {current_url}"
                        )
                    next_url = urljoin(current_url, location)
                    next_target = resolve_public_target(next_url)
                    if next_target.origin != original_origin:
                        raise NetworkPolicyError(
                            f"cross-origin redirect is forbidden: {current_url} -> {next_url}"
                        )
                    redirect_chain.append(next_url)
                    response.close()
                    current_url = next_url
                    continue

                content_length = response.headers.get("Content-Length", "").strip()
                if content_length.isdigit() and int(content_length) > self.max_response_bytes:
                    raise NetworkPolicyError(
                        f"response is larger than {self.max_response_bytes} bytes"
                    )
                body = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    body.extend(chunk)
                    if len(body) > self.max_response_bytes:
                        raise NetworkPolicyError(
                            f"response exceeded {self.max_response_bytes} bytes"
                        )
                response._content = bytes(body)
                response._content_consumed = True
                response.url = current_url
                self.last_network_evidence = {
                    "requested_url": requested_url,
                    "final_url": current_url,
                    "resolved_addresses": list(target.addresses),
                    "peer_ip": peer,
                    "redirect_chain": list(redirect_chain),
                    "response_bytes": len(body),
                }
                return response
            except Exception:
                response.close()
                raise

        raise NetworkPolicyError(f"request did not complete: {requested_url}")


__all__ = [
    "NetworkPolicyError",
    "NetworkPolicySession",
    "ResolvedTarget",
    "resolve_browser_pin",
    "resolve_public_target",
    "response_peer_ip",
    "same_origin",
    "strict_network_policy_enabled",
    "validate_http_url",
    "verify_response_peer",
]
