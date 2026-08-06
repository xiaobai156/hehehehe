from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from he_app.config.sites import load_sites
from he_app.domain.models import Site
from he_app.fetch.browser import BrowserClient
from he_app.fetch.discovery import decode_strdecode_blocks
from he_app.fetch.http import create_session, fetch_text
from he_app.parsers.common import candidate_chunks, has_kill_sum_keyword
from he_app.parsers.common import period_numbers_in_text
from he_app.parsers.common import extract_values
from he_app.parsers.dedicated.tables import site_rule
from he_app.services.document_sources import collect_special_site_documents


PERIOD = 212
TIMEOUT = 12
IFRAME_TIMEOUT = 8
SCRIPT_TIMEOUT = 6
MAX_CHILD_DEPTH = 2
PERIOD_RE = re.compile(r"(?<!\d)212\s*期")
VALUE_RE = re.compile(r"(?<!\d)(0?[1-9]|1[0-3])\s*合(?!数)")
KILL_MARKER_RE = re.compile(
    r"绝\s*杀|絕\s*殺|公式\s*(?:杀|殺)\s*合|(?:稳|穩)\s*(?:杀|殺)|(?:杀|殺)\s*合|女人味",
    re.I,
)


def normalize_text(value: str) -> str:
    value = value or ""
    table = str.maketrans(
        {
            "０": "0",
            "１": "1",
            "２": "2",
            "３": "3",
            "４": "4",
            "５": "5",
            "６": "6",
            "７": "7",
            "８": "8",
            "９": "9",
            "：": ":",
            "；": ";",
            "，": ",",
            "［": "[",
            "］": "]",
            "【": "[",
            "】": "]",
        }
    )
    return " ".join(value.translate(table).split())


def compact(value: str) -> str:
    return re.sub(r"\s+", "", normalize_text(value))


def permissive_values(text: str) -> list[str]:
    values: list[str] = []
    for match in VALUE_RE.finditer(normalize_text(text)):
        value = int(match.group(1))
        item = f"{value:02d}合"
        if item not in values:
            values.append(item)
    if values:
        return values
    try:
        return extract_values(text, allow_weak=True)
    except Exception:
        return []


def row_candidates(text: str) -> list[tuple[str, tuple[str, ...]]]:
    if not text:
        return []
    chunks: list[str] = []
    try:
        chunks.extend(candidate_chunks(text, PERIOD))
    except Exception:
        pass
    soup = BeautifulSoup(text, "html.parser")
    visible = soup.get_text("\n", strip=True) if soup.find() else text
    lines = [normalize_text(line) for line in visible.splitlines() if normalize_text(line)]
    for index, line in enumerate(lines):
        if PERIOD_RE.search(line):
            chunks.append(line)
            chunks.append(normalize_text(" ".join(lines[index : index + 7])))
    if not chunks:
        chunks.append(text)

    rows: list[tuple[str, tuple[str, ...]]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for chunk in chunks:
        normalized = normalize_text(chunk)
        if not PERIOD_RE.search(normalized):
            continue
        periods = period_numbers_in_text(normalized)
        if not periods or any(item != PERIOD for item in periods):
            continue
        if not (has_kill_sum_keyword(normalized, allow_weak=True) or KILL_MARKER_RE.search(normalized)):
            continue
        values = permissive_values(normalized)
        if not 1 <= len(values) <= 2:
            continue
        line = normalized[:400]
        key = (compact(line), tuple(values))
        if key in seen:
            continue
        seen.add(key)
        rows.append((line, tuple(values)))
    return rows


def snapshot(path: Path) -> tuple[bool, str, int | None]:
    if not path.exists():
        return False, "", None
    data = path.read_bytes()
    return True, hashlib.sha256(data).hexdigest(), path.stat().st_mtime_ns


def source_page_key(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()


class SiteAudit:
    def __init__(self, site: Site):
        self.site = site
        self.units: list[dict[str, str]] = []
        self.visited_pages: set[str] = set()
        self.visited_scripts: set[str] = set()
        self.visited_iframes: set[str] = set()
        self.special_seen: set[tuple[str, str]] = set()
        self.errors: list[str] = []
        self.page_count = 0
        self.script_count = 0
        self.iframe_count = 0
        self.browser_ok = not site.browser
        self.initial_page_ok = False
        self.coverage = {"title": False, "body": False, "script": False, "iframe": False}

    def add_unit(
        self,
        channel: str,
        text: str,
        source_url: str,
        page_url: str,
        *,
        countable: bool = True,
    ) -> None:
        self.units.append(
            {
                "channel": channel,
                "text": text or "",
                "source_url": source_url or page_url,
                "page_url": page_url,
                "countable": "1" if countable else "0",
            }
        )

    def add_document(self, channel: str, text: str, source_url: str, page_url: str) -> None:
        key = (channel, source_url, hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest())
        if key in self.special_seen:
            return
        self.special_seen.add(key)
        self.add_unit(channel, text, source_url, page_url)

    def source_rows(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for unit in self.units:
            if unit["countable"] != "1":
                continue
            for line, values in row_candidates(unit["text"]):
                rows.append(
                    {
                        "channel": unit["channel"],
                        "source_url": unit["source_url"],
                        "page_url": unit["page_url"],
                        "line": line,
                        "values": values,
                    }
                )
        return rows

    def logical_rows(self) -> list[dict[str, object]]:
        rows = self.source_rows()
        unique: dict[tuple[str, str, tuple[str, ...]], dict[str, object]] = {}
        for row in rows:
            key = (source_page_key(str(row["source_url"])), compact(str(row["line"])), tuple(row["values"]))
            unique.setdefault(key, row)
        return list(unique.values())

    def raw_hits(self) -> int:
        return sum(len(PERIOD_RE.findall(normalize_text(unit["text"]))) for unit in self.units if unit["countable"] == "1")


def fetch_resource(session, url: str, timeout: int) -> str:
    return fetch_text(session, url, timeout)


def inspect_html_document(
    audit: SiteAudit,
    session,
    html: str,
    source_url: str,
    page_url: str,
    label: str,
    depth: int = 0,
    *,
    fetch_children: bool = True,
) -> None:
    soup = BeautifulSoup(html or "", "html.parser")
    audit.page_count += 1
    audit.coverage["title"] = True
    audit.coverage["body"] = True
    audit.coverage["script"] = True
    audit.coverage["iframe"] = True
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    body = soup.body.get_text("\n", strip=True) if soup.body else soup.get_text("\n", strip=True)
    audit.add_unit(f"{label}:title", title, source_url, page_url)
    audit.add_unit(f"{label}:body", body, source_url, page_url)

    scripts = soup.find_all("script")
    for index, tag in enumerate(scripts):
        src = str(tag.get("src") or "").strip()
        inline = tag.get_text("\n", strip=False) or ""
        if inline.strip():
            audit.add_unit(f"{label}:script-inline#{index + 1}", inline, source_url, page_url)
            for decoded_index, decoded in enumerate(decode_strdecode_blocks(inline), 1):
                audit.add_unit(f"{label}:script-decoded#{index + 1}.{decoded_index}", decoded, source_url, page_url)
        if not src or not fetch_children:
            continue
        script_url = urljoin(source_url or page_url, src)
        audit.add_unit(f"{label}:script-ref#{index + 1}", script_url, script_url, page_url, countable=False)
        if script_url in audit.visited_scripts:
            continue
        audit.visited_scripts.add(script_url)
        try:
            script_text = fetch_resource(session, script_url, SCRIPT_TIMEOUT)
            audit.script_count += 1
            audit.add_unit(f"{label}:script-src#{index + 1}", script_text, script_url, page_url)
            for decoded_index, decoded in enumerate(decode_strdecode_blocks(script_text), 1):
                audit.add_unit(f"{label}:script-decoded-src#{index + 1}.{decoded_index}", decoded, script_url, page_url)
        except Exception as exc:
            audit.errors.append(f"script {script_url}: {type(exc).__name__}: {exc}")

    for index, frame in enumerate(soup.find_all(["iframe", "frame"]), 1):
        src = str(frame.get("src") or "").strip()
        srcdoc = str(frame.get("srcdoc") or "")
        frame_url = urljoin(source_url or page_url, src) if src else ""
        audit.add_unit(f"{label}:iframe-ref#{index}", frame_url or srcdoc, frame_url or source_url, page_url, countable=False)
        if srcdoc:
            audit.iframe_count += 1
            inspect_html_document(
                audit,
                session,
                srcdoc,
                frame_url or source_url,
                page_url,
                f"{label}:iframe#{index}:srcdoc",
                depth + 1,
                fetch_children=depth + 1 < MAX_CHILD_DEPTH,
            )
            continue
        if not frame_url or frame_url.startswith(("javascript:", "about:", "data:")):
            continue
        if frame_url in audit.visited_iframes or depth >= MAX_CHILD_DEPTH:
            continue
        audit.visited_iframes.add(frame_url)
        try:
            frame_html = fetch_resource(session, frame_url, IFRAME_TIMEOUT)
            audit.iframe_count += 1
            inspect_html_document(
                audit,
                session,
                frame_html,
                frame_url,
                page_url,
                f"{label}:iframe#{index}",
                depth + 1,
                fetch_children=depth + 1 < MAX_CHILD_DEPTH,
            )
        except Exception as exc:
            audit.errors.append(f"iframe {frame_url}: {type(exc).__name__}: {exc}")


def inspect_http_page(audit: SiteAudit, session, url: str, label: str, depth: int = 0) -> bool:
    key = source_page_key(url)
    if key in audit.visited_pages:
        return True
    audit.visited_pages.add(key)
    try:
        html = fetch_resource(session, url, TIMEOUT)
    except Exception as exc:
        audit.errors.append(f"page {url}: {type(exc).__name__}: {exc}")
        return False
    inspect_html_document(audit, session, html, url, url, label, depth)
    return True


def browser_snapshot(browser: BrowserClient, url: str, timeout: int) -> dict[str, object]:
    driver = browser.driver
    if driver is None:
        raise RuntimeError("browser driver unavailable")
    driver.set_page_load_timeout(timeout)
    try:
        driver.get(url)
    except Exception as exc:
        if "timeout" not in str(exc).lower():
            raise
    browser.wait_for_document(min(float(timeout), 6.0))
    script = r"""
    function frameSnapshot(frame) {
      const result = {src: frame.src || frame.getAttribute('src') || '', srcdoc: frame.getAttribute('srcdoc') || '', html: '', title: '', body: '', scripts: []};
      try {
        const d = frame.contentDocument;
        if (d) {
          result.html = d.documentElement ? d.documentElement.outerHTML : '';
          result.title = d.title || '';
          result.body = d.body ? (d.body.innerText || '') : '';
          result.scripts = Array.from(d.scripts || []).map(s => ({src: s.src || '', text: s.textContent || ''}));
        }
      } catch (e) {}
      return result;
    }
    return {
      html: document.documentElement ? document.documentElement.outerHTML : '',
      title: document.title || '',
      body: document.body ? (document.body.innerText || '') : '',
      scripts: Array.from(document.scripts || []).map(s => ({src: s.src || '', text: s.textContent || ''})),
      frames: Array.from(document.querySelectorAll('iframe,frame')).map(frameSnapshot)
    };
    """
    return driver.execute_script(script)


def inspect_browser_state(audit: SiteAudit, session, browser: BrowserClient, page_url: str) -> None:
    payload = browser_snapshot(browser, page_url, TIMEOUT)
    audit.browser_ok = True
    audit.coverage["title"] = True
    audit.coverage["body"] = True
    audit.coverage["script"] = True
    audit.coverage["iframe"] = True
    audit.page_count += 1
    label = "browser"
    audit.add_unit(f"{label}:title", str(payload.get("title") or ""), page_url, page_url)
    audit.add_unit(f"{label}:body", str(payload.get("body") or ""), page_url, page_url)

    for index, script in enumerate(payload.get("scripts") or [], 1):
        src = str(script.get("src") or "")
        inline = str(script.get("text") or "")
        if inline.strip():
            audit.add_unit(f"{label}:script-inline#{index}", inline, page_url, page_url)
            for decoded_index, decoded in enumerate(decode_strdecode_blocks(inline), 1):
                audit.add_unit(f"{label}:script-decoded#{index}.{decoded_index}", decoded, page_url, page_url)
        if not src:
            continue
        script_url = urljoin(page_url, src)
        audit.add_unit(f"{label}:script-ref#{index}", script_url, script_url, page_url, countable=False)
        if script_url in audit.visited_scripts:
            continue
        audit.visited_scripts.add(script_url)
        try:
            script_text = fetch_resource(session, script_url, SCRIPT_TIMEOUT)
            audit.script_count += 1
            audit.add_unit(f"{label}:script-src#{index}", script_text, script_url, page_url)
            for decoded_index, decoded in enumerate(decode_strdecode_blocks(script_text), 1):
                audit.add_unit(f"{label}:script-decoded-src#{index}.{decoded_index}", decoded, script_url, page_url)
        except Exception as exc:
            audit.errors.append(f"browser script {script_url}: {type(exc).__name__}: {exc}")

    for index, frame in enumerate(payload.get("frames") or [], 1):
        src = str(frame.get("src") or "")
        srcdoc = str(frame.get("srcdoc") or "")
        embedded_html = str(frame.get("html") or "")
        frame_url = urljoin(page_url, src) if src else ""
        audit.add_unit(f"{label}:iframe-ref#{index}", frame_url or srcdoc, frame_url or page_url, page_url, countable=False)
        if embedded_html:
            audit.iframe_count += 1
            inspect_html_document(
                audit,
                session,
                embedded_html,
                frame_url or page_url,
                page_url,
                f"{label}:iframe#{index}",
                1,
                fetch_children=False,
            )
        elif srcdoc:
            audit.iframe_count += 1
            inspect_html_document(audit, session, srcdoc, frame_url or page_url, page_url, f"{label}:iframe#{index}:srcdoc", 1, fetch_children=False)
        elif frame_url and not frame_url.startswith(("javascript:", "about:", "data:")):
            if frame_url not in audit.visited_iframes:
                audit.visited_iframes.add(frame_url)
                try:
                    frame_html = fetch_resource(session, frame_url, IFRAME_TIMEOUT)
                    audit.iframe_count += 1
                    inspect_html_document(audit, session, frame_html, frame_url, page_url, f"{label}:iframe#{index}", 1, fetch_children=False)
                except Exception as exc:
                    audit.errors.append(f"browser iframe {frame_url}: {type(exc).__name__}: {exc}")


def inspect_special_sources(audit: SiteAudit, session, site: Site) -> None:
    try:
        documents = collect_special_site_documents(session, site, TIMEOUT, PERIOD)
    except Exception as exc:
        audit.errors.append(f"special source: {type(exc).__name__}: {exc}")
        return
    if documents is None:
        return
    for index, document in enumerate(documents, 1):
        source_url = str(getattr(document, "source_url", "") or site.url)
        document_type = str(getattr(document, "document_type", "text") or "text")
        fetch_kind = str(getattr(document, "fetch_kind", "special") or "special")
        audit.add_document(f"special:{fetch_kind}:{document_type}#{index}", str(document), source_url, site.url)
        if document_type in {"html", "page-source"} and source_url.startswith(("http://", "https://")):
            inspect_http_page(audit, session, source_url, f"special-page#{index}")


def audit_site(site: Site, browser: BrowserClient | None) -> SiteAudit:
    audit = SiteAudit(site)
    session = create_session({})
    try:
        audit.initial_page_ok = inspect_http_page(audit, session, site.url, "initial")
        if site.browser and browser is not None:
            try:
                inspect_browser_state(audit, session, browser, site.url)
            except Exception as exc:
                audit.browser_ok = False
                audit.errors.append(f"browser page {site.url}: {type(exc).__name__}: {exc}")
        inspect_special_sources(audit, session, site)
    finally:
        session.close()
    return audit


def main() -> int:
    root = PROJECT_ROOT
    sites_path = root / "sites.json"
    sites = load_sites(sites_path)
    formal_paths = [
        sites_path,
        root / "outputs" / "recent_10_cache.json",
        Path(r"C:\Users\Administrator\Desktop\每天工具\数据系列\七类数据统一归纳\212期-合.txt"),
        Path(r"C:\Users\Administrator\Desktop\每天工具\数据系列\七类数据统一归纳\212期-合-失败.txt"),
    ]
    before = {str(path): snapshot(path) for path in formal_paths}

    browser: BrowserClient | None = None
    browser_error = ""
    if any(site.browser for site in sites):
        browser = BrowserClient(headless=True)
        try:
            browser.start()
        except Exception as exc:
            browser_error = f"{type(exc).__name__}: {exc}"
            browser.close()
            browser = None
            print(f"BROWSER_START_ERROR {browser_error}", flush=True)

    audits: list[SiteAudit] = []
    started = time.time()
    try:
        for index, site in enumerate(sites, 1):
            audit = audit_site(site, browser)
            audits.append(audit)
            source_rows = audit.source_rows()
            logical_rows = audit.logical_rows()
            values = sorted({value for row in logical_rows for value in row["values"]})
            coverage = ",".join(key for key, value in audit.coverage.items() if value) or "none"
            if not os.environ.get("STRICT_AUDIT_QUIET"):
                print(
                    f"PROGRESS {index}/{len(sites)} {site.site_id} {site.name} "
                    f"pages={audit.page_count} scripts={audit.script_count} iframes={audit.iframe_count} "
                    f"raw212={audit.raw_hits()} source_rows={len(source_rows)} logical_rows={len(logical_rows)} "
                    f"values={','.join(values) or '-'} coverage={coverage} errors={len(audit.errors)}",
                    flush=True,
                )
    finally:
        if browser is not None:
            browser.close()

    after = {str(path): snapshot(path) for path in formal_paths}
    unchanged = all(before[key] == after[key] for key in before)
    elapsed = time.time() - started
    multi_logical = [audit for audit in audits if len(audit.logical_rows()) >= 2]
    multi_source = [audit for audit in audits if len(audit.source_rows()) >= 2]
    errors = [audit for audit in audits if audit.errors]
    full_coverage = [
        audit
        for audit in audits
        if audit.initial_page_ok
        and audit.browser_ok
        and all(audit.coverage.values())
    ]

    print(f"SUMMARY total={len(sites)} completed={len(audits)} elapsed_seconds={elapsed:.1f}", flush=True)
    print(f"SUMMARY browser_start_error={browser_error or '-'}", flush=True)
    print(f"SUMMARY full_channel_coverage={len(full_coverage)}/{len(sites)}", flush=True)
    print(f"SUMMARY multi_logical_rows={len(multi_logical)} multi_source_rows={len(multi_source)}", flush=True)
    print(f"SUMMARY sites_with_errors={len(errors)} formal_files_unchanged={unchanged}", flush=True)
    print("MULTI_LOGICAL", flush=True)
    for audit in multi_logical:
        logical_rows = audit.logical_rows()
        values = sorted({value for row in logical_rows for value in row["values"]})
        channels = sorted({str(row["channel"]) for row in logical_rows})
        print(
            f"{audit.site.site_id}\t{audit.site.name}\t{audit.site.url}\tlogical_rows={len(logical_rows)} "
            f"source_rows={len(audit.source_rows())}\tvalues={','.join(values) or '-'}\tchannels={','.join(channels)}",
            flush=True,
        )
    print("ERRORS", flush=True)
    for audit in errors:
        print(f"{audit.site.site_id}\t{audit.site.name}\t{audit.site.url}", flush=True)
        for error in audit.errors[:8]:
            print(f"  {error[:500]}", flush=True)
    print("FORMAL_SNAPSHOT", flush=True)
    for path in formal_paths:
        print(f"{path}\tbefore={before[str(path)]}\tafter={after[str(path)]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
