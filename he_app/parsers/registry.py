from collections.abc import Callable

from he_app.domain.errors import DedicatedCandidateConflict
from he_app.domain.models import Candidate, Site
from he_app.domain.policies import normalize_pick
from he_app.parsers.common import (
    analyze_missing_reason,
    current_candidates_outside_window,
    directional_three_label,
    is_valid_success_value,
    pick_region_label,
    trusted_candidate_with_conflict,
)
from he_app.parsers.dedicated.history import (
    BATCH_NEW_DEDICATED_SITE_IDS,
    dedicated_directional_window_failure,
    find_anchor_latest_candidate,
    find_batch_new_site_kill_sum_candidate,
    find_baxianguohai_kill_sum_candidate,
    find_change_archive_kill_sum_candidate,
    find_chunfenghuayu_kill_sum_candidate,
    find_directional_cycle_kill_sum_candidate,
    find_huluntunzao_kill_sum_candidate,
    find_hushuobadao_kill_sum_candidate,
    find_jincaishen_kill_sum_candidate,
    find_liangjian_kill_sum_candidate,
    find_named_anchor_history_candidate,
    find_ruyimutan_kill_sum_candidate_with_direction,
    find_saima_archive_kill_sum_candidate,
    find_toutianhuanri_kill_sum_candidate_with_direction,
    find_woyaobaoma_kill_sum_candidate,
    find_xianrenzhilu_kill_sum_candidate,
    find_yaoweiqiushi_kill_sum_candidate,
    find_yidianhong_kill_sum_candidate,
    find_yizhiluanuyan_kill_sum_candidate,
    find_youzuichunshe_kill_sum_candidate,
)
from he_app.parsers.dedicated.gucheng import (
    GUCHENG_SITE_ID,
    find_gucheng_kill_sum_candidate_with_direction,
)
from he_app.parsers.dedicated.article_content import (
    ARTICLE_CONTENT_SITE_IDS,
    find_article_content_candidate_with_direction,
)
from he_app.parsers.dedicated.structured import (
    DAJIAFA_SITE_ID,
    YIAIZHIMING_SITE_ID,
    find_dajiafa_kill_sum_candidate,
    find_yiaizhimin_kill_sum_candidate,
)
from he_app.parsers.dedicated.kaijiangfacai import (
    KAIJIANGFACAI_SITE_ID,
    find_kaijiangfacai_kill_sum_candidate_with_direction,
)
from he_app.parsers.dedicated.blackpepper import (
    BLACKPEPPER_SITE_ID,
    find_blackpepper_candidate_with_direction,
)
from he_app.parsers.dedicated.tables import (
    find_jiuxiao_kill_sum_candidate_with_direction,
    find_tongtian_kill_sum_candidate_with_direction,
    find_woman_flavor_sum_candidate_with_direction,
    format_success_result,
    site_rule,
)
from he_app.parsers.dedicated.ttss import (
    TTSS_CHENYUAN_SITE_ID,
    TTSS_QIFENG_SITE_ID,
    TTSS_YIKAO_SITE_ID,
    find_ttss_kill_sum_candidate_for_site,
)


Evaluation = tuple[str | None, str, list[str], str | None]
Parser = Callable[[Site, int, list[str]], Evaluation]


class ParserRegistry:
    def __init__(self, default: Parser):
        self.default = default
        self._parsers: dict[str, Parser] = {}

    def register(self, site_id: str, parser: Parser) -> None:
        if site_id in self._parsers:
            raise ValueError(f"重复解析器: {site_id}")
        self._parsers[site_id] = parser

    def evaluate(self, site: Site, period: int, documents: list[str]) -> Evaluation:
        return self._parsers.get(site.site_id, self.default)(site, period, documents)


def _success(site: Site, period: int, candidate: Candidate) -> Evaluation:
    return format_success_result(site, period, candidate), candidate.line, candidate.values.split(","), None


def _missing(site: Site, period: int, detail: str, reason: str = "专属缺期") -> Evaluation:
    return None, detail.format(site=site.name, period=period), [], reason


def _parse_hushuobadao(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate, outside_direction, found_anchor = find_hushuobadao_kill_sum_candidate(documents, period, site.pick)
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        return None, f"{site.name} {period}期不是顶部区块第一条专属历史行", [], "方向范围外"
    if not found_anchor:
        return None, f"{site.name} 没找到作者锚点: 胡说八道", [], "锚点缺失"
    return None, f"{site.name} 作者块下没找到{period}期严格杀合数据", [], "作者块无当期"


def _parse_ruyimutan(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate, outside_direction, window_periods = find_ruyimutan_kill_sum_candidate_with_direction(
        documents, period, site.pick
    )
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        actual_window = "/".join(f"{item}期" for item in window_periods) or "空"
        return (
            None,
            f"{site.name} {period}期不是{directional_three_label(site.pick)}作者主正文边界行；"
            f"实际窗口: {actual_window}",
            [],
            "方向范围外",
        )
    return _missing(site, period, "{site} 作者主正文里没找到{period}期绝杀一合", "无当期")


def _parse_yiaizhimin(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate, outside_direction = find_yiaizhimin_kill_sum_candidate(documents, period, site.pick)
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        return None, f"{site.name} {period}期不是尾部区块最后一条专属历史行", [], "方向范围外"
    return _missing(site, period, "{site} URL记录ID专属正文里没找到{period}期绝杀一合")


def _parse_dajiafa(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate, outside_direction = find_dajiafa_kill_sum_candidate(documents, period, site.pick)
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        return None, f"{site.name} {period}期不是{pick_region_label(site.pick)}边界专属历史行", [], "方向范围外"
    return _missing(site, period, "{site} 标题、作者和正文同一专属块里没找到{period}期绝杀一合", "无当期")


def _parse_xianrenzhilu(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate = find_xianrenzhilu_kill_sum_candidate(documents, period, normalize_pick(site.pick))
    return _success(site, period, candidate) if candidate else _missing(
        site, period, "{site} 标题锚点后的专属历史行里没找到{period}期绝杀一合"
    )


def _parse_baxianguohai(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate = find_baxianguohai_kill_sum_candidate(documents, period, normalize_pick(site.pick))
    return _success(site, period, candidate) if candidate else _missing(
        site, period, "{site} 专属块到澳门六合彩合数属性前没找到{period}期绝杀一合"
    )


def _parse_tongtian(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate, outside_direction = find_tongtian_kill_sum_candidate_with_direction(
        documents, period, normalize_pick(site.pick)
    )
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        return (
            None,
            f"{site.name} {period}期不是{directional_three_label(site.pick)}专属表格边界行",
            [],
            "方向范围外",
        )
    return _missing(site, period, "{site} 澳门综合杀表格里没找到{period}期杀合", "未找到目标")


def _parse_jiuxiao(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate, outside_direction = find_jiuxiao_kill_sum_candidate_with_direction(
        documents, period, normalize_pick(site.pick)
    )
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        return None, f"{site.name} {period}期不是顶部专属合数边界行", [], "方向范围外"
    return _missing(
        site, period, "{site} 绝杀①段①合表格里没找到{period}期杀合", "未找到目标"
    )


def _parse_woman_flavor(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate, outside_direction = find_woman_flavor_sum_candidate_with_direction(
        documents, period, normalize_pick(site.pick)
    )
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        return None, f"{site.name} {period}期不是顶部专属二合边界行", [], "方向范围外"
    return _missing(
        site, period, "{site} 绝杀一尾二合表格里没找到{period}期合数", "未找到目标"
    )


def _parse_kaijiangfacai(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate, outside_direction = find_kaijiangfacai_kill_sum_candidate_with_direction(
        documents, period, normalize_pick(site.pick)
    )
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        return (
            None,
            f"{site.name} {period}期不是尾部综合杀料表最后一条有效杀合行",
            [],
            "方向范围外",
        )
    return _missing(
        site,
        period,
        "{site} 综合杀料表杀合列里没找到{period}期合法值",
        "无当期",
    )


def _parse_blackpepper(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate, outside_direction = find_blackpepper_candidate_with_direction(
        site, documents, period, site.pick
    )
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        return None, f"{site.name} {period}期不是顶部专属历史第一条有效行", [], "方向范围外"
    return _missing(site, period, "{site} 专属topic 805245里没找到{period}期绝杀合数", "无当期")


def _parse_required(
    finder: Callable[[list[str], int, str], Candidate | None],
    detail: str,
    reason: str = "专属缺期",
) -> Parser:
    def parser(site: Site, period: int, documents: list[str]) -> Evaluation:
        candidate = finder(documents, period, normalize_pick(site.pick))
        return _success(site, period, candidate) if candidate else _missing(site, period, detail, reason)

    return parser


def _parse_named_anchor(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate = find_named_anchor_history_candidate(documents, period, normalize_pick(site.pick), site.name)
    return _success(site, period, candidate) if candidate else _missing(
        site, period, "{site} 名称锚点专属块里没找到{period}期唯一绝杀一合"
    )


def _parse_batch(site: Site, period: int, documents: list[str]) -> Evaluation:
    rule = site_rule(site)
    candidate = find_batch_new_site_kill_sum_candidate(
        documents,
        period,
        normalize_pick(site.pick),
        detect_conflict=True,
        allow_weak=rule.allow_weak_kill_sum_keyword,
    )
    return _success(site, period, candidate) if candidate else _missing(
        site, period, "{site} 专属块里没找到{period}期唯一杀一合"
    )


def _parse_ttss(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate = find_ttss_kill_sum_candidate_for_site(
        documents, period, normalize_pick(site.pick), site.name
    )
    return _success(site, period, candidate) if candidate else _missing(
        site,
        period,
        "{site} 列表定位的同名绝杀一合文章顶部没找到{period}期唯一杀合",
    )


def _parse_directional_cycle(
    site: Site,
    period: int,
    documents: list[str],
    anchor: str | None = None,
) -> Evaluation:
    candidate, outside_direction = find_directional_cycle_kill_sum_candidate(
        documents, period, normalize_pick(site.pick), anchor
    )
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        return (
            None,
            f"{site.name} {period}期不是{directional_three_label(site.pick)}专属历史边界行",
            [],
            "方向范围外",
        )
    return _missing(site, period, "{site} 所选方向专属块里没找到{period}期唯一绝杀一合")


def _parse_xianrenhouji(site: Site, period: int, documents: list[str]) -> Evaluation:
    return _parse_directional_cycle(site, period, documents, "作者:先人后己")


def _parse_leifeng_second(site: Site, period: int, documents: list[str]) -> Evaluation:
    return _parse_directional_cycle(site, period, documents)


def _parse_yingba(site: Site, period: int, documents: list[str]) -> Evaluation:
    return _parse_directional_cycle(site, period, documents, "盈把之木")


def _parse_shushen(site: Site, period: int, documents: list[str]) -> Evaluation:
    return _parse_directional_cycle(site, period, documents, "束身自修")


def _parse_batch_author(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate = find_batch_new_site_kill_sum_candidate(
        documents, period, normalize_pick(site.pick), detect_conflict=True
    )
    return _success(site, period, candidate) if candidate else _missing(
        site, period, "{site} 作者块里没找到{period}期唯一绝杀一合"
    )


def _parse_gucheng(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate, outside_direction = find_gucheng_kill_sum_candidate_with_direction(
        site, documents, period, normalize_pick(site.pick)
    )
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        return (
            None,
            f"{site.name} {period}期不是尾部专属杀2合数边界行",
            [],
            "方向范围外",
        )
    return _missing(
        site,
        period,
        "{site} 作者故城笙声的杀2合数专属块里没找到{period}期合数",
        "无当期",
    )


def _parse_article_content(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate, outside_direction = find_article_content_candidate_with_direction(
        site, documents, period, normalize_pick(site.pick)
    )
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        return (
            None,
            f"{site.name} {period}期不是顶部文章专属块第一条有效候选",
            [],
            "方向范围外",
        )
    return _missing(
        site,
        period,
        "{site} Article/ar_content专属文章块里没找到{period}期合数",
        "无当期",
    )


def _parse_toutianhuanri(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate, outside_direction = find_toutianhuanri_kill_sum_candidate_with_direction(
        documents, period, normalize_pick(site.pick)
    )
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        return (
            None,
            f"{site.name} {period}期不是{directional_three_label(site.pick)}专属历史边界行",
            [],
            "方向范围外",
        )
    return _missing(site, period, "{site} 专属块里没找到{period}期稳杀一合", "无当期")


def _parse_munan(site: Site, period: int, documents: list[str]) -> Evaluation:
    return _parse_directional_cycle(site, period, documents, "木南少年")


def _parse_saima(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate, outside_direction, found_archive = find_saima_archive_kill_sum_candidate(
        documents, period, normalize_pick(site.pick)
    )
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        return (
            None,
            f"{site.name} {period}期不是{directional_three_label(site.pick)}特殊历史归档边界行",
            [],
            "方向范围外",
        )
    if not found_archive:
        return None, f"{site.name} 没找到标题、作者和正文同属一条记录的特殊历史归档块", [], "锚点缺失"
    return _missing(site, period, "{site} 特殊历史归档块里没找到{period}期绝杀一合", "无当期")


def _parse_change(site: Site, period: int, documents: list[str]) -> Evaluation:
    candidate, outside_direction, found_archive = find_change_archive_kill_sum_candidate(
        documents, period, normalize_pick(site.pick)
    )
    if candidate is not None:
        return _success(site, period, candidate)
    if outside_direction:
        return (
            None,
            f"{site.name} {period}期不是{directional_three_label(site.pick)}嫦娥彩报绝杀一合边界行",
            [],
            "方向范围外",
        )
    if not found_archive:
        return None, f"{site.name} 没找到嫦娥彩报绝杀一合同区块", [], "锚点缺失"
    return _missing(site, period, "{site} 嫦娥彩报绝杀一合区块里没找到{period}期", "无当期")


def _parse_generic(site: Site, period: int, documents: list[str]) -> Evaluation:
    rule = site_rule(site)
    pick = normalize_pick(site.pick)
    value_count = site.value_count
    candidate, anchor_category, anchor_reason = find_anchor_latest_candidate(site, documents, period)
    if anchor_category is not None:
        category = "方向范围外" if anchor_category == "超出范围" else anchor_category
        return None, anchor_reason or "", [], category

    if candidate is None:
        candidate, conflict_values, conflict_lines = trusted_candidate_with_conflict(
            documents,
            period,
            pick,
            require_body_locator=rule.require_body_locator,
            allow_weak=rule.allow_weak_kill_sum_keyword,
            value_count=value_count,
        )
        if conflict_values:
            return None, (
                f"{site.name} {period}期多个高可信候选结果冲突: {' / '.join(conflict_values)}；"
                f"候选: {' | '.join(conflict_lines[:5])}"
            ), [], "候选冲突"
    if candidate is None:
        if current_candidates_outside_window(
            documents,
            period,
            pick,
            rule.require_body_locator,
            rule.allow_weak_kill_sum_keyword,
            value_count,
        ):
            return None, (
                f"{pick_region_label(site.pick)}的{period}期严格候选不在"
                f"{directional_three_label(pick)}高可信候选边界"
            ), [], "超出范围"
        category, reason = analyze_missing_reason(
            documents, period, pick, rule.allow_weak_kill_sum_keyword, value_count
        )
        return None, reason, [], category

    rank_values = candidate.values.split(",")
    if len(rank_values) != value_count or not all(is_valid_success_value(value) for value in rank_values):
        return None, f"{candidate.line} 提取值不在01-13合范围", [], "数据不完整"
    return _success(site, period, candidate)


def _parse_two_value_anchor(site: Site, period: int, documents: list[str]) -> Evaluation:
    """Dedicated registry binding for explicitly configured two-sum sites."""

    return _parse_generic(site, period, documents)


def build_registry() -> ParserRegistry:
    registry = ParserRegistry(_parse_generic)
    registrations: dict[str, Parser] = {
        "s025_topic_435508": _parse_hushuobadao,
        "s109_topic_222783": _parse_ruyimutan,
        YIAIZHIMING_SITE_ID: _parse_yiaizhimin,
        DAJIAFA_SITE_ID: _parse_dajiafa,
        "s088_mm_737799b_art_8129": _parse_xianrenzhilu,
        "s117_993345_gsb_025": _parse_baxianguohai,
        "s058_vkjwinyt": _parse_tongtian,
        "s073_shuqhbq": _parse_jiuxiao,
        "s085_kcvpleh": _parse_woman_flavor,
        KAIJIANGFACAI_SITE_ID: _parse_kaijiangfacai,
        BLACKPEPPER_SITE_ID: _parse_blackpepper,
        "s013_topic_226261": _parse_yingba,
        "s057_topic_227386": _parse_shushen,
        "s071_topic_768615": _parse_munan,
        "s002_topic_247357": _parse_required(
            find_jincaishen_kill_sum_candidate,
            "{site} 专属历史行里没找到{period}期唯一绝杀一合",
        ),
        "s070_topic_246762": _parse_required(
            find_yidianhong_kill_sum_candidate,
            "{site} 专属历史行里没找到{period}期唯一绝杀一合",
        ),
        "s077_topic_233118": _parse_required(
            find_youzuichunshe_kill_sum_candidate,
            "{site} 作者油嘴油舌的专属块里没找到{period}期绝杀一合",
        ),
        "s094_topic_727508": _parse_toutianhuanri,
        "s095_kk_212557a_art_zhuanqu_8149": _parse_required(
            find_woyaobaoma_kill_sum_candidate,
            "{site} 作者块里没找到{period}期绝杀一合",
        ),
        "s096_topic_206671": _parse_required(
            find_yaoweiqiushi_kill_sum_candidate,
            "{site} 作者块主历史里没找到{period}期绝杀一合",
        ),
        "s029_topic_336887": _parse_xianrenhouji,
        "s099_topic_464275": _parse_leifeng_second,
        "s043_topic_252215": _parse_batch_author,
        "s091_2_www39169b_gsbl_s01": _parse_required(
            find_yizhiluanuyan_kill_sum_candidate,
            "{site} 专属栏目里没找到{period}期绝杀一合",
        ),
        "s092_a_995546_gsb_aspx_id_amjyb036": _parse_required(
            find_huluntunzao_kill_sum_candidate,
            "{site} 专属栏目里没找到{period}期绝杀一合",
        ),
        "s093_a_909922_article_aspx_id_3694545": _parse_required(
            find_liangjian_kill_sum_candidate,
            "{site} 专属栏目里没找到{period}期公式杀合",
        ),
        "s059_topic_324760": _parse_required(
            find_chunfenghuayu_kill_sum_candidate,
            "{site} 作者块里没找到{period}期绝杀一合",
        ),
        "s060_topic_589491": _parse_saima,
        "s087_enpcjg": _parse_change,
        TTSS_YIKAO_SITE_ID: _parse_ttss,
        TTSS_CHENYUAN_SITE_ID: _parse_ttss,
        TTSS_QIFENG_SITE_ID: _parse_ttss,
        "s135_topic_481646": _parse_two_value_anchor,
        "s136_topic_677676": _parse_two_value_anchor,
        "s137_topic_682109": _parse_two_value_anchor,
        GUCHENG_SITE_ID: _parse_gucheng,
    }
    for site_id in ARTICLE_CONTENT_SITE_IDS:
        registrations[site_id] = _parse_article_content
    for site_id in ("s069_topic_682018", "s083_topic_242261"):
        registrations[site_id] = _parse_named_anchor
    for site_id in BATCH_NEW_DEDICATED_SITE_IDS:
        registrations.setdefault(site_id, _parse_batch)
    for site_id, parser in registrations.items():
        registry.register(site_id, parser)
    return registry


REGISTRY = build_registry()
WINDOW_PRECHECK_EXEMPT = {
    "s025_topic_435508",
    "s109_topic_222783",
    YIAIZHIMING_SITE_ID,
    DAJIAFA_SITE_ID,
    "s088_mm_737799b_art_8129",
    "s117_993345_gsb_025",
    "s073_shuqhbq",
    "s085_kcvpleh",
    KAIJIANGFACAI_SITE_ID,
    BLACKPEPPER_SITE_ID,
    "s070_topic_246762",
    "s029_topic_336887",
    "s099_topic_464275",
    "s060_topic_589491",
    "s087_enpcjg",
    "s094_topic_727508",
    TTSS_YIKAO_SITE_ID,
    TTSS_CHENYUAN_SITE_ID,
    TTSS_QIFENG_SITE_ID,
}
WINDOW_PRECHECK_EXEMPT.update(ARTICLE_CONTENT_SITE_IDS)


def evaluate_site_documents(site: Site, period: int, documents: list[str]) -> Evaluation:
    try:
        if site.site_id not in WINDOW_PRECHECK_EXEMPT:
            window_failure = dedicated_directional_window_failure(site, period, documents)
            if window_failure is not None:
                return window_failure
        return REGISTRY.evaluate(site, period, documents)
    except DedicatedCandidateConflict as exc:
        return (
            None,
            f"{site.name} {period}期多个专属候选结果冲突: {' / '.join(exc.values)}；"
            f"候选: {' | '.join(exc.lines[:5])}",
            [],
            "候选冲突",
        )
