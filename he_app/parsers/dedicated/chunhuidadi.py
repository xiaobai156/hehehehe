from __future__ import annotations

import re

from bs4 import BeautifulSoup

from he_app.domain.errors import DedicatedCandidateConflict
from he_app.domain.models import Candidate, Site
from he_app.domain.policies import normalize_digit_text, normalize_pick, normalize_text
from he_app.parsers.common import score_candidate


SPRING_SITE_ID = "s152_topic_443994"
SPRING_AUTHOR = "春回大地"
SPRING_BLOCK_RE = re.compile(
    r"高手资料(?P<head_period>\d{1,4})\s*期\s*\[\s*必杀(?:二|两)合\s*\]"
    r"\s*已公开[!！]?\s*作者\s*:\s*春回大地(?P<body>.*?)(?=高手资料|$)"
)
SPRING_ROW_RE = re.compile(
    r"(?<!\d)(?P<period>\d{1,4})\s*期\s*:\s*必杀(?:二|两)合\s*"
    r"\(\s*(?P<first>0?[1-9]|1[0-3])\s*[.,]\s*"
    r"(?P<second>0?[1-9]|1[0-3])\s*\)\s*"
    r"(?:开|開)[0-9]{1,3}(?:准|错|錯|对|中)?",
    re.I,
)


def _normalized_text(document: str) -> str:
    soup = BeautifulSoup(document, "html.parser")
    text = soup.get_text(" ", strip=True) if soup.find() else document
    return normalize_digit_text(normalize_text(text))


def _blocks(site: Site, documents: list[str]) -> list[list[tuple[int, Candidate]]]:
    blocks: list[list[tuple[int, Candidate]]] = []
    signatures: set[tuple[tuple[int, str], ...]] = set()
    if normalize_text(site.name) != SPRING_AUTHOR:
        return blocks

    for document in documents:
        text = _normalized_text(document)
        for match in SPRING_BLOCK_RE.finditer(text):
            block: list[tuple[int, Candidate]] = []
            for order, row in enumerate(SPRING_ROW_RE.finditer(match.group("body"))):
                values = (
                    f"{int(row.group('first')):02d}合",
                    f"{int(row.group('second')):02d}合",
                )
                line = normalize_text(row.group(0))
                block.append(
                    (
                        int(row.group("period")),
                        Candidate(
                            values=",".join(values),
                            line=line,
                            score=score_candidate(line, list(values)),
                            order=order,
                        ),
                    )
                )
            signature = tuple((period, candidate.values) for period, candidate in block)
            if signature and signature not in signatures:
                signatures.add(signature)
                blocks.append(block)
    return blocks


def _select_unique(candidates: list[Candidate]) -> Candidate | None:
    if not candidates:
        return None
    values = {candidate.values for candidate in candidates}
    if len(values) > 1:
        raise DedicatedCandidateConflict(
            sorted(values), [candidate.line for candidate in candidates]
        )
    return candidates[0]


def find_spring_kill_sum_candidate_with_direction(
    site: Site,
    documents: list[str],
    period: int,
    pick: str,
) -> tuple[Candidate | None, bool]:
    blocks = _blocks(site, documents)
    if not blocks:
        return None, False

    normalized_pick = normalize_pick(pick)
    selected = blocks[0] if normalized_pick == "top" else blocks[-1]
    edge_period = selected[0][0] if normalized_pick == "top" else selected[-1][0]
    edge_matches = [
        candidate
        for row_period, candidate in selected
        if row_period == period == edge_period
    ]
    candidate = _select_unique(edge_matches)
    if candidate is not None:
        return candidate, False

    target_found = any(row_period == period for block in blocks for row_period, _ in block)
    return None, target_found


def is_spring_kill_sum_row(text: str, period: int, values: list[str]) -> bool:
    normalized = _normalized_text(text)
    expected = tuple(values)
    for row in SPRING_ROW_RE.finditer(normalized):
        if int(row.group("period")) != period:
            continue
        actual = (
            f"{int(row.group('first')):02d}合",
            f"{int(row.group('second')):02d}合",
        )
        if actual == expected:
            return True
    return False


def spring_segment_is_bounded(text: str, start: int, end: int) -> bool:
    normalized = normalize_digit_text(normalize_text(text))
    for block in SPRING_BLOCK_RE.finditer(normalized):
        body_start, body_end = block.span("body")
        if body_start <= start and end <= body_end:
            return True
    return False
