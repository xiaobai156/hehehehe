from dataclasses import dataclass


class SourceDocument(str):
    def __new__(
        cls,
        text: str,
        *,
        source_url: str = "",
        fetch_kind: str = "legacy",
        document_type: str = "text",
        parent_url: str = "",
        record_id: str | None = None,
        authority_id: str = "legacy",
        document_id: str = "",
    ):
        instance = super().__new__(cls, text)
        instance.source_url = source_url
        instance.fetch_kind = fetch_kind
        instance.document_type = document_type
        instance.parent_url = parent_url
        instance.record_id = record_id
        instance.authority_id = authority_id or "legacy"
        instance.document_id = document_id or instance.authority_id
        return instance


@dataclass(frozen=True)
class Site:
    name: str
    url: str
    pick: str
    browser: bool = False
    click_first: bool = False
    site_id: str = ""


@dataclass(frozen=True)
class Candidate:
    values: str
    line: str
    score: int
    order: int


@dataclass(frozen=True)
class PreviousInfo:
    display: str
    rankable: bool
    reason: str | None = None


@dataclass(frozen=True)
class FailureInfo:
    category: str
    reason: str


@dataclass(frozen=True)
class SiteRule:
    require_body_locator: bool = True
    note: str = ""
    anchor_text: str = ""
    latest_after_anchor: bool = False
    anchor_pick: str = ""
    allow_weak_kill_sum_keyword: bool = False
    allowed_fetch_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateEvidence:
    site_id: str
    period: int
    values: tuple[str, ...]
    source_url: str
    document_id: str
    row_order: int
    raw_line: str
    article_id: str | None = None
    author: str = ""
    title: str = ""
    keyword: str = ""
    fetch_kind: str = "legacy"
    document_type: str = "text"
    parent_url: str = ""
    authority_id: str = "legacy"
    block_id: str = ""
    block_start: int = -1
    block_end: int = -1
    actual_position: int = -1


@dataclass(frozen=True)
class ParseResult:
    success: bool
    value: str | None = None
    evidence: tuple[CandidateEvidence, ...] = ()
    failure: FailureInfo | None = None
