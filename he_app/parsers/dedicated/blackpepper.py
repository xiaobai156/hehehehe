import re

from bs4 import BeautifulSoup

from he_app.domain.errors import DedicatedCandidateConflict
from he_app.domain.models import Candidate, Site
from he_app.domain.policies import normalize_digit_text, normalize_pick, normalize_text
from he_app.validation.direction import directional_window


BLACKPEPPER_SITE_ID = "s148_topic_805245"
BLACKPEPPER_RECORD_ID = "805245"
BLACKPEPPER_URL_RE = re.compile(r"/topic/805245\.html(?:[?#].*)?$", re.I)
BLACKPEPPER_TITLE_RE = re.compile(
    r"^(\d{1,4})\s*期\s*:\s*黑胡椒酱\s*\[\s*绝杀合数\s*\]\s*高手研究$"
)
BLACKPEPPER_ROW_RE = re.compile(
    r"^(\d{1,4})\s*[期在]\s*[:：]\s*绝杀合数\s*"
    r"\[\s*杀\s*(0?[1-9]|1[0-3])\s*合\s*\]\s*开"
)
HistoryBlock = list[tuple[int, Candidate]]


def is_blackpepper_row(text: str, period: int, value: str) -> bool:
    match = BLACKPEPPER_ROW_RE.match(normalize_digit_text(normalize_text(text)))
    return bool(
        match
        and int(match.group(1)) == period
        and f"{int(match.group(2)):02d}合" == value
    )


def _history_blocks(site: Site, documents: list[str]) -> list[HistoryBlock]:
    blocks: list[HistoryBlock] = []
    signatures: set[tuple[tuple[int, str], ...]] = set()
    for document in documents:
        source_url = str(getattr(document, "source_url", "") or "")
        if source_url.rstrip("/") != site.url.rstrip("/") or BLACKPEPPER_URL_RE.search(source_url) is None:
            continue
        soup = BeautifulSoup(document, "html.parser")
        tag_order = {id(tag): order for order, tag in enumerate(soup.find_all(True))}
        for title in soup.select(".title"):
            title_text = normalize_digit_text(normalize_text(title.get_text(" ", strip=True)))
            title_match = BLACKPEPPER_TITLE_RE.fullmatch(title_text)
            if title_match is None:
                continue
            next_title = title.find_next(class_="title")
            content = title.find_next(class_="topic-content")
            if content is None or (
                next_title is not None and tag_order[id(next_title)] < tag_order[id(content)]
            ):
                continue

            rows: HistoryBlock = []
            values_by_period: dict[int, str] = {}
            for raw_line in content.get_text("\n", strip=True).splitlines():
                line = normalize_digit_text(normalize_text(raw_line))
                match = BLACKPEPPER_ROW_RE.match(line)
                if match is None:
                    continue
                period = int(match.group(1))
                value = f"{int(match.group(2)):02d}合"
                previous = values_by_period.get(period)
                if previous is not None and previous != value:
                    raise DedicatedCandidateConflict(
                        sorted({previous, value}),
                        [candidate.line for row_period, candidate in rows if row_period == period] + [line],
                    )
                if previous is None:
                    values_by_period[period] = value
                    rows.append((period, Candidate(value, line, 120, len(rows))))

            if not rows or rows[0][0] != int(title_match.group(1)):
                continue
            signature = tuple((period, candidate.values) for period, candidate in rows)
            if signature not in signatures:
                signatures.add(signature)
                blocks.append(rows)

    if len(blocks) > 1:
        candidates = [candidate for block in blocks for _period, candidate in block]
        raise DedicatedCandidateConflict(
            sorted({candidate.values for candidate in candidates}),
            [candidate.line for candidate in candidates],
        )
    return blocks


def find_blackpepper_candidate_with_direction(
    site: Site,
    documents: list[str],
    period: int,
    pick: str,
) -> tuple[Candidate | None, bool]:
    blocks = _history_blocks(site, documents)
    if not blocks:
        return None, False
    selected = directional_window(blocks[0], normalize_pick(pick), 1)
    match = next(
        (candidate for row_period, candidate in selected if row_period == period),
        None,
    )
    outside = any(row_period == period for row_period, _candidate in blocks[0]) and match is None
    return match, outside


def extract_blackpepper_period_values(site: Site, documents: list[str]) -> dict[int, str]:
    return {
        period: candidate.values
        for block in _history_blocks(site, documents)
        for period, candidate in block
    }
