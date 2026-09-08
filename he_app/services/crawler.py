from threading import Lock
from urllib.parse import urlparse, urlunparse

import requests

from he_app.domain.errors import SiteScrapeFailure
from he_app.domain.models import Site
from he_app.fetch.browser import BrowserClient, BrowserPool
from he_app.fetch.discovery import collect_documents
from he_app.fetch.http import create_session
from he_app.parsers.dedicated.tables import site_rule
from he_app.services.document_sources import (
    collect_special_site_browser_documents,
    collect_special_site_documents,
)
from he_app.services.single_period import evaluate_site_period


Outcome = tuple[int, Site, str | None, str, str | None, list[str], str | None]

def build_mirror_urls(site: Site, all_sites: list[Site], limit: int) -> list[str]:
    if limit <= 0 or "/topic/" not in site.url:
        return []
    base = urlparse(site.url)
    result = []
    for other in all_sites:
        parsed = urlparse(other.url)
        if not parsed.netloc or parsed.netloc == base.netloc:
            continue
        url = urlunparse((parsed.scheme, parsed.netloc, base.path, "", base.query, ""))
        if url not in result:
            result.append(url)
        if len(result) >= limit:
            break
    return result

def build_mirror_url_map(sites: list[Site], limit: int) -> dict[int, list[str]]:
    return {i: build_mirror_urls(site, sites, limit) for i, site in enumerate(sites)}


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
        rule = site_rule(site)
        documents = browser.get_documents(
            site.url,
            period,
            site.click_first,
            timeout,
            rule.browser_wait_selector,
            rule.browser_wait_anchor,
        )
    else:
        documents = collect_documents(session, site.url, timeout)
    return evaluate_site_period(site, period, documents)


def scrape_site_with_browser(
    session: requests.Session, site: Site, period: int, timeout: int, browser: BrowserClient
) -> tuple[str | None, str, list[str], str | None]:
    try:
        documents = collect_special_site_browser_documents(browser, site, timeout, period)
    except SiteScrapeFailure as exc:
        return None, exc.reason, [], exc.category
    if documents is not None:
        return evaluate_site_period(site, period, documents)
    try:
        documents = collect_special_site_documents(session, site, timeout, period)
    except SiteScrapeFailure as exc:
        return None, exc.reason, [], exc.category
    if documents is None:
        rule = site_rule(site)
        documents = browser.get_documents(
            site.url,
            period,
            site.click_first,
            timeout,
            rule.browser_wait_selector,
            rule.browser_wait_anchor,
        )
    return evaluate_site_period(site, period, documents)


def scrape_http_site(
    index: int,
    site: Site,
    period: int,
    timeout: int,
    host_locks: dict[str, Lock] | None = None,
    mirror_urls: list[str] | None = None,
) -> Outcome:
    try:
        with create_session(host_locks) as session:
            result = detail = None
            rank_values, previous_reason = [], None
            for url in [site.url, *(mirror_urls or [])]:
                attempt = site if url == site.url else Site(site.name, url, site.pick, False, site.click_first, site.site_id)
                result, detail, rank_values, previous_reason = scrape_site(session, attempt, period, timeout)
                if result:
                    break
        return index, site, result, detail, None, rank_values, previous_reason
    except Exception as exc:
        return index, site, None, "", f"{type(exc).__name__}: {exc}", [], None


def scrape_parallel_site(
    index: int,
    site: Site,
    period: int,
    timeout: int,
    show_browser: bool,
    host_locks: dict[str, Lock] | None = None,
    mirror_urls: list[str] | None = None,
    browser_pool: BrowserPool | None = None,
) -> Outcome:
    try:
        if site.browser:
            owns_pool = browser_pool is None
            pool = browser_pool or BrowserPool(1, headless=not show_browser)
            if owns_pool:
                pool.start()
            try:
                with create_session(host_locks) as session:
                    result, detail, error, rank_values, previous_reason = pool.run(
                        lambda browser: invoke_browser_scrape(session, site, period, timeout, browser)
                    )
            finally:
                if owns_pool:
                    pool.close()
            return index, site, result, detail, error, rank_values, previous_reason
        return scrape_http_site(index, site, period, timeout, host_locks, mirror_urls)
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
