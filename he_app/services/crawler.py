import inspect
import re
from threading import Lock
from urllib.parse import parse_qs, urlparse, urlunparse

import requests

from he_app.domain.errors import SiteScrapeFailure
from he_app.domain.models import Site
from he_app.fetch.browser import BrowserClient, BrowserPool
from he_app.fetch.discovery import collect_documents_from_page, collect_page_documents
from he_app.fetch.http import create_session
from he_app.parsers.common import (
    analyze_missing_reason,
    build_candidate_parts,
    diagnose_candidate_state,
    extract_values,
    find_candidate,
)
from he_app.parsers.dedicated.tables import site_rule
from he_app.services.document_sources import collect_special_site_documents
from he_app.services.single_period import evaluate_site_period
from he_app.storage.success_cache import normalize_site_name


Outcome = tuple[int, Site, str | None, str, str | None, list[str], str | None]


def default_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    host_name = parsed.hostname.split(".")[0] if parsed.hostname else "site"
    topic_match = re.search(r"/topic/(\d+)\.html", parsed.path)
    if topic_match:
        return f"{host_name}_topic_{topic_match.group(1)}"
    query = parse_qs(parsed.query)
    return f"{host_name}_tid_{query['tid'][0]}" if query.get("tid") else host_name


def build_mirror_urls(site: Site, all_sites: list[Site], limit: int) -> list[str]:
    if limit <= 0:
        return []
    parsed = urlparse(site.url)
    if not parsed.scheme or not parsed.netloc or not parsed.path.startswith("/topic/"):
        return []
    mirrors: list[str] = []
    seen = {site.url}
    for other in all_sites:
        other_parsed = urlparse(other.url)
        if not other_parsed.scheme or not other_parsed.netloc or other_parsed.netloc == parsed.netloc:
            continue
        candidate = urlunparse((other_parsed.scheme, other_parsed.netloc, parsed.path, "", parsed.query, ""))
        if candidate in seen:
            continue
        seen.add(candidate)
        mirrors.append(candidate)
        if len(mirrors) >= limit:
            break
    return mirrors


def build_mirror_url_map(sites: list[Site], limit: int) -> dict[int, list[str]]:
    return {index: build_mirror_urls(site, sites, limit) for index, site in enumerate(sites)}


def apply_cached_urls(sites: list[Site], cached_urls: dict[str, str]) -> list[Site]:
    return sites


def filter_sites_for_debug(sites: list[Site], query: str) -> list[tuple[int, Site]]:
    query = normalize_site_name(query.strip())
    if not query:
        return list(enumerate(sites))
    return [
        (index, site)
        for index, site in enumerate(sites)
        if query in normalize_site_name(site.name) or query in normalize_site_name(site.site_id)
    ]


def scrape_site(
    session: requests.Session,
    site: Site,
    period: int,
    timeout: int,
    browser: BrowserClient | None = None,
) -> tuple[str | None, str, list[str], str | None]:
    try:
        documents = collect_special_site_documents(session, site, timeout, period)
    except SiteScrapeFailure as exc:
        return None, exc.reason, [], exc.category
    if documents is not None:
        return evaluate_site_period(site, period, documents)

    if site.browser:
        if browser is None:
            raise RuntimeError("browser client is required")
        documents = browser.get_documents(site.url, period, site.click_first, timeout)
    else:
        documents, page_html = collect_page_documents(session, site.url, timeout)
        documents = collect_documents_from_page(session, site.url, timeout, documents, page_html)
    return evaluate_site_period(site, period, documents)


def build_debug_report(site: Site, period: int, documents: list[str]) -> list[str]:
    rule = site_rule(site)
    parts = build_candidate_parts(documents, period)
    state = diagnose_candidate_state(documents, period)
    candidate = find_candidate(
        documents,
        period,
        site.pick,
        require_body_locator=rule.require_body_locator,
        allow_weak=rule.allow_weak_kill_sum_keyword,
    )
    lines = [
        f"[DEBUG] {site.name} {site.site_id}",
        f"URL: {site.url}",
        f"pick: {site.pick}",
        f"rule.require_body_locator: {rule.require_body_locator}",
        f"documents: {len(documents)}",
        f"state: {state}",
        f"strict_parts: {len(parts)}",
    ]
    for index, (line, has_locator) in enumerate(parts[:20], 1):
        lines.append(f"candidate#{index} locator={has_locator} values={extract_values(line)} line={line[:260]}")
    if candidate:
        lines.append(f"selected: {candidate.values} line={candidate.line[:260]}")
    else:
        category, reason = analyze_missing_reason(documents, period, site.pick, rule.allow_weak_kill_sum_keyword)
        lines.append(f"selected: None category={category} reason={reason}")
    return lines


def scrape_site_with_browser(
    session: requests.Session, site: Site, period: int, timeout: int, browser: BrowserClient
) -> tuple[str | None, str, list[str], str | None]:
    try:
        documents = collect_special_site_documents(session, site, timeout, period)
    except SiteScrapeFailure as exc:
        return None, exc.reason, [], exc.category
    if documents is None:
        documents = browser.get_documents(site.url, period, site.click_first, timeout)
    return evaluate_site_period(site, period, documents)


def scrape_http_site(
    index: int,
    site: Site,
    period: int,
    timeout: int,
    mirror_urls: list[str] | None = None,
    host_locks: dict[str, Lock] | None = None,
) -> Outcome:
    last_error = None
    for attempt_url in [site.url, *list(mirror_urls or [])]:
        attempt_site = site if attempt_url == site.url else Site(
            site.name, attempt_url, site.pick, False, site.click_first, site.site_id
        )
        if attempt_url == site.url and site.browser:
            attempt_site = Site(site.name, site.url, site.pick, False, site.click_first, site.site_id)
        try:
            result, detail, rank_values, previous_reason = scrape_site(
                create_session(host_locks), attempt_site, period, timeout
            )
            return index, site, result, detail, None, rank_values, previous_reason
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return index, site, None, "", last_error or "未找到可用地址", [], None


def scrape_parallel_site(
    index: int,
    site: Site,
    period: int,
    timeout: int,
    show_browser: bool,
    mirror_urls: list[str] | None = None,
    host_locks: dict[str, Lock] | None = None,
    browser_pool: BrowserPool | None = None,
) -> Outcome:
    try:
        if site.browser:
            owns_pool = browser_pool is None
            pool = browser_pool or BrowserPool(1, headless=not show_browser)
            if owns_pool:
                pool.start()
            try:
                result, detail, error, rank_values, previous_reason = pool.run(
                    lambda browser: invoke_browser_scrape(create_session(host_locks), site, period, timeout, browser)
                )
            finally:
                if owns_pool:
                    pool.close()
            return index, site, result, detail, error, rank_values, previous_reason
        return scrape_http_site(index, site, period, timeout, mirror_urls, host_locks)
    except Exception as exc:
        return index, site, None, "", f"{type(exc).__name__}: {exc}", [], None


def scrape_browser_site(
    session: requests.Session, site: Site, period: int, timeout: int, browser: BrowserClient
) -> tuple[str | None, str, str | None, list[str], str | None]:
    try:
        result, detail, rank_values, previous_reason = scrape_site_with_browser(session, site, period, timeout, browser)
        return result, detail, None, rank_values, previous_reason
    except Exception as exc:
        return None, "", f"{type(exc).__name__}: {exc}", [], None


def invoke_browser_scrape(
    session: requests.Session, site: Site, period: int, timeout: int, browser: BrowserClient
) -> tuple[str | None, str, str | None, list[str], str | None]:
    if len(inspect.signature(scrape_browser_site).parameters) >= 6:
        return scrape_browser_site(session, site, period, timeout, browser, 0)
    return scrape_browser_site(session, site, period, timeout, browser)


def print_outcome(site: Site, result: str | None, detail: str, error: str | None, progress_prefix: str = "") -> None:
    prefix = f"{progress_prefix} " if progress_prefix else ""
    if result:
        print(f"{prefix}[OK] {result}")
        print(f"     {detail}")
    elif error:
        print(f"{prefix}[FAIL] {site.name}: {error}")
    else:
        print(f"{prefix}[FAIL] {site.name}: {detail}")
