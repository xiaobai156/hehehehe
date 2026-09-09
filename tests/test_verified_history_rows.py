from he_app.domain.models import Site
from he_app.services.verified_history_rows import extract_verified_history_rows


def test_abbreviated_dedicated_rows_are_extracted_from_locked_record() -> None:
    site = Site(
        "师出长安",
        "https://example.test/bbs/10699",
        "top",
        False,
        False,
        "s112_dd_62782b_bbs_10699",
    )
    document = (
        "绝杀一合 专属栏目\n"
        "213期:杀 [(10合)] 开:01准\n"
        "212期:杀 [(07合)] 开:49准\n"
        "211期:杀 [(03合)] 开:08准"
    )
    assert extract_verified_history_rows(site, [document]) == {
        213: "10合",
        212: "07合",
        211: "03合",
    }


def test_conflicting_same_period_is_excluded() -> None:
    site = Site(
        "测试站",
        "https://example.test/topic/1.html",
        "top",
        False,
        False,
        "s097_topic_250886",
    )
    document = (
        "213期 绝杀一合 [01合] 开00准\n"
        "212期 绝杀一合 [02合] 开00准\n"
        "212期 绝杀一合 [03合] 开00准"
    )
    assert extract_verified_history_rows(site, [document]) == {213: "01合"}


def test_unrelated_attribute_values_do_not_create_history() -> None:
    site = Site(
        "测试站",
        "https://example.test/topic/1.html",
        "top",
        False,
        False,
        "s097_topic_250886",
    )
    document = (
        "213期 绝杀一合 [01合] 开00准\n"
        "澳彩合数属性: 01合:01.10 02合:02.11.20"
    )
    assert extract_verified_history_rows(site, [document]) == {213: "01合"}
