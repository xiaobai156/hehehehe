import re
from urllib.parse import urlparse

from he_app.domain.errors import SiteScrapeFailure


RECORD_ID_KEYS = ("id", "_id", "articleId", "article_id", "recordId", "record_id")


def record_id_from_url(url: str) -> str | None:
    path = urlparse(url).path
    match = re.search(r"/article/(?:manager|admin)/([A-Za-z0-9_-]+)(?:$|/)", path)
    return match.group(1) if match is not None else None


def find_unique_record(payload: object, record_id: str) -> tuple[str, dict]:
    matches: list[tuple[str, dict]] = []

    def visit(value: object, path: str) -> None:
        if isinstance(value, dict):
            if any(str(value.get(key, "")) == record_id for key in RECORD_ID_KEYS):
                matches.append((path, value))
            for key, nested in value.items():
                visit(nested, f"{path}.{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                visit(nested, f"{path}[{index}]")

    visit(payload, "$")
    if not matches:
        raise SiteScrapeFailure("记录ID缺失", f"接口未找到URL记录ID: {record_id}")
    if len(matches) != 1:
        raise SiteScrapeFailure("记录ID冲突", f"接口找到{len(matches)}条相同记录ID: {record_id}")
    return matches[0]
