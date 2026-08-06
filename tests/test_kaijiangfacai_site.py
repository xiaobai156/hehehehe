import pytest

from he_app.domain.errors import DedicatedCandidateConflict
from he_app.domain.models import Site
from he_app.parsers.dedicated.kaijiangfacai import (
    extract_kaijiangfacai_kill_sum_period_values,
    find_kaijiangfacai_kill_sum_candidate_with_direction,
)
from he_app.services.fingerprint import build_site_fingerprint
from he_app.services.single_period import parse_site_period


SITE_ID = "s131_kjfc_234432"


def site(pick: str = "bottom") -> Site:
    return Site(
        "开奖发财",
        "https://156.225.88.144:12098/#234432",
        pick,
        False,
        False,
        SITE_ID,
    )


def target_table(rows: list[tuple[str, str, str, str, str, str]]) -> str:
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"""
    <div class="list-title">开奖发财【综合杀料】11447.COM</div>
    <table>
      <thead><tr><td>期数</td><td>杀尾</td><td>杀肖</td><td>杀合</td><td>杀波</td><td>开奖</td></tr></thead>
      <tbody>{body}</tbody>
    </table>
    """


def valid_rows() -> list[tuple[str, str, str, str, str, str]]:
    return [
        ("215期", "2尾", "龙肖", "08", "红波", "开:蛇14"),
        ("216期", "1尾", "牛肖", "07", "蓝波", "开:马37"),
        ("217期", "7尾", "龙肖", "06", "红波", "开:赚99"),
        ("218期", "?尾", "?", "?", "?", "开:赚99"),
    ]


def test_bottom_uses_last_valid_row_and_ignores_invalid_placeholder() -> None:
    document = target_table(valid_rows())

    result = parse_site_period(site(), 217, [document])

    assert result.success and result.value == "06合 开奖发财"


def test_adjacent_valid_period_is_outside_bottom_direction() -> None:
    result = parse_site_period(site(), 216, [target_table(valid_rows())])

    assert not result.success
    assert result.failure and result.failure.category == "方向范围外"


def test_nonexistent_period_is_not_reported_as_direction_failure() -> None:
    result = parse_site_period(site(), 219, [target_table(valid_rows())])

    assert not result.success
    assert result.failure and result.failure.category == "无当期"


@pytest.mark.parametrize(
    "document",
    [
        target_table(valid_rows()).replace("开奖发财【综合杀料】", "开奖发财【四码中特】"),
        target_table(valid_rows()).replace("<td>杀合</td>", "<td>杀码</td>"),
        target_table(valid_rows()).replace("<td>06</td>", "<td>14</td>"),
        target_table(valid_rows()).replace("<td>06</td>", "<td>06 07</td>"),
    ],
)
def test_wrong_anchor_column_or_value_cannot_succeed(document: str) -> None:
    result = parse_site_period(site(), 217, [document])

    assert not result.success


def test_other_tables_cannot_supply_anchor_or_kill_sum_column() -> None:
    anchor_only = target_table(valid_rows()).replace("<table>", "<section>").replace(
        "</table>", "</section>"
    )
    data_only = target_table(valid_rows()).replace(
        '<div class="list-title">开奖发财【综合杀料】11447.COM</div>', ""
    )

    result = parse_site_period(site(), 217, [anchor_only, data_only])

    assert not result.success


def test_conflicting_authoritative_documents_fail() -> None:
    first = target_table(valid_rows())
    second = first.replace("<td>06</td>", "<td>05</td>")

    with pytest.raises(DedicatedCandidateConflict):
        extract_kaijiangfacai_kill_sum_period_values([first, second])


def test_identical_duplicate_documents_are_deduplicated() -> None:
    document = target_table(valid_rows())

    candidate, outside = find_kaijiangfacai_kill_sum_candidate_with_direction(
        [document, document], 217, "bottom"
    )

    assert candidate and candidate.values == "06合"
    assert not outside


def test_fingerprint_uses_full_target_table_history() -> None:
    rows = [
        (f"{period}期", "1尾", "牛肖", f"{(period % 13) + 1:02d}", "蓝波", "开:00")
        for period in range(208, 218)
    ]
    rows.append(("218期", "?尾", "?", "?", "?", "开:赚99"))

    fingerprint = build_site_fingerprint(site(), [target_table(rows)], 217, 10)

    assert list(fingerprint) == list(range(217, 207, -1))
    assert fingerprint[217] == "10合"
    assert fingerprint[208] == "01合"
