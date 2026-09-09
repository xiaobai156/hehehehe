"""HTTP-first fetch with verified redirects, bounded bodies and one curl fallback.

Requests' socket timeouts are NOT hard wall-clock task deadlines. The deadline
here is cooperative; isolated worker deadlines bound complete duplicate jobs.
"""
import codecs
import re
import shutil
import subprocess
import time
from contextlib import nullcontext
from threading import Lock
from urllib.parse import urlparse

import requests

from he_app.domain.models import Site
from he_app.fetch.url_policy import StrictNetworkPolicy, same_origin_url, url_origin

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
CURL_VARIANT_BY_HOST: dict[str, int] = {}
CURL_VARIANT_MEMORY_LOCK = Lock()


class FetchedText(str):
    def __new__(cls, text: str, *, final_url: str, status_code: int,
                content_type: str = ""):
        value = super().__new__(cls, text)
        value.final_url = final_url
        value.status_code = status_code
        value.content_type = content_type
        return value


def host_key(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or url


def build_host_locks(sites: list[Site], mirror_url_map: dict[int, list[str]] | None = None) -> dict[str, Lock]:
    urls = [site.url for site in sites]
    if mirror_url_map:
        urls.extend(url for mirrors in mirror_url_map.values() for url in mirrors)
    return {host_key(url): Lock() for url in urls}


def create_session(host_locks: dict[str, Lock] | None = None) -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    session.trust_env = False
    session.proxies = {}
    session._he_host_locks = host_locks or {}
    session._he_network_policy = StrictNetworkPolicy(require_peer=True)
    return session


def fetch_text(session: requests.Session, url: str, timeout: int) -> str:
    url_origin(url)
    if timeout <= 0:
        raise ValueError("timeout 必须为正数")
    lock = getattr(session, "_he_host_locks", {}).get(host_key(url))
    with lock if lock is not None else nullcontext():
        deadline = time.monotonic() + timeout
        try:
            return fetch_text_fast(session, url, timeout)
        except (requests.exceptions.SSLError, requests.exceptions.Timeout):
            raise
        except requests.exceptions.ConnectionError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HTTP请求预算已耗尽")
            # Curl is a transport compatibility fallback, never a TLS bypass.
            policy = getattr(session, "_he_network_policy", StrictNetworkPolicy(require_peer=False))
            return fetch_text_with_curl(url, remaining, policy.resolve(url))


def is_deterministic_http_error(exc: Exception) -> bool:
    match = re.search(r"HTTP Error (\d{3})", str(exc), re.I)
    return bool(match and 400 <= int(match.group(1)) < 500
                and int(match.group(1)) not in {408, 409, 425, 429})


def fetch_text_fast(session: requests.Session, url: str, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    current = url
    policy = getattr(session, "_he_network_policy", None)
    for _ in range(6):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP请求预算已耗尽")
        resolved = policy.resolve(current) if policy is not None else None
        with session.get(current, timeout=remaining, allow_redirects=False, stream=True) as response:
            peer_ip = policy.verify_response(response, resolved) if policy is not None else None
            final_url = same_origin_url(url, response.url)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise RuntimeError("HTTP重定向缺少Location")
                current = same_origin_url(url, same_origin_url(final_url, location))
                continue
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f"HTTP Error {response.status_code}: {http_status_label(response.status_code)}")
            content_type = response.headers.get("Content-Type", "")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=16384):
                if time.monotonic() >= deadline:
                    raise TimeoutError("HTTP响应读取预算已耗尽")
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("HTTP响应超过8MiB上限")
            fetched = FetchedText(decode_response_bytes(bytes(body), content_type),
                                  final_url=final_url, status_code=response.status_code,
                                  content_type=content_type)
            fetched.peer_ip = peer_ip
            return fetched
    raise RuntimeError("HTTP重定向次数超过限制")


def curl_base_command(url: str, timeout: float, resolved=None) -> list[str]:
    executable = shutil.which("curl.exe") or shutil.which("curl")
    if executable is None:
        raise RuntimeError("未安装curl，无法使用兼容传输")
    # No -k, -L, protocol downgrade, or automatic retry. A curl-only redirect
    # is rejected rather than followed before its destination can be checked.
    command = [executable, "--proto", "=http,https", "--http1.1", "--max-redirs", "0",
               "--noproxy", "*", "--connect-timeout", str(min(timeout, 10.0)),
               "--max-time", str(timeout), "--max-filesize", str(MAX_RESPONSE_BYTES),
               "-sS", "-A", DEFAULT_HEADERS["User-Agent"]]
    if resolved is not None:
        command.extend(["--resolve", f"{resolved.host}:{resolved.port}:{resolved.addresses[0]}"])
    return command


def curl_command_variants(url: str, timeout: float, resolved=None) -> list[list[str]]:
    return [curl_base_command(url, timeout, resolved) + ["--compressed", url]]


def split_curl_http_status(raw: bytes) -> tuple[bytes, int | None]:
    marker = b"\n__HTTP_STATUS__:"
    if marker not in raw:
        return raw, None
    body, status = raw.rsplit(marker, 1)
    return (body, int(status.strip())) if re.fullmatch(rb"\d{3}", status.strip()) else (body, None)


def should_try_next_curl_variant(returncode: int, status: int | None, message: str) -> bool:
    return False


def reset_curl_variant_memory() -> None:
    with CURL_VARIANT_MEMORY_LOCK:
        CURL_VARIANT_BY_HOST.clear()


def fetch_text_with_curl(url: str, timeout: float, resolved=None) -> str:
    url_origin(url)
    resolved = resolved or StrictNetworkPolicy(require_peer=False).resolve(url)
    command = curl_command_variants(url, timeout, resolved)[0]
    command = command[:-1] + ["-w", "\n__HTTP_STATUS__:%{http_code}", command[-1]]
    result = subprocess.run(command, capture_output=True, check=False, timeout=timeout)
    body, status = split_curl_http_status(result.stdout)
    if result.returncode:
        # Lossy decoding is only for diagnostics, never for candidate content.
        message = result.stderr.decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"curl exit {result.returncode}: {message}")
    if status is None or not 200 <= status < 300:
        raise RuntimeError(f"HTTP Error {status}: curl未取得有效2xx响应")
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError("curl响应超过8MiB上限")
    return FetchedText(decode_response_bytes(body), final_url=url, status_code=status)


def http_status_label(status: int | None) -> str:
    return {502: "Bad Gateway", 503: "Service Unavailable", 520: "Unknown Error",
            521: "Web Server Is Down", 522: "Connection Timed Out",
            523: "Origin Is Unreachable", 524: "A Timeout Occurred"}.get(status, "HTTP request failed")


def parse_charset_candidates(content_type: str) -> list[str]:
    match = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_.-]+)", content_type or "", re.I)
    return [match.group(1)] if match else []


def decode_response_bytes(data: bytes, content_type: str = "") -> str:
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    candidates = parse_charset_candidates(content_type)
    head = data[:4096].decode("ascii", errors="ignore")
    for meta in re.findall(r"<meta\b[^>]*>", head, re.I):
        candidates.extend(parse_charset_candidates(meta))
    if candidates:
        for encoding in dict.fromkeys(candidates):
            try:
                return data.decode(encoding, errors="strict")
            except (LookupError, UnicodeDecodeError):
                continue
        raise ValueError(f"响应声明的字符编码无法无损解码: {candidates}")
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        alternatives = set()
        for encoding in ("gb18030", "big5"):
            try:
                alternatives.add(data.decode(encoding, errors="strict"))
            except UnicodeDecodeError:
                pass
        if len(alternatives) == 1:
            return alternatives.pop()
        raise ValueError("响应编码不明确，拒绝有损或歧义解码") from None
