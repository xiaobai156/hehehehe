"""Read-only round audit for independent 212-period data blocks.

This helper intentionally writes no result, cache, configuration, or TXT file.
It reuses the existing full-channel collector and groups related field rows
inside one HTML table as one logical data block.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from he_app.config.sites import load_sites
from he_app.domain.models import Site
from he_app.domain.policies import normalize_digit_text, normalize_text
from he_app.fetch.browser import BrowserClient

from strict_212_audit import audit_site


PERIOD = 212
PERIOD_RE = re.compile(r"(?<!\d)212\s*期")
TABLE_VALUE_RE = re.compile(
    r"[0-9０-９]|鼠|牛|虎|兔|龙|蛇|马|羊|猴|鸡|狗|猪|红|蓝|绿|单|双|大数|小数|家畜|野兽"
)
ACTUAL_MARKER_RE = re.compile(r"开|開|合|杀|殺|准|错|錯|中")
ROUND_SIZES = (30, 30, 30, 31)


@dataclass(frozen=True)
class Block:
    signature: str
    display: str
    source: str
    channel: str


def compact(text: str) -> str:
    return re.sub(r"\s+", "", normalize_digit_text(normalize_text(text)))


def has_data_after_period(text: str) -> bool:
    normalized = normalize_digit_text(normalize_text(text))
    match = PERIOD_RE.search(normalized)
    if not match:
        return False
    tail = normalized[match.end() :]
    tail = re.sub(r"(?<!\d)\d+\s*期", "", tail)
    tail = re.sub(r"[【\[](.*?)[】\]]", "", tail)
    return bool(re.search(r"\d", tail) and ACTUAL_MARKER_RE.search(tail))


def has_table_data_after_period(text: str) -> bool:
    normalized = normalize_digit_text(normalize_text(text))
    match = PERIOD_RE.search(normalized)
    if not match:
        return False
    tail = normalized[match.end() :]
    tail = re.sub(r"[【\[](.*?)[】\]]", "", tail)
    tail = re.sub(r"(?<!\d)\d+\s*期", "", tail)
    return bool(tail and TABLE_VALUE_RE.search(tail))


def target_lines(text: str) -> list[str]:
    normalized = normalize_digit_text(normalize_text(text))
    lines = [normalize_text(line) for line in normalized.splitlines() if normalize_text(line)]
    return [line for line in lines if has_data_after_period(line)]


def html_blocks(text: str, channel: str, source: str) -> list[Block]:
    soup = BeautifulSoup(text, "html.parser")
    if soup.find() is None:
        return plain_blocks(text, channel, source)

    blocks: list[Block] = []

    for index, table in enumerate(soup.find_all("table"), start=1):
        table_lines = [
            normalize_text(line)
            for line in table.get_text("\n", strip=True).splitlines()
            if has_table_data_after_period(line)
        ]
        if not table_lines:
            continue
        display = normalize_text(" ".join(table_lines))
        blocks.append(Block(f"table:{compact(display)}", display[:900], source, channel))

    candidate_tags = ("p", "li", "article", "div", "font")
    for tag in soup.find_all(candidate_tags):
        if tag.find_parent("table") is not None:
            continue
        text_value = normalize_text(tag.get_text(" ", strip=True))
        if not has_data_after_period(text_value):
            continue
        child_candidate = any(
            has_data_after_period(child.get_text(" ", strip=True))
            for child in tag.find_all(candidate_tags)
        )
        if child_candidate:
            continue
        signature = compact(text_value)
        blocks.append(Block(f"row:{signature}", text_value[:900], source, channel))

    if not blocks:
        for line in target_lines(soup.get_text("\n", strip=True)):
            blocks.append(Block(f"line:{compact(line)}", line[:900], source, channel))
    return blocks


def plain_blocks(text: str, channel: str, source: str) -> list[Block]:
    lines = target_lines(text)
    if not lines:
        return []
    # A plain decoded payload has no reliable DOM boundary. Keep contiguous
    # target lines together when it is clearly one payload; otherwise retain
    # individual lines for an explicit diagnostic rather than guessing.
    if len(lines) >= 3 and all(PERIOD_RE.search(line) for line in lines):
        display = normalize_text(" ".join(lines))
        return [Block(f"plain-group:{compact(display)}", display[:900], source, channel)]
    return [Block(f"line:{compact(line)}", line[:900], source, channel) for line in lines]


def is_html_unit(unit: dict[str, object]) -> bool:
    document_type = str(unit.get("channel", ""))
    return "script" not in document_type and "decoded" not in document_type


def select_primary_units(units: list[dict[str, object]]) -> list[dict[str, object]]:
    preferred = [
        unit
        for unit in units
        if str(unit.get("channel", "")).endswith(":page-source")
        and str(unit.get("channel", "")).startswith("browser")
    ]
    if preferred:
        return preferred
    preferred = [unit for unit in units if str(unit.get("channel", "")).endswith(":page-source")]
    if preferred:
        return preferred
    return sorted(
        [unit for unit in units if is_html_unit(unit)],
        key=lambda unit: len(str(unit.get("text", ""))),
        reverse=True,
    )[:1]


def add_unique(blocks: list[Block], candidate: Block) -> None:
    candidate_key = compact(candidate.display)
    if not candidate_key:
        return
    for existing in blocks:
        existing_key = compact(existing.display)
        if candidate_key == existing_key:
            return
        if candidate_key in existing_key or existing_key in candidate_key:
            return
    blocks.append(candidate)


def collect_blocks(audit) -> tuple[list[Block], list[Block]]:
    primary: list[Block] = []
    extra: list[Block] = []
    primary_units = select_primary_units(audit.units)
    primary_ids = {id(unit) for unit in primary_units}

    for unit in primary_units:
        text = str(unit.get("text", ""))
        channel = str(unit.get("channel", ""))
        source = str(unit.get("source_url", ""))
        for block in html_blocks(text, channel, source):
            add_unique(primary, block)

    for unit in audit.units:
        if id(unit) in primary_ids:
            continue
        text = str(unit.get("text", ""))
        channel = str(unit.get("channel", ""))
        source = str(unit.get("source_url", ""))
        if not text:
            continue
        if "iframe" in channel or "decoded" in channel or "script" in channel:
            candidates = html_blocks(text, channel, source) if "<" in text else plain_blocks(text, channel, source)
            for block in candidates:
                before = len(primary) + len(extra)
                add_unique(primary, block)
                if len(primary) + len(extra) > before:
                    extra.append(block)

    # The loop above adds novel blocks to primary to make containment checks
    # deterministic. Return all blocks and a diagnostic copy for non-primary
    # sources discovered in hidden channels.
    return primary, extra


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int, required=True, choices=range(1, 5))
    args = parser.parse_args()

    sites = load_sites(PROJECT_ROOT / "sites.json")
    start = sum(ROUND_SIZES[: args.round - 1])
    selected = sites[start : start + ROUND_SIZES[args.round - 1]]
    browser = BrowserClient(headless=True)
    browser.start()
    print(f"ROUND {args.round} sites={len(selected)} range={start + 1}-{start + len(selected)}", flush=True)
    try:
        for index, original in enumerate(selected, start + 1):
            site = Site(original.name, original.url, original.pick, True, original.click_first, original.site_id)
            try:
                audit = audit_site(site, browser)
                if browser.driver is not None:
                    audit.add_unit(
                        "browser:page-source",
                        browser.driver.page_source or "",
                        site.url,
                        site.url,
                    )
                blocks, hidden = collect_blocks(audit)
                errors = len(audit.errors)
                print(
                    f"SITE {index}/{len(sites)}\t{site.name}\t{site.url}\tblocks={len(blocks)}"
                    f"\thidden_new={len(hidden)}\terrors={errors}",
                    flush=True,
                )
                for block_index, block in enumerate(blocks, 1):
                    print(
                        f"  BLOCK {block_index}\t{block.channel}\t{block.display[:700]}",
                        flush=True,
                    )
                if errors:
                    for error in audit.errors[:4]:
                        print(f"  ERROR {error[:400]}", flush=True)
            except Exception as exc:  # diagnostic only; never convert to success
                print(f"SITE {index}/{len(sites)}\t{original.name}\t{original.url}\tERROR {type(exc).__name__}: {exc}", flush=True)
    finally:
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
