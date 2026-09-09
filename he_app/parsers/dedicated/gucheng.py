import re

from bs4 import BeautifulSoup

from he_app.domain.errors import DedicatedCandidateConflict
from he_app.domain.models import Candidate, Site
from he_app.domain.policies import normalize_digit_text, normalize_pick, normalize_text
from he_app.parsers.common import score_candidate


GUCHENG_SITE_ID = "s138_gsx_20"
GUCHENG_NAME = "故城笙声"
GUCHENG_MARKER_RE = re.compile(r"杀\s*(?:2|二)\s*合\s*数", re.I)
GUCHENG_STOP_RE = re.compile(r"澳门六合彩合数属性|合数属性|copyright", re.I)
GUCHENG_ROW_RE = re.compile(
    r"(?<!\d)(?P<period>\d{1,4})\s*期\s*[:：]?\s*"
    r"杀\s*(?:2|二)\s*合\s*数\s*"
    r"[\[【(（]\s*(?P<first>0?[1-9]|1[0-3])\s*合\s*"
    r"(?:[-－—~～、,，\s]*)"
    r"(?P<second>0?[1-9]|1[0-3])\s*合\s*[\]】)）]\s*(?:开|開)",
    re.I,
)


def _normalized_text(document: str) -> str:
    soup = BeautifulSoup(document, "html.parser")
    text = soup.get_text(" ", strip=True) if soup.find() else document
    return normalize_digit_text(normalize_text(text))


def _gucheng_blocks(site: Site, documents: list[str]) -> list[list[tuple[int, Candidate]]]:
    blocks: list[list[tuple[int, Candidate]]] = []
    signatures: set[tuple[tuple[int, str], ...]] = set()
    name = normalize_digit_text(normalize_text(site.name or GUCHENG_NAME))

    for document in documents:
        text = _normalized_text(document)
        if name not in text:
            continue
        marker = GUCHENG_MARKER_RE.search(text)
        if marker is None:
            continue
        section = text[marker.start() :]
        stop = GUCHENG_STOP_RE.search(section, marker.end() - marker.start())
        if stop is not None:
            section = section[: stop.start()]

        block: list[tuple[int, Candidate]] = []
        for order, match in enumerate(GUCHENG_ROW_RE.finditer(section)):
            period = int(match.group("period"))
            values = (
                f"{int(match.group('first')):02d}合",
                f"{int(match.group('second')):02d}合",
            )
            line = normalize_text(match.group(0))
            block.append(
                (
                    period,
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


def _select_unique_gucheng_candidate(
    candidates: list[Candidate], pick: str
) -> Candidate | None:
    if not candidates:
        return None
    unique_values = {candidate.values for candidate in candidates}
    if len(unique_values) > 1:
        raise DedicatedCandidateConflict(
            sorted(unique_values), [candidate.line for candidate in candidates]
        )
    return candidates[0] if normalize_pick(pick) == "top" else candidates[-1]


def find_gucheng_kill_sum_candidate_with_direction(
    site: Site,
    documents: list[str],
    period: int,
    pick: str,
) -> tuple[Candidate | None, bool]:
    blocks = _gucheng_blocks(site, documents)
    if not blocks:
        return None, False

    normalized_pick = normalize_pick(pick)
    selected_block = blocks[0] if normalized_pick == "top" else blocks[-1]
    edge_period = selected_block[0][0] if normalized_pick == "top" else selected_block[-1][0]
    edge_matches = [
        candidate
        for row_period, candidate in selected_block
        if row_period == period and row_period == edge_period
    ]
    candidate = _select_unique_gucheng_candidate(edge_matches, normalized_pick)
    if candidate is not None:
        return candidate, False

    target_found = any(row_period == period for block in blocks for row_period, _ in block)
    return None, target_found


def extract_gucheng_kill_sum_period_values(
    site: Site, documents: list[str]
) -> dict[int, str]:
    values: dict[int, str] = {}
    conflicts: set[int] = set()
    for block in _gucheng_blocks(site, documents):
        for period, candidate in block:
            if period in conflicts:
                continue
            existing = values.get(period)
            if existing is not None and existing != candidate.values:
                conflicts.add(period)
                values.pop(period, None)
                continue
            values[period] = candidate.values
    return values


def find_gucheng_history_candidate(
    site: Site, documents: list[str], period: int
) -> Candidate | None:
    candidates: list[Candidate] = []
    for block in _gucheng_blocks(site, documents):
        candidates.extend(
            candidate for row_period, candidate in block if row_period == period
        )
    return _select_unique_gucheng_candidate(candidates, "top")


def is_gucheng_kill_sum_row(text: str, period: int, values: list[str]) -> bool:
    normalized = normalize_digit_text(normalize_text(text))
    expected = tuple(values)
    for match in GUCHENG_ROW_RE.finditer(normalized):
        if int(match.group("period")) != period:
            continue
        actual = (
            f"{int(match.group('first')):02d}合",
            f"{int(match.group('second')):02d}合",
        )
        if actual == expected:
            return True
    return False
