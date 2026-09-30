"""Unit-тесты сборки выгрузки в Google Таблицу. Сети нет — только чистые функции."""

from typing import Any

import upload_to_sheets as up


def metrics(shows: int, orders_count: int, orders_sum: int, buyout_count: int,
            buyout_sum: int, avg_check: float, avg: float, low: float, high: float) -> dict[str, Any]:
    return {
        "days": 7,
        "shows_field": "openCount",
        "shows": shows,
        "orders_count": orders_count,
        "orders_sum": orders_sum,
        "buyout_count": buyout_count,
        "buyout_sum": buyout_sum,
        "avg_check": avg_check,
        "cr_percent": {
            "avg": avg,
            "min": low,
            "max": high,
            "days_counted": 7,
            "days_with_zero_shows": 0,
        },
        "daily_cr": [avg] * 7,
    }


def make_stats() -> dict[str, Any]:
    """Два артикула: у первого выкупы больше, поэтому он идёт первой строкой."""
    stats = {
        "period_check": {
            "period": {"begin": "2026-09-23", "end": "2026-09-29", "days": 7},
            "ok": True,
        },
        "articles": {
            "111": metrics(1000, 40, 40000, 30, 30000, 1000.0, 4.0, 2.0, 6.0),
            "222": metrics(500, 10, 10000, 20, 20000, 1000.0, 2.0, 1.0, 3.0),
        },
        "grand_total": metrics(1500, 50, 50000, 50, 50000, 1000.0, 3.0, 1.0, 6.0),
    }
    # title есть у артикулов в funnel_stats.json и отсутствует у grand_total
    stats["articles"]["111"]["title"] = "Шуруповёрт аккумуляторный 21В"
    stats["articles"]["222"]["title"] = "Коврик для мыши игровой"
    return stats


def funnel_row(nm_id: int, day: str, shows: int, orders: int = 1, buyout: int = 0) -> dict[str, Any]:
    return {
        "nm_id": nm_id,
        "date": day,
        "openCount": shows,
        "cartCount": 10,
        "orderCount": orders,
        "orderSum": 1000,
        "buyoutCount": buyout,
        "buyoutSum": 900 if buyout else 0,
        "buyoutPercent": 90 if buyout else 0,
        "addToCartConversion": 10,
        "cartToOrderConversion": 10,
        "addToWishlistCount": 5,
    }


def summary_row(key: Any, entry: dict[str, Any]) -> list[Any]:
    """Строка так, как её отдаёт таблица в UNFORMATTED: CR лежит долей."""
    cr = entry["cr_percent"]
    return [
        key, "Товар", entry["shows"], entry["orders_count"], entry["orders_sum"],
        entry["buyout_count"], entry["buyout_sum"], entry["avg_check"],
        cr["avg"] / 100, cr["min"] / 100, cr["max"] / 100, "Диаграмма",
    ]


def summary_body(stats: dict[str, Any], order: list[int]) -> list[list[Any]]:
    """Заголовок + строки артикулов + ИТОГО из тех же цифр, что и в stats."""
    return (
        [["Артикул", "Товар", *up.SUMMARY_HEADER[2:]]]
        + [summary_row(nm_id, stats["articles"][str(nm_id)]) for nm_id in order]
        + [summary_row(up.TOTAL_LABEL, stats["grand_total"])]
    )


def formula_body(body: list[list[Any]]) -> list[list[Any]]:
    """То же тело, но в C..L лежат формулы, как их читает FORMULA."""
    rows = [list(body[0])]
    for line, row in enumerate(body[1:], start=2):
        rows.append([row[0], row[1], *up.summary_formulas(line)])
    return rows


def test_article_order_is_buyout_sum_descending() -> None:
    assert up.article_order(make_stats()) == [111, 222]


def test_raw_block_sorts_article_then_date() -> None:
    rows = [
        funnel_row(222, "2026-09-24", 50),
        funnel_row(111, "2026-09-25", 100),
        funnel_row(111, "2026-09-23", 200),
        funnel_row(222, "2026-09-23", 60),
    ]

    block = up.build_raw_block(rows, [111, 222])

    assert block[0] == up.RAW_HEADER
    assert [row[0] for row in block[1:]] == [111, 111, 222, 222]
    assert [row[1] for row in block[1:]] == ["2026-09-23", "2026-09-25", "2026-09-23", "2026-09-24"]
    assert all(isinstance(row[2], int) for row in block[1:])


def test_raw_block_has_21_data_rows_with_rowwise_cr_formula() -> None:
    order = [300, 200, 100]
    rows = [funnel_row(nm, f"2026-09-{23 + day}", 10) for nm in order for day in range(7)]

    block = up.build_raw_block(rows, order)

    assert len(block) == 22
    assert block[1][12] == '=IF(C2=0,"",E2/C2)'
    assert block[-1][12] == '=IF(C22=0,"",E22/C22)'


def test_summary_formulas_reference_raw_sheet() -> None:
    formulas = up.summary_formulas(2)

    assert formulas[0] == "=SUMIFS('Сырые данные'!C:C,'Сырые данные'!A:A,A2)"
    assert formulas[1] == "=SUMIFS('Сырые данные'!E:E,'Сырые данные'!A:A,A2)"
    assert formulas[2] == "=SUMIFS('Сырые данные'!F:F,'Сырые данные'!A:A,A2)"
    assert formulas[3] == "=SUMIFS('Сырые данные'!G:G,'Сырые данные'!A:A,A2)"
    assert formulas[4] == "=SUMIFS('Сырые данные'!H:H,'Сырые данные'!A:A,A2)"
    assert formulas[5] == '=IF(D2=0,"—",E2/D2)'
    assert formulas[6] == '=IFERROR(AVERAGEIFS(\'Сырые данные\'!M:M,\'Сырые данные\'!A:A,A2),"—")'
    assert formulas[7] == '=IFERROR(MINIFS(\'Сырые данные\'!M:M,\'Сырые данные\'!A:A,A2),"—")'
    assert formulas[8] == '=IFERROR(MAXIFS(\'Сырые данные\'!M:M,\'Сырые данные\'!A:A,A2),"—")'
    assert formulas[9] == "=SPARKLINE(FILTER('Сырые данные'!H:H,'Сырые данные'!A:A=A2))"


def test_total_row_formulas_sum_and_pool_daily_cr() -> None:
    formulas = up.total_formulas(2, 3, 22)

    assert formulas[:5] == ["=SUM(C2:C3)", "=SUM(D2:D3)", "=SUM(E2:E3)",
                            "=SUM(F2:F3)", "=SUM(G2:G3)"]
    assert formulas[5] == '=IF(D4=0,"—",E4/D4)'
    assert formulas[6] == '=IFERROR(AVERAGE(\'Сырые данные\'!M2:M22),"—")'
    assert formulas[7] == "=MIN('Сырые данные'!M2:M22)"
    assert formulas[8] == "=MAX('Сырые данные'!M2:M22)"
    assert formulas[9] == ""


def test_summary_block_lays_out_articles_then_total() -> None:
    stats = make_stats()

    block = up.build_summary_block([111, 222], {111: "Товар первый", 222: "Товар второй"}, 21)

    assert block[0] == up.SUMMARY_HEADER
    assert [row[0] for row in block[1:]] == [111, 222, up.TOTAL_LABEL]
    assert block[1][1] == "Товар первый"
    assert block[1][2].startswith("=SUMIFS(")
    assert block[3][1] == ""
    assert all(len(row) == len(up.SUMMARY_HEADER) for row in block)


def test_verify_summary_accepts_consistent_table() -> None:
    stats = make_stats()
    order = up.article_order(stats)
    body = summary_body(stats, order)

    report = up.verify_summary(body, formula_body(body), stats, order)

    assert report["ok"] is True, report["problems"]
    assert len(report["checks"]) == 27
    # строка ИТОГО в проверку порядка не входит: она заведомо больше всех
    assert report["row_order"]["buyout_sequence"] == [30000.0, 20000.0]


def test_verify_summary_catches_wrong_number() -> None:
    stats = make_stats()
    order = up.article_order(stats)
    body = summary_body(stats, order)
    body[1][2] = 999

    report = up.verify_summary(body, formula_body(body), stats, order)

    assert report["ok"] is False
    assert any("показы" in problem for problem in report["problems"])


def test_verify_summary_catches_cr_scale_error() -> None:
    """CR в таблице — доля; если положить проценты, сверка обязана это заметить."""
    stats = make_stats()
    order = up.article_order(stats)
    body = summary_body(stats, order)
    body[1][8] = 4.0

    report = up.verify_summary(body, formula_body(body), stats, order)

    assert report["ok"] is False
    assert any("CR среднее" in problem for problem in report["problems"])


def test_verify_summary_catches_non_formula_cell() -> None:
    stats = make_stats()
    order = up.article_order(stats)
    body = summary_body(stats, order)
    formulas = formula_body(body)
    formulas[1][2] = 1000

    report = up.verify_summary(body, formulas, stats, order)

    assert report["ok"] is False
    assert any("не является формулой" in problem for problem in report["problems"])


def test_verify_summary_catches_broken_order() -> None:
    stats = make_stats()
    order = [222, 111]
    body = summary_body(stats, [111, 222])
    body = [body[0], body[2], body[1], body[3]]

    report = up.verify_summary(body, formula_body(body), stats, order)

    assert report["ok"] is False
    assert report["row_order"]["buyout_sum_descending"] is False


def test_verify_summary_flags_non_numeric_cell() -> None:
    stats = make_stats()
    order = up.article_order(stats)
    body = summary_body(stats, order)
    body[1][7] = "#DIV/0!"

    report = up.verify_summary(body, formula_body(body), stats, order)

    assert report["ok"] is False
    assert any("средний чек" in problem for problem in report["problems"])


def test_buyout_percent_semantics_reports_no_simple_formula() -> None:
    rows = [
        # 36/40 = 90% по штукам, но 500/1000 = 50% по суммам — совпадение только по штукам
        {"orderCount": 40, "buyoutCount": 36, "orderSum": 1000, "buyoutSum": 500, "buyoutPercent": 90},
        # 5/10 = 50% и по штукам, и по суммам, а в API 90 — не совпадает ни с чем
        {"orderCount": 10, "buyoutCount": 5, "orderSum": 100, "buyoutSum": 50, "buyoutPercent": 90},
    ]

    result = up.buyout_percent_semantics(rows)

    assert result["checked"] == 2
    assert result["matched_count_only"] == 1
    assert result["matched_sum_only"] == 0
    assert result["matched_neither"] == 1
    assert "не воспроизводится" in result["conclusion"]
    # вывод ограничен этим срезом: версия о когортах подана как гипотеза, а не факт
    assert "документального подтверждения нет" in result["conclusion"]
    assert "из 2 записей" in result["conclusion"]
    assert "справочная" in result["conclusion"]


def test_buyout_percent_skips_zero_denominators() -> None:
    result = up.buyout_percent_semantics(
        [{"orderCount": 0, "buyoutCount": 0, "orderSum": 0, "buyoutSum": 0, "buyoutPercent": 0}]
    )

    assert result["checked"] == 0
    assert result["skipped_zero_denominator"] == 1


def test_methodology_text_carries_definitions_and_caveats() -> None:
    stats = make_stats()
    order = up.article_order(stats)
    rows = [funnel_row(111, "2026-09-23", 100, orders=4, buyout=2)]
    text = "\n".join(
        up.methodology_lines(up.build_period_meta(stats), stats, order, rows,
                            up.buyout_percent_semantics(rows))
    )

    assert "[2026-09-23; 2026-09-29]" in text
    assert "сегодняшний день исключён" in text
    assert "Показы = openCount" in text
    assert "CR день = Заказы, шт / Показы" in text
    assert "Средний чек = Сумма заказов, ₽ / Заказы, шт" in text
    assert "без промежуточного округления" in text
    assert "среднее арифметическое дневных CR" in text
    assert "buyoutPercent" in text
    assert "по сумме выкупов ₽ по убыванию" in text
    assert "sales-funnel/products/history" in text


def test_sorting_note_explains_inversion_against_order_count() -> None:
    """Нижняя строка с большим числом заказов не должна выглядеть ошибкой выгрузки."""
    stats = make_stats()
    stats["articles"]["222"]["orders_count"] = 90
    text = "\n".join(up.sorting_note(stats, up.article_order(stats)))

    assert "по сумме выкупов ₽ по убыванию, а не по числу заказов" in text
    assert "заказов у него 40 против 90" in text
    assert "30 000 ₽ против 20 000 ₽" in text
    assert "Шуруповёрт аккумуляторный 21В" in text
    assert "Коврик для мыши игровой" in text


def test_sorting_note_without_inversion_keeps_sorting_rule() -> None:
    stats = make_stats()
    text = "\n".join(up.sorting_note(stats, up.article_order(stats)))

    assert "по сумме выкупов ₽ по убыванию" in text
    assert "не пересортировывают строки" in text


def test_formula_separator_follows_sheet_locale() -> None:
    """Локаль ru_RU требует «;»: с запятой "=SUM(1,2)" означает число 1.2."""
    assert up.formula_separator("ru_RU") == ";"
    assert up.formula_separator("uk") == ";"
    assert up.formula_separator("de_DE") == ";"
    assert up.formula_separator("en_US") == ","
    assert up.formula_separator(None) == ","


def test_formulas_can_be_built_with_semicolon_separator() -> None:
    assert up.summary_formulas(2, ";")[0] == "=SUMIFS('Сырые данные'!C:C;'Сырые данные'!A:A;A2)"
    assert up.summary_formulas(2, ";")[5] == '=IF(D2=0;"—";E2/D2)'
    assert up.summary_formulas(2, ";")[6] == '=IFERROR(AVERAGEIFS(\'Сырые данные\'!M:M;\'Сырые данные\'!A:A;A2);"—")'
    assert up.total_formulas(2, 3, 22, ";")[6] == '=IFERROR(AVERAGE(\'Сырые данные\'!M2:M22);"—")'

    row = funnel_row(111, "2026-09-23", 100)
    assert up.raw_row_values(row, 2, ";")[12] == '=IF(C2=0;"";E2/C2)'
    assert up.raw_row_values(row, 2)[12] == '=IF(C2=0,"",E2/C2)'


def test_column_letter_and_flatten_expected() -> None:
    assert up.column_letter(1) == "A"
    assert up.column_letter(13) == "M"

    flat = up.flatten_expected(make_stats()["articles"]["111"])

    assert flat["cr_avg"] == 4.0 and flat["cr_min"] == 2.0 and flat["cr_max"] == 6.0
    assert "cr_percent" not in flat
