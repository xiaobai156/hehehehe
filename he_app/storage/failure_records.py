"""One lossless failure record contract for reports, retries and multi-period runs."""
from dataclasses import dataclass
import re
from collections.abc import Iterable

from he_app.domain.models import Site
from he_app.domain.policies import normalize_pick


@dataclass(frozen=True)
class FailureRecord:
    site_id: str
    name: str
    url: str
    pick: str
    period: int
    category: str
    stage: str
    reason: str


def serialize_failure_record(record: FailureRecord) -> str:
    reason = " ".join(record.reason.split())
    identity = f"站点ID: {record.site_id} " if record.site_id else ""
    return (f"失败 {record.name} {record.url} {identity}"
            f"方向: {record.pick} 期数: {record.period} "
            f"阶段: {record.stage} 失败类型: {record.category} 具体原因: {reason}")


def parse_failure_record(text: str, sites: Iterable[Site] = ()) -> FailureRecord:
    text = " ".join(text.lstrip("\ufeff").split())
    url_match = re.search(r"https?://\S+", text)
    if url_match is None:
        raise ValueError(f"失败TXT记录缺少URL: {text}")
    prefix, tail = text[:url_match.start()].strip(), text[url_match.end():].strip()
    url = url_match.group()
    id_match = re.search(r"站点ID:\s*(\S+)", text)
    site_id = id_match.group(1) if id_match else ""
    prefix = re.sub(r"站点ID:\s*\S+", "", prefix).strip()
    old = re.fullmatch(r"(\d+)期\s+(.+)", prefix)
    if old:
        period, name = int(old.group(1)), old.group(2)
    elif prefix.startswith("失败 "):
        name = prefix.removeprefix("失败 ").strip()
        match = re.search(r"(?:^|\s)期数:\s*(\d+)(?=\s|$)", tail)
        if match is None:
            raise ValueError(f"失败TXT记录缺少期数: {text}")
        period = int(match.group(1))
    else:
        raise ValueError(f"无法识别失败TXT记录: {text}")
    if period < 1 or not name:
        raise ValueError(f"失败TXT记录期数或名称无效: {text}")
    pick_match = re.search(r"(?:^|\s)方向:\s*(\S+)", tail)
    pick = normalize_pick(pick_match.group(1)) if pick_match else ""
    reason_match = re.search(r"(?:具体原因|原因):\s*(.*)$", tail)
    if reason_match is None:
        raise ValueError(f"失败TXT记录缺少具体原因: {text}")
    labels = tail[:reason_match.start()]
    category_match = re.search(r"失败类型:\s*(.*?)(?=\s+阶段:|$)", labels)
    stage_match = re.search(r"阶段:\s*(.*?)(?=\s+失败类型:|$)", labels)
    category = category_match.group(1).strip() if category_match else "历史记录"
    stage = stage_match.group(1).strip() if stage_match else "任务执行"
    configured = list(sites)
    if configured:
        candidates = [s for s in configured if
                      (not site_id or s.site_id == site_id) and s.name == name
                      and s.url == url and (not pick or s.pick == pick)]
        if len(candidates) != 1:
            raise ValueError(f"失败TXT记录无法唯一匹配当前站点身份: {text}")
        site_id, pick = candidates[0].site_id, candidates[0].pick
    return FailureRecord(site_id, name, url, pick, period, category, stage,
                         reason_match.group(1))


def parse_failure_records(text: str, sites: Iterable[Site] = ()) -> list[FailureRecord]:
    body = re.split(r"(?:^|\n)\s*失败分类统计\s*(?:\n|$)",
                    text.lstrip("\ufeff"), maxsplit=1)[0]
    records = []
    for block in re.split(r"\r?\n\s*\r?\n|\r?\n(?=失败\s|\d+期\s)", body):
        if block.strip():
            records.append(parse_failure_record(block, sites))
    seen = set()
    for record in records:
        key = record.site_id or (record.name, record.url, record.pick)
        if key in seen:
            raise ValueError(f"失败TXT包含重复站点: {key}")
        seen.add(key)
    return records
