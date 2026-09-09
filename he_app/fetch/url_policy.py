"""Validate discovered links before requesting them; never infer mirror relationships."""
import ipaddress
from urllib.parse import urljoin, urlsplit

from he_app.domain.errors import SiteScrapeFailure


def url_origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("only absolute HTTP(S) URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URL credentials are not allowed")
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("local host is not allowed")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("non-public IP address is not allowed")
        return parsed.scheme, host, parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise SiteScrapeFailure("站点身份错误", f"非法来源URL: {url}: {exc}") from exc


def same_origin_url(base: str, href: str) -> str:
    target = urljoin(base, href.strip())
    if url_origin(base) != url_origin(target):
        raise SiteScrapeFailure("站点身份错误", f"拒绝未授权跨源地址: {target}")
    return target
