from he_app.domain.models import Site
from he_app.services.single_period import evaluate_site_period


HTML = """
<html><body>
高手资料271期【必杀两合】已公开！ 作者:春回大地
270期：必杀二合{08.12}开19准
271期：必杀二合{03.02}开00准
</body></html>
"""


def test_spring_site_parses_bottom_two_sum_row():
    site = Site(
        "春回大地",
        "https://example.test/topic/443994.html",
        "bottom",
        True,
        False,
        "s152_topic_443994",
        2,
    )

    assert evaluate_site_period(site, 271, [HTML]) == (
        "03合,02合 春回大地",
        "271期:必杀二合(03.02)开00准",
        ["03合", "02合"],
        None,
    )
