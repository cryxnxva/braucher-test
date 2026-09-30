"""Unit-тесты сборщика воронки. Данные синтетические, сети нет."""

from datetime import timedelta
from typing import Any

import pytest

import fetch_sales_funnel as ff
from common import msk_today

NM_IDS_FIXTURE = (111111111, 222222222, 333333333)


def period_dates() -> list[str]:
    """[T-7; T-1] по Москве — те же границы, что у боевого запроса."""
    today = msk_today()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(7, 0, -1)]


def day_row(day: str, seed: int) -> dict[str, Any]:
    """Дневная запись по образцу дампа, числа выдуманы, конверсия согласована."""
    open_count = 100 + seed * 10
    cart_count = 10 + seed
    order_count = 1 + seed % 3
    buyout_count = order_count - 1
    return {
        "date": day,
        "openCount": open_count,
        "cartCount": cart_count,
        "orderCount": order_count,
        "orderSum": 1000 * order_count,
        "buyoutCount": buyout_count,
        "buyoutSum": 900 * buyout_count,
        "buyoutPercent": 50 + seed,
        "addToCartConversion": round(cart_count / open_count * 100),
        "cartToOrderConversion": round(order_count / cart_count * 100),
        "addToWishlistCount": 5 + seed,
    }


def make_payload(dates: list[str] | None = None) -> list[dict[str, Any]]:
    days = dates if dates is not None else period_dates()
    return [
        {
            "currency": "RUB",
            "product": {
                "nmId": nm_id,
                "title": f"Тестовый товар {nm_id}",
                "vendorCode": f"art-{nm_id}",
                "brandName": "ТестБренд",
                "subjectId": 1,
                "subjectName": "Товары",
            },
            "history": [day_row(day, index) for index, day in enumerate(days)],
        }
        for nm_id in NM_IDS_FIXTURE
    ]


@pytest.fixture
def payload() -> list[dict[str, Any]]:
    return make_payload()


@pytest.fixture
def rows(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return ff.normalize_rows(payload)


def test_funnel_period_excludes_today() -> None:
    today = msk_today()
    begin, end = ff.funnel_period()

    assert begin == today - timedelta(days=7)
    assert end == today - timedelta(days=1)
    assert end < today
    assert (end - begin).days == 6


def test_request_body_matches_explored_shape() -> None:
    begin, end = ff.funnel_period()

    body = ff.build_body(begin, end)

    assert set(body) == {"nmIds", "selectedPeriod", "aggregationLevel"}
    assert body["selectedPeriod"] == {"start": begin.isoformat(), "end": end.isoformat()}
    assert body["aggregationLevel"] == "day"


def test_normalization_makes_one_row_per_article_and_date(rows: list[dict[str, Any]]) -> None:
    assert len(rows) == 21
    assert len({(row["nm_id"], row["date"]) for row in rows}) == 21
    assert rows[0]["nm_id"] in NM_IDS_FIXTURE
    assert ff.csv_columns(rows)[:2] == ["nm_id", "date"]
    assert "openCount" in ff.csv_columns(rows)
    assert "buyoutSum" in ff.csv_columns(rows)


def test_shows_field_is_picked_by_arithmetic(rows: list[dict[str, Any]]) -> None:
    field, rationale = ff.detect_shows_field(rows)

    assert field == "openCount"
    assert "addToCartConversion" in rationale
    assert "buyoutPercent" not in rationale


def test_period_check_ok_on_full_seven_days(rows: list[dict[str, Any]]) -> None:
    begin, end = ff.funnel_period()

    check = ff.check_period(rows, begin, end, msk_today(), list(NM_IDS_FIXTURE))

    assert check["ok"] is True
    assert check["problems"] == []
    assert all(entry["unique_dates"] == 7 for entry in check["articles"].values())


def test_period_check_catches_six_days_and_foreign_date() -> None:
    begin, end = ff.funnel_period()
    short = period_dates()[:-1]
    stray = short + [(end + timedelta(days=30)).isoformat()]

    six_days = ff.check_period(ff.normalize_rows(make_payload(short)), begin, end, msk_today(), list(NM_IDS_FIXTURE))
    today_row = ff.normalize_rows(make_payload(period_dates() + [msk_today().isoformat()]))
    with_today = ff.check_period(today_row, begin, end, msk_today(), list(NM_IDS_FIXTURE))

    assert six_days["ok"] is False
    assert any("ожидалось 7" in problem for problem in six_days["problems"])
    assert any("вне периода" in problem for problem in
               ff.check_period(ff.normalize_rows(make_payload(stray)), begin, end, msk_today(), list(NM_IDS_FIXTURE))["problems"])
    assert any("сегодня" in problem for problem in with_today["problems"])


def test_period_check_reports_missing_article(rows: list[dict[str, Any]]) -> None:
    begin, end = ff.funnel_period()
    only_two = [row for row in rows if row["nm_id"] != NM_IDS_FIXTURE[-1]]

    check = ff.check_period(only_two, begin, end, msk_today(), list(NM_IDS_FIXTURE))

    assert check["missing_articles"] == [str(NM_IDS_FIXTURE[-1])]
    assert check["ok"] is False


def test_control_figures_on_hand_counted_numbers() -> None:
    manual = [
        {"nm_id": 1, "date": "2026-01-01", "openCount": 100, "orderCount": 5,
         "orderSum": 1000, "buyoutCount": 4, "buyoutSum": 800},
        {"nm_id": 1, "date": "2026-01-02", "openCount": 50, "orderCount": 5,
         "orderSum": 1000, "buyoutCount": 0, "buyoutSum": 0},
    ]

    figures = ff.control_figures(manual, "openCount")

    assert figures["shows"] == 150
    assert figures["orders_count"] == 10
    assert figures["orders_sum"] == 2000
    assert figures["buyout_count"] == 4
    assert figures["buyout_sum"] == 800
    assert figures["avg_check"] == 200.0
    assert figures["daily_cr"] == [5.0, 10.0]
    assert figures["cr_percent"]["avg"] == 7.5
    assert figures["cr_percent"]["min"] == 5.0
    assert figures["cr_percent"]["max"] == 10.0
    assert figures["cr_percent"]["days_with_zero_shows"] == 0


def test_zero_shows_and_zero_orders_do_not_divide_by_zero() -> None:
    rows = [
        {"nm_id": 1, "date": "2026-01-01", "openCount": 0, "orderCount": 0,
         "orderSum": 0, "buyoutCount": 0, "buyoutSum": 0},
        {"nm_id": 1, "date": "2026-01-02", "openCount": 200, "orderCount": 8,
         "orderSum": 1600, "buyoutCount": 6, "buyoutSum": 1200},
    ]

    figures = ff.control_figures(rows, "openCount")

    assert figures["avg_check"] == 200.0
    assert figures["daily_cr"] == [4.0]
    assert figures["cr_percent"]["days_with_zero_shows"] == 1
    assert figures["cr_percent"]["days_counted"] == 1

    empty_orders = ff.control_figures(
        [{"nm_id": 1, "date": "2026-01-01", "openCount": 50, "orderCount": 0,
          "orderSum": 0, "buyoutCount": 0, "buyoutSum": 0}],
        "openCount",
    )
    assert empty_orders["avg_check"] is None
    assert empty_orders["cr_percent"]["avg"] == 0.0


def test_cr_average_uses_unrounded_daily_ratios() -> None:
    """Среднее берётся по НЕ округлённым дневным CR.

    На этих числах среднее от округлённых значений дало бы 15.88, а честное
    среднее по дневным отношениям — 15.87. Разница и есть расхождение с
    формулой таблицы.
    """
    rows = [
        {"nm_id": 1, "date": "2026-01-01", "openCount": 6, "orderCount": 1,
         "orderSum": 100, "buyoutCount": 0, "buyoutSum": 0},
        {"nm_id": 1, "date": "2026-01-02", "openCount": 6, "orderCount": 1,
         "orderSum": 100, "buyoutCount": 0, "buyoutSum": 0},
        {"nm_id": 1, "date": "2026-01-03", "openCount": 7, "orderCount": 1,
         "orderSum": 100, "buyoutCount": 0, "buyoutSum": 0},
    ]

    figures = ff.control_figures(rows, "openCount")

    assert figures["daily_cr"] == [16.67, 16.67, 14.29]
    assert figures["cr_percent"]["avg"] == 15.87
    assert figures["cr_percent"]["min"] == 14.29
    assert figures["cr_percent"]["max"] == 16.67


def test_control_figures_on_full_fixture_matches_sums(rows: list[dict[str, Any]]) -> None:
    figures = ff.control_figures(rows, "openCount")

    assert figures["days"] == 7
    assert figures["shows"] == sum(row["openCount"] for row in rows)
    assert figures["orders_count"] == sum(row["orderCount"] for row in rows)
    assert figures["avg_check"] == round(
        figures["orders_sum"] / figures["orders_count"], 2
    )
    assert len(figures["daily_cr"]) == 21


def test_period_check_catches_duplicate_day_row(rows: list[dict[str, Any]]) -> None:
    """Дубль дневной строки не спрячется за правильным числом уникальных дат."""
    begin, end = ff.funnel_period()
    duplicated = rows + [dict(rows[0])]

    check = ff.check_period(duplicated, begin, end, msk_today(), list(NM_IDS_FIXTURE))

    assert check["ok"] is False
    assert any("дубликаты по дням" in problem for problem in check["problems"])
    key = str(rows[0]["nm_id"])
    assert check["articles"][key]["rows"] == 8
    assert check["articles"][key]["unique_dates"] == 7
    assert check["articles"][key]["duplicate_dates"] == [rows[0]["date"]]


def test_period_check_reports_row_count_on_clean_data(rows: list[dict[str, Any]]) -> None:
    begin, end = ff.funnel_period()

    check = ff.check_period(rows, begin, end, msk_today(), list(NM_IDS_FIXTURE))

    assert all(entry["rows"] == entry["unique_dates"] == 7 for entry in check["articles"].values())
