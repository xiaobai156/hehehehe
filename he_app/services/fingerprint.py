"""History extraction stays inside the same independently validated record.

Daily top/bottom rules are never weakened to obtain older rows. Historical
extractors expose the already-selected block's rows and verify their evidence.
Unsupported layouts yield only an evidenced edge, reported as insufficient by
our caller; they are not silently treated as a completed duplicate check.
"""
from bs4 import BeautifulSoup

from he_app.domain.errors import SiteScrapeFailure
from he_app.domain.models import Candidate, Site
from he_app.parsers.common import build_latest_candidate_parts, extract_values, period_numbers_in_text
from he_app.parsers.dedicated import history, structured, tables, kaijiangfacai, ttss
from he_app.parsers.registry import REGISTRY
from he_app.services.single_period import _allowed_documents, build_document_evidence, parse_site_period
from he_app.validation.direction import directional_window
from he_app.validation.period import is_valid_sum_value

Fingerprint = dict[int, str]


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
        selected = _edge_block([[block] for block in blocks], site.pick)
        return _parts_to_candidates(build_latest_candidate_parts(selected, rule.allow_weak_kill_sum_keyword),
                                    rule.allow_weak_kill_sum_keyword)
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
    return []


def build_site_fingerprint(site: Site, documents: list[str], period: int, periods: int) -> Fingerprint:
    if periods < 1 or period < 1:
        raise ValueError("期数和历史窗口必须为正数")
    documents = _allowed_documents(site, documents)
    # This enforces the exact requested baseline and same-snapshot veto before
    # any interior history is considered. No borrowing an adjacent baseline.
    baseline = parse_site_period(site, period, documents)
    if not baseline.success:
        return {}
    snapshots: list[Fingerprint] = []
    for document in documents:
        edge = parse_site_period(site, period, [document])
        if not edge.success or not edge.evidence:
            continue
        fingerprint = {period: ",".join(edge.evidence[0].values)}
        values_by_issue = {}
        ambiguous = set()
        for candidate in _history_rows(site, document, period):
            issues = period_numbers_in_text(candidate.line)
            if not issues or any(issue != issues[0] for issue in issues):
                continue
            issue = issues[0]
            if not max(1, period - periods + 1) <= issue <= period:
                continue
            values = candidate.values.split(",")
            if (len(values) != site.value_count or len(set(values)) != len(values)
                    or not all(is_valid_sum_value(value) for value in values)):
                continue
            previous = values_by_issue.get(issue)
            if previous is not None and previous != candidate.values:
                ambiguous.add(issue)
                continue
            evidence = build_document_evidence(site, issue, values, candidate.line, [document])
            if evidence:
                values_by_issue[issue] = candidate.values
        # The selected current edge remains authoritative; conflicting interior
        # duplicates cannot change it. Ambiguous historical issues are omitted.
        for issue, value in values_by_issue.items():
            if issue != period and issue not in ambiguous:
                fingerprint[issue] = value
        snapshots.append(dict(sorted(fingerprint.items(), reverse=True)))
    for index, left in enumerate(snapshots):
        for right in snapshots[index + 1:]:
            for issue in left.keys() & right.keys():
                if left[issue] != right[issue]:
                    raise SiteScrapeFailure("候选冲突", f"{site.name} {issue}期不同来源历史指纹冲突")
    # Select one whole snapshot, never assemble missing issues across documents.
    return max(snapshots, key=len) if snapshots else {}
