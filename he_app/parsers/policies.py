import re

from he_app.domain.models import SiteRule


PERIOD_RE = re.compile(r"(?<!\d)(\d{1,4})\s*期")
VALUE_RE = re.compile(r"(?<!\d)([0-9０-９]{1,2})\s*合(?!\s*[:：])")
REVERSE_VALUE_RE = re.compile(r"合\s*([0-9０-９]{1,2})(?!\d)")
STRICT_SUCCESS_VALUE_RE = re.compile(r"^(?:0[1-9]|1[0-3])合$")
STRICT_KILL_SUM_RE = re.compile(
    r"绝\s*杀\s*一\s*合|絕\s*殺\s*一\s*合|"
    r"公式\s*杀\s*合|公式\s*殺\s*合|"
    r"绝\s*杀\s*合|絕\s*殺\s*合"
)
WEAK_KILL_SUM_RE = re.compile(
    r"[杀殺]\s*(?:一|1|①)?\s*合"
)
STRICT_NO_LOCATOR_SITE_IDS = {
    "s015_topic_206515",
    "s028_topic_349654",
    "s044_topic_445676",
    "s024_topic_350541",
    "s040_topic_287375",
    "s061_fllxjy",
    "s081_users_2121",
    "s082_users_3722",
    "s079_users_3722",
    "s080_forums_15361483",
    "s081_topic_281519",
    "s087_enpcjg",
    "s088_mm_737799b_art_8129",
    "s090_667755_gsb_022",
}
SITE_RULES = {
    site_id: SiteRule(require_body_locator=False, note="strict candidate without body locator")
    for site_id in STRICT_NO_LOCATOR_SITE_IDS
}
SITE_RULES["s015_topic_206515"] = SiteRule(
    require_body_locator=False,
    note="anchor 作者:返回来个 then first data row must match manual period",
    anchor_text="作者:返回来个",
    latest_after_anchor=True,
)
SITE_RULES["s016_topic_497780"] = SiteRule(
    require_body_locator=False,
    note="strict current-period top candidate without reliable body locator",
)
SITE_RULES["s017_topic_225131"] = SiteRule(
    require_body_locator=False,
    note="anchor 作者:布天盖地 then selected data row must match manual period",
    anchor_text="作者:布天盖地",
    latest_after_anchor=True,
)
SITE_RULES["s019_topic_324685"] = SiteRule(
    require_body_locator=False,
    note="anchor 无中生有 then selected data row must match manual period",
    anchor_text="无中生有",
    latest_after_anchor=True,
)
SITE_RULES["s006_topic_255343"] = SiteRule(
    require_body_locator=False,
    note="anchor 作者:亡魂丧魄 then selected data row must match manual period",
    anchor_text="作者:亡魂丧魄",
    latest_after_anchor=True,
)
SITE_RULES["s007_topic_206633"] = SiteRule(
    require_body_locator=False,
    note="strict current-period bottom candidate without reliable body locator",
)
SITE_RULES["s008_topic_250654"] = SiteRule(
    require_body_locator=False,
    note="strict current-period bottom candidate without reliable anchor",
)
SITE_RULES["s002_topic_247357"] = SiteRule(
    require_body_locator=False,
    note="strict current-period bottom candidate without reliable body locator",
)
SITE_RULES["s011_sk7_fago"] = SiteRule(
    note="site uses weak 杀①合 wording",
    allow_weak_kill_sum_keyword=True,
)
SITE_RULES["s013_topic_226261"] = SiteRule(
    require_body_locator=False,
    note="bottom edge strict candidate without reliable body locator",
    allowed_fetch_kinds=("legacy", "http", "http-decoded", "browser"),
)
SITE_RULES["s026_topic_504960"] = SiteRule(
    require_body_locator=False,
    note="anchor 作者:花花不语 then selected data row must match manual period",
    anchor_text="作者:花花不语",
    latest_after_anchor=True,
)
SITE_RULES["s023_topic_350518"] = SiteRule(
    require_body_locator=False,
    note="anchor 佳人旧梦 then selected data row must match manual period",
    anchor_text="佳人旧梦",
    latest_after_anchor=True,
)
SITE_RULES["s020_fklgrq"] = SiteRule(
    require_body_locator=False,
    note="strict current-period top candidate without reliable anchor",
)
SITE_RULES["s025_topic_435508"] = SiteRule(
    require_body_locator=False,
    note="anchor 胡说八道 then selected data row must match manual period",
    anchor_text="胡说八道",
    latest_after_anchor=True,
)
SITE_RULES["s028_topic_349654"] = SiteRule(
    require_body_locator=False,
    note="anchor 归途他梦 then selected data row must match manual period",
    anchor_text="归途他梦",
    latest_after_anchor=True,
)
SITE_RULES["s030_topic_351192"] = SiteRule(
    require_body_locator=False,
    note="anchor 勤勤恳恳 then selected data row must match manual period",
    anchor_text="勤勤恳恳",
    latest_after_anchor=True,
)
SITE_RULES["s034_topic_322671"] = SiteRule(
    require_body_locator=False,
    note="anchor 作者:阳光记忆 then selected data row must match manual period",
    anchor_text="作者:阳光记忆",
    latest_after_anchor=True,
)
SITE_RULES["s031_topic_437742"] = SiteRule(
    require_body_locator=False,
    note="anchor 活泼开朗 then selected data row must match manual period",
    anchor_text="活泼开朗",
    latest_after_anchor=True,
)
SITE_RULES["s044_topic_445676"] = SiteRule(
    require_body_locator=False,
    note="anchor 百花齐放 then selected data row must match manual period",
    anchor_text="百花齐放",
    latest_after_anchor=True,
)
SITE_RULES["s024_topic_350541"] = SiteRule(
    require_body_locator=False,
    note="anchor 南风过境 then selected data row must match manual period",
    anchor_text="南风过境",
    latest_after_anchor=True,
)
SITE_RULES["s041_topic_249367"] = SiteRule(
    require_body_locator=False,
    note="strict current-period bottom candidate without reliable anchor",
)
SITE_RULES["s045_topic_272180"] = SiteRule(
    require_body_locator=False,
    note="anchor 官逼民反 then selected data row must match manual period",
    anchor_text="官逼民反",
    latest_after_anchor=True,
)
SITE_RULES["s046_topic_178774"] = SiteRule(
    require_body_locator=False,
    note="anchor 杀神在世 then selected data row must match manual period",
    anchor_text="杀神在世",
    latest_after_anchor=True,
)
SITE_RULES["s052_topic_206523"] = SiteRule(
    require_body_locator=False,
    note="anchor 作者:大田方是 then selected data row must match manual period",
    anchor_text="作者:大田方是",
    latest_after_anchor=True,
)
SITE_RULES["s049_topic_792936"] = SiteRule(
    require_body_locator=False,
    note="anchor 满面红光 then selected data row must match manual period",
    anchor_text="满面红光",
    latest_after_anchor=True,
)
SITE_RULES["s062_topic_349555"] = SiteRule(
    require_body_locator=False,
    note="anchor 悉悉索索 then selected data row must match manual period",
    anchor_text="悉悉索索",
    latest_after_anchor=True,
)
SITE_RULES["s063_topic_283475"] = SiteRule(
    require_body_locator=False,
    note="anchor 怪咖少年 then selected data row must match manual period",
    anchor_text="怪咖少年",
    latest_after_anchor=True,
)
SITE_RULES["s037_topic_252205"] = SiteRule(
    require_body_locator=False,
    note="strict current-period bottom candidate without reliable anchor",
)
SITE_RULES["s035_topic_417590"] = SiteRule(
    require_body_locator=False,
    note="anchor 清爽无敌 then selected data row must match manual period",
    anchor_text="清爽无敌",
    latest_after_anchor=True,
)
SITE_RULES["s038_topic_455540"] = SiteRule(
    require_body_locator=False,
    note="anchor 风靡全球 then selected data row must match manual period",
    anchor_text="风靡全球",
    latest_after_anchor=True,
)
SITE_RULES["s047_topic_256745"] = SiteRule(
    require_body_locator=False,
    note="anchor 作者:法无二门 then selected data row must match manual period",
    anchor_text="作者:法无二门",
    latest_after_anchor=True,
)
SITE_RULES["s050_topic_680730"] = SiteRule(
    require_body_locator=False,
    note="anchor 画龙点睛 post header then selected data row must match manual period",
    anchor_text="画龙点睛 发表于",
    latest_after_anchor=True,
)
SITE_RULES["s051_topic_702074"] = SiteRule(
    require_body_locator=False,
    note="anchor 作者:坐收其利 then selected data row must match manual period",
    anchor_text="作者:坐收其利",
    latest_after_anchor=True,
)
SITE_RULES["s055_topic_225617"] = SiteRule(
    require_body_locator=False,
    note="anchor 濯缨弹冠 then selected data row must match manual period",
    anchor_text="濯缨弹冠",
    latest_after_anchor=True,
)
SITE_RULES["s056_topic_220982"] = SiteRule(
    require_body_locator=False,
    note="anchor 杏腮桃脸 then selected data row must match manual period",
    anchor_text="杏腮桃脸",
    latest_after_anchor=True,
)
SITE_RULES["s057_topic_227386"] = SiteRule(
    require_body_locator=False,
    note="bottom edge strict candidate without reliable body locator",
    allowed_fetch_kinds=("legacy", "http", "http-decoded", "browser"),
)
SITE_RULES["s059_topic_324760"] = SiteRule(
    require_body_locator=False,
    note="anchor 春风化雨 then selected data row must match manual period",
    anchor_text="春风化雨",
    latest_after_anchor=True,
)
SITE_RULES["s060_topic_589491"] = SiteRule(
    require_body_locator=False,
    note="bottom special archive candidate from verified rendered page",
    allowed_fetch_kinds=("legacy", "http", "http-decoded", "browser"),
)
SITE_RULES["s064_topic_268622"] = SiteRule(
    require_body_locator=False,
    note="strict current-period bottom candidate without reliable anchor",
)
SITE_RULES["s065_topic_437721"] = SiteRule(
    require_body_locator=False,
    note="anchor 鸡飞蛋打 then selected data row must match manual period",
    anchor_text="鸡飞蛋打",
    latest_after_anchor=True,
)
SITE_RULES["s066_topic_463139"] = SiteRule(
    require_body_locator=False,
    note="anchor 春花烂漫 then bottom current rows must match manual period",
    anchor_text="春花烂漫",
    latest_after_anchor=True,
)
SITE_RULES["s004_topic_225401"] = SiteRule(
    require_body_locator=False,
    note="anchor 默契神会 then selected data row must match manual period",
    anchor_text="默契神会",
    latest_after_anchor=True,
)
SITE_RULES["s009_topic_250869"] = SiteRule(
    require_body_locator=False,
    note="strict current-period bottom candidate without reliable anchor",
)
SITE_RULES["s010_topic_250856"] = SiteRule(
    require_body_locator=False,
    note="anchor 射雕英雄 then selected data row must match manual period",
    anchor_text="射雕英雄",
    latest_after_anchor=True,
)
SITE_RULES["s027_topic_436712"] = SiteRule(
    require_body_locator=False,
    note="anchor 古往今来 then selected data row must match manual period",
    anchor_text="古往今来",
    latest_after_anchor=True,
)
SITE_RULES["s032_topic_309383"] = SiteRule(
    require_body_locator=False,
    note="anchor 踏雪无痕网 then selected data row must match manual period",
    anchor_text="踏雪无痕网",
    latest_after_anchor=True,
)
SITE_RULES["s033_topic_309365"] = SiteRule(
    require_body_locator=False,
    note="strict current-period top candidate without reliable anchor",
)
SITE_RULES["s039_topic_455521"] = SiteRule(
    require_body_locator=False,
    note="anchor 久梦初醒 then selected data row must match manual period",
    anchor_text="久梦初醒",
    latest_after_anchor=True,
)
SITE_RULES["s042_topic_439183"] = SiteRule(
    require_body_locator=False,
    note="anchor 作茧自缚 then selected data row must match manual period",
    anchor_text="作茧自缚",
    latest_after_anchor=True,
)
SITE_RULES["s043_topic_252215"] = SiteRule(
    require_body_locator=False,
    note="anchor 作舍道旁 then selected data row must match manual period",
    anchor_text="作舍道旁",
    latest_after_anchor=True,
)

SITE_RULES["s067_topic_680695"] = SiteRule(
    require_body_locator=False,
    note="strict current-period top candidate without reliable body locator",
)
SITE_RULES["s068_topic_253482"] = SiteRule(
    require_body_locator=False,
    note="strict current-period top candidate without reliable body locator",
)
SITE_RULES["s069_topic_682018"] = SiteRule(
    require_body_locator=False,
    note="strict current-period bottom candidate without reliable body locator",
)
SITE_RULES["s070_topic_246762"] = SiteRule(
    require_body_locator=False,
    note="strict current-period bottom candidate without reliable body locator",
)
SITE_RULES["s071_topic_768615"] = SiteRule(
    require_body_locator=False,
    note="strict current-period bottom candidate without reliable body locator",
)
SITE_RULES["s098_topic_227257"] = SiteRule(
    require_body_locator=False,
    note="雷锋第一版专属稳杀一合，仍执行顶部第一条边界",
    allow_weak_kill_sum_keyword=True,
)
SITE_RULES["s073_shuqhbq"] = SiteRule(
    require_body_locator=False,
    note="dedicated 绝杀①段①合 table parser, sum column only",
)

BODY_LOCATOR_RE = re.compile(
    r"期数|开奖|作者|楼主|發布|发布|发表于|發表於|发表于|發表于|发贴于|發貼於|"
    r"杀料专区|殺料專區|高手论坛|高手論壇|澳门通天报|澳門通天報|综合杀|綜合殺|"
    r"已公开|已公開|提高速度|减少浏览流量|不保留大量往期记录|合数属性|合數屬性"
)
