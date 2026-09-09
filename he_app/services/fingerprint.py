"""History extraction stays inside the same independently validated record.

Daily top/bottom rules are never weakened to obtain older rows. Historical
extractors expose the already-selected block's rows and verify their evidence.
Unsupported layouts yield only an evidenced edge, reported as insufficient by
our caller; they are not silently treated as a completed duplicate check.
"""
from bs4 import BeautifulSoup

from he_app.domain.errors import SiteScrapeFailure
from he_app.domain.models import Candidate, Site
from he_app.domain.periods import PeriodKey, issue_map_for_window, period_window
from he_app.parsers.common import build_latest_candidate_parts, extract_values, period_numbers_in_text
from he_app.parsers.dedicated import history, structured, tables, kaijiangfacai, ttss
from he_app.parsers.registry import REGISTRY
from he_app.services.single_period import _allowed_documents, build_document_evidence, parse_site_period
from he_app.validation.direction import directional_window
from he_app.validation.period import is_valid_sum_value

Fingerprint = dict[int, str]
CycleFingerprint = dict[PeriodKey, str]


def _edge_block(blocks, pick):
    selected = directional_window([block for block in blocks if block], pick)
    return selected[0] if selected else []


def _parts_to_candidates(parts, weak=False):
    return [Candidate(",".join(extract_values(line, weak)), line, 0, index)
            for index, (line, _located) in enumerate(parts)]


def _history_rows(site: Site, document: str, period: int) -> list[Candidate]:
    builders = {
        "s058_vkjwinyt": tables._tongtian_table_blocks,
        "s073_shuqhbq": tables._jiuxiao_table_blocks,
        "s085_kcvpleh": tables._woman_flavor_table_blocks,
        kaijiangfacai.KAIJIANGFACAI_SITE_ID: kaijiangfacai._table_blocks,
        "s094_topic_727508": history._toutianhuanri_rows,
    }
    if site.site_id in builders:
        return [candidate for _issue, candidate in
                _edge_block(builders[site.site_id]([document]), site.pick)]
    if site.site_id == "s025_topic_435508":
        return _edge_block(history.hushuobadao_author_blocks([document])[0], site.pick)
    if site.site_id == "s109_topic_222783":
        return _edge_block(history.ruyimutan_author_content_blocks([document]), site.pick)
    if site.site_id == structured.YIAIZHIMING_SITE_ID:
        return structured._yiaizhimin_raw_history_rows([document])
    if site.site_id == structured.DAJIAFA_SITE_ID:
        return _edge_block(structured.dajiafa_history_cycles([document]), site.pick)
    if site.site_id in ttss.TTSS_SITE_IDS:
        blocks = [rows for title_period, rows in ttss._article_blocks(document, site.name)
                  if title_period == period]
        return [candidate for _issue, candidate in _edge_block(blocks, site.pick)]
    parser = REGISTRY._parsers.get(site.site_id, REGISTRY.default)
    cycles = {
        "_parse_xianrenhouji": "作者:先人后己", "_parse_leifeng_second": None,
        "_parse_yingba": "盈把之木", "_parse_shushen": "束身自修", "_parse_munan": "木南少年",
    }
    if parser.__name__ in cycles:
        return _edge_block(history._strict_kill_sum_cycles([document], cycles[parser.__name__]), site.pick)
    rule = tables.site_rule(site)
    if parser is REGISTRY.default and rule.anchor_text:
        blocks, _found = history.anchor_document_blocks(site, [document])
        candidate_blocks = []
        for block in blocks:
            parts = build_latest_candidate_parts(
                [block], rule.allow_weak_kill_sum_keyword
            )
            candidates = _parts_to_candidates(
                parts, rule.allow_weak_kill_sum_keyword
            )
            if candidates:
                candidate_blocks.append(candidates)
        return _edge_block(candidate_blocks, site.pick)
    if parser.__name__ in {"_parse_batch", "_parse_batch_author"}:
        # A document with separate article bodies needs an explicit block adapter.
        soup = BeautifulSoup(document, "html.parser")
        if len(soup.select(".topic-content, article")) > 1:
            return []
        parts = history.build_dedicated_window_parts([document], rule.allow_weak_kill_sum_keyword)
        return _parts_to_candidates(parts, rule.allow_weak_kill_sum_keyword)
    if parser is REGISTRY.default:
        soup = BeautifulSoup(document, "html.parser")
        if len(soup.select(".topic-content, article")) > 1:
            return []
        parts = build_latest_candidate_parts([document], rule.allow_weak_kill_sum_keyword)
        if rule.require_body_locator:
            parts = [part for part in parts if part[1]]
        return _parts_to_candidates(parts, rule.allow_weak_kill_sum_keyword)

    # A dedicated current parser does not automatically imply a dedicated
    # history extractor.  When the fetched authority contains exactly one
    # article/body, reuse the strict dedicated row recognizer.  The baseline
    # has already been proven by that site's dedicated parser, so this only
    # exposes older rows from the same record; it never scans another article
    # or relaxes the current top/bottom boundary.
    soup = BeautifulSoup(document, "html.parser")
    if len(soup.select(".topic-content, article")) > 1:
        return []
    parts = history.build_dedicated_window_parts(
        [document], rule.allow_weak_kill_sum_keyword
    )
    return _parts_to_candidates(parts, rule.allow_weak_kill_sum_keyword)



def _candidate_issue(candidate: Candidate) -> int | None:
    issues = period_numbers_in_text(candidate.line)
    if not issues or any(issue != issues[0] for issue in issues):
        return None
    return issues[0]


def _directional_current_cycle(
    rows: list[Candidate],
    base: PeriodKey,
    periods: int,
    pick: str,
    current_value: str,
) -> list[Candidate]:
    """Return only the contiguous cycle containing the selected edge row.

    Long-running pages often contain several calendar cycles with the same
    bare issue numbers.  Once the current parser proves the exact top/bottom
    edge, historical extraction must follow the physically adjacent older
    rows from that edge; a later/earlier cycle may not create false conflicts
    or fill gaps.
    """

    matches = [
        index
        for index, candidate in enumerate(rows)
        if _candidate_issue(candidate) == base.issue
        and candidate.values == current_value
    ]
    if not matches:
        return []

    anchor = matches[0] if pick == "top" else matches[-1]
    step = 1 if pick == "top" else -1
    expected = period_window(base, periods)
    selected = [rows[anchor]]
    cursor = anchor

    for key in expected[1:]:
        cursor += step
        # Nested HTML may expose an exact duplicate of the same physical row.
        # Skip only exact issue/value/line duplicates; any other repeated or
        # unexpected issue marks a real boundary and stops the cycle.
        while 0 <= cursor < len(rows):
            candidate = rows[cursor]
            previous = selected[-1]
            if (
                _candidate_issue(candidate) == _candidate_issue(previous)
                and candidate.values == previous.values
                and candidate.line == previous.line
            ):
                cursor += step
                continue
            break
        if not 0 <= cursor < len(rows):
            break
        candidate = rows[cursor]
        if _candidate_issue(candidate) != key.issue:
            break
        selected.append(candidate)

    return selected

def build_site_period_fingerprint(
    site: Site,
    documents: list[str],
    base: PeriodKey,
    periods: int,
) -> CycleFingerprint:
    if periods < 1:
        raise ValueError("历史窗口必须为正数")
    documents = _allowed_documents(site, documents)
    issue_map = issue_map_for_window(base, periods)

    # Enforce the exact requested current edge before considering history.
    baseline = parse_site_period(site, base.issue, documents)
    if not baseline.success:
        return {}

    snapshots: list[CycleFingerprint] = []
    for document in documents:
        edge = parse_site_period(site, base.issue, [document])
        if not edge.success or not edge.evidence:
            continue
        fingerprint: CycleFingerprint = {
            base: ",".join(edge.evidence[0].values)
        }
        values_by_key: dict[PeriodKey, str] = {}
        ambiguous: set[PeriodKey] = set()
        current_value = ",".join(edge.evidence[0].values)
        history_rows = _directional_current_cycle(
            _history_rows(site, document, base.issue),
            base,
            periods,
            site.pick,
            current_value,
        )
        for candidate in history_rows:
            issues = period_numbers_in_text(candidate.line)
            if not issues or any(issue != issues[0] for issue in issues):
                continue
            key = issue_map.get(issues[0])
            if key is None:
                continue
            values = candidate.values.split(",")
            if (
                len(values) != site.value_count
                or len(set(values)) != len(values)
                or not all(is_valid_sum_value(value) for value in values)
            ):
                continue
            previous = values_by_key.get(key)
            if previous is not None and previous != candidate.values:
                ambiguous.add(key)
                continue
            evidence = build_document_evidence(
                site, key.issue, values, candidate.line, [document]
            )
            if evidence:
                values_by_key[key] = candidate.values
        for key, value in values_by_key.items():
            if key != base and key not in ambiguous:
                fingerprint[key] = value
        snapshots.append(dict(sorted(fingerprint.items(), reverse=True)))

    for index, left in enumerate(snapshots):
        for right in snapshots[index + 1:]:
            for key in left.keys() & right.keys():
                if left[key] != right[key]:
                    raise SiteScrapeFailure(
                        "候选冲突",
                        f"{site.name} {key.cache_key}期不同来源历史指纹冲突",
                    )
    return max(snapshots, key=len) if snapshots else {}


def build_site_fingerprint(
    site: Site,
    documents: list[str],
    period: int,
    periods: int,
    cycle_year: int | None = None,
):
    """Build a fingerprint; cycle-aware callers receive ``PeriodKey`` keys.

    Omitting ``cycle_year`` preserves the historical integer-key public API.
    Production entry points always pass a year and therefore retain identity
    across 365/366 -> 001 rollovers.
    """

    if period < 1:
        raise ValueError("期数必须为正数")
    compatibility_year = cycle_year or 2000
    base = PeriodKey(compatibility_year, period)
    fingerprint = build_site_period_fingerprint(site, documents, base, periods)
    if cycle_year is not None:
        return fingerprint
    return {key.issue: value for key, value in fingerprint.items()}
