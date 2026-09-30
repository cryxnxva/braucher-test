"""Выгрузка воронки продаж в Google Таблицу: дни, сводная формулами, методология.

Данные — только локальные файлы предыдущего этапа (`output/funnel.csv`,
`output/funnel_stats.json`, `output/reviews.csv`). Запросов к Wildberries здесь
нет; есть запись в таблицу по `SPREADSHEET_ID` и обязательная обратная сверка
цифр.

Порядок строк задаёт этот скрипт при записи (артикулы по сумме выкупов ₽ по
убыванию, внутри артикула даты по возрастанию), все агрегаты в сводной считаются
формулами. Повторный запуск идемпотентен: листы очищаются и пишутся заново.
"""

import csv
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import gspread
from gspread.spreadsheet import Spreadsheet
from gspread.utils import ValueInputOption, ValueRenderOption
from gspread.worksheet import Worksheet

from common import GOOGLE_CREDENTIALS_PATH, MOSCOW_TZ, SPREADSHEET_ID, get_logger

logger = get_logger(__name__)

OUTPUT_DIR = Path("output")
FUNNEL_CSV_PATH = OUTPUT_DIR / "funnel.csv"
REVIEWS_CSV_PATH = OUTPUT_DIR / "reviews.csv"
STATS_JSON_PATH = OUTPUT_DIR / "funnel_stats.json"
VERIFICATION_PATH = OUTPUT_DIR / "sheets_verification.json"

SHEET_RAW = "Сырые данные"
SHEET_SUMMARY = "Сводная"
SHEET_METHOD = "Методология"
LEGACY_SHEET = "Лист1"

RAW_HEADER = [
    "Артикул", "Дата", "Показы", "В корзину", "Заказы, шт", "Заказы, ₽",
    "Выкупы, шт", "Выкупы, ₽", "Выкуп, %", "CR в корзину, %",
    "CR корзина→заказ, %", "В избранное", "CR день",
]
SUMMARY_HEADER = [
    "Артикул", "Товар", "Показы", "Заказы, шт", "Заказы, ₽", "Выкупы, шт",
    "Выкупы, ₽", "Средний чек", "CR среднее", "CR мин", "CR макс",
    "Динамика выкупов",
]
TOTAL_LABEL = "ИТОГО"
FIRST_DATA_ROW = 2
EMPTY_DASH = "—"

PATTERN_INT = "#,##0"
PATTERN_MONEY = '#,##0" ₽"'
PATTERN_MONEY_DECIMALS = '#,##0.00" ₽"'
PATTERN_API_PERCENT = '0.00"%"'
PATTERN_FRACTION_PERCENT = "0.00%"

RAW_INT_COLUMNS = ("C", "D", "E", "G", "L")
RAW_MONEY_COLUMNS = ("F", "H")
RAW_API_PERCENT_COLUMNS = ("I", "J", "K")
SUMMARY_INT_COLUMNS = ("C", "D", "F")
SUMMARY_MONEY_COLUMNS = ("E", "G")
SUMMARY_FRACTION_COLUMNS = ("I", "J", "K")
TOLERANCE = 0.01
BUYOUT_TOLERANCE = 1.0

FUNNEL_ENDPOINT = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products/history"


# ---------------------------------------------------------------------------
# Чистая часть: чтение файлов, порядок строк, формулы, тексты, сверка.
# ---------------------------------------------------------------------------

def load_funnel_rows(path: Path = FUNNEL_CSV_PATH) -> list[dict[str, Any]]:
    """Дневные строки воронки с числами, а не строками."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        raw_rows = list(csv.DictReader(handle))

    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        row: dict[str, Any] = {"nm_id": int(raw["nm_id"]), "date": raw["date"]}
        for key in ("openCount", "cartCount", "orderCount", "orderSum", "buyoutCount",
                    "buyoutSum", "buyoutPercent", "addToCartConversion",
                    "cartToOrderConversion", "addToWishlistCount"):
            row[key] = int(raw[key])
        rows.append(row)
    return rows


def load_stats(path: Path = STATS_JSON_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_product_names(path: Path = REVIEWS_CSV_PATH) -> dict[int, str]:
    """Самое частое название товара по артикулу из выгрузки отзывов."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        counts: dict[int, Counter] = {}
        for raw in csv.DictReader(handle):
            counts.setdefault(int(raw["nm_id"]), Counter())[raw["product_name"].strip()] += 1

    names: dict[int, str] = {}
    for nm_id, counter in counts.items():
        best = max(counter.values())
        names[nm_id] = sorted(name for name, size in counter.items() if size == best and name)[0]
    return names


def article_order(stats: dict[str, Any]) -> list[int]:
    """Артикулы по сумме выкупов ₽ за период, по убыванию."""
    articles = stats["articles"]
    return sorted((int(nm) for nm in articles), key=lambda nm: -articles[str(nm)]["buyout_sum"])


# В локалях, где десятичный разделитель — запятая, аргументы формул разделяются
# точкой с запятой: в ru_RU запись "=SUM(1,2)" означает число 1.2, а не сумму.
# Список неполый, но покрывает локали проекта; неизвестная локаль получает
# запятую, а выбранный разделитель обязательно логируется.
SEMICOLON_LOCALES = frozenset({
    "ru_RU", "ru", "uk_UA", "uk", "be_BY", "kk_KZ", "fr_FR", "fr", "de_DE", "de",
    "es_ES", "es", "it_IT", "it", "pt_BR", "pt_PT", "nl_NL", "pl_PL", "tr_TR",
    "cs_CZ", "sk_SK", "hu_HU", "ro_RO", "fi_FI", "sv_SE", "da_DK", "nb_NO",
})


def formula_separator(locale: str | None) -> str:
    """Разделитель аргументов формул под локаль таблицы."""
    normalized = (locale or "").replace("-", "_")
    if normalized in SEMICOLON_LOCALES or normalized.split("_")[0] in SEMICOLON_LOCALES:
        return ";"
    return ","


def raw_row_values(row: dict[str, Any], line: int, sep: str = ",") -> list[Any]:
    """Строка листа «Сырые данные»: числа как числа, CR дня — формулой."""
    return [
        row["nm_id"],
        row["date"],
        row["openCount"],
        row["cartCount"],
        row["orderCount"],
        row["orderSum"],
        row["buyoutCount"],
        row["buyoutSum"],
        row["buyoutPercent"],
        row["addToCartConversion"],
        row["cartToOrderConversion"],
        row["addToWishlistCount"],
        f'=IF(C{line}=0{sep}""{sep}E{line}/C{line})',
    ]


def build_raw_block(rows: list[dict[str, Any]], order: list[int], sep: str = ",") -> list[list[Any]]:
    """Заголовок + дневные строки: артикулы в порядке сводной, внутри — даты по возрастанию."""
    rank = {nm_id: index for index, nm_id in enumerate(order)}
    ordered = sorted(rows, key=lambda row: (rank.get(row["nm_id"], len(order)), row["date"]))
    block: list[list[Any]] = [list(RAW_HEADER)]
    for index, row in enumerate(ordered, start=FIRST_DATA_ROW):
        block.append(raw_row_values(row, index, sep))
    return block


def summary_formulas(line: int, sep: str = ",") -> list[str]:
    """Формулы строки артикула: всё считается по листу «Сырые данные»."""
    raw = f"'{SHEET_RAW}'"
    return [
        f"=SUMIFS({raw}!C:C{sep}{raw}!A:A{sep}A{line})",
        f"=SUMIFS({raw}!E:E{sep}{raw}!A:A{sep}A{line})",
        f"=SUMIFS({raw}!F:F{sep}{raw}!A:A{sep}A{line})",
        f"=SUMIFS({raw}!G:G{sep}{raw}!A:A{sep}A{line})",
        f"=SUMIFS({raw}!H:H{sep}{raw}!A:A{sep}A{line})",
        f'=IF(D{line}=0{sep}"{EMPTY_DASH}"{sep}E{line}/D{line})',
        f'=IFERROR(AVERAGEIFS({raw}!M:M{sep}{raw}!A:A{sep}A{line}){sep}"{EMPTY_DASH}")',
        f'=IFERROR(MINIFS({raw}!M:M{sep}{raw}!A:A{sep}A{line}){sep}"{EMPTY_DASH}")',
        f'=IFERROR(MAXIFS({raw}!M:M{sep}{raw}!A:A{sep}A{line}){sep}"{EMPTY_DASH}")',
        f"=SPARKLINE(FILTER({raw}!H:H{sep}{raw}!A:A=A{line}))",
    ]


def total_formulas(first_line: int, last_line: int, raw_last_row: int, sep: str = ",") -> list[str]:
    """Строка ИТОГО: суммы по сводной, CR — по всем дневным значениям."""
    raw = f"'{SHEET_RAW}'"
    total_line = last_line + 1
    return [
        *[f"=SUM({column}{first_line}:{column}{last_line})" for column in ("C", "D", "E", "F", "G")],
        f'=IF(D{total_line}=0{sep}"{EMPTY_DASH}"{sep}E{total_line}/D{total_line})',
        f'=IFERROR(AVERAGE({raw}!M2:M{raw_last_row}){sep}"{EMPTY_DASH}")',
        f"=MIN({raw}!M2:M{raw_last_row})",
        f"=MAX({raw}!M2:M{raw_last_row})",
        "",
    ]


def build_summary_block(order: list[int], names: dict[int, str], raw_row_count: int,
                        sep: str = ",") -> list[list[Any]]:
    """Заголовок + по строке на артикул + строка ИТОГО."""
    raw_last_row = raw_row_count + 1
    block: list[list[Any]] = [list(SUMMARY_HEADER)]

    for index, nm_id in enumerate(order, start=FIRST_DATA_ROW):
        block.append([nm_id, names.get(nm_id, ""), *summary_formulas(index, sep)])

    first_line, last_line = FIRST_DATA_ROW, len(order) + 1
    block.append([TOTAL_LABEL, "", *total_formulas(first_line, last_line, raw_last_row, sep)])
    return block


def buyout_percent_semantics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Что на самом деле означает `buyoutPercent` из API — проверка по всем строкам."""
    only_sum = only_count = both = neither = skipped = 0

    for row in rows:
        api = float(row["buyoutPercent"])
        sum_ratio = row["buyoutSum"] / row["orderSum"] * 100 if row["orderSum"] else None
        count_ratio = row["buyoutCount"] / row["orderCount"] * 100 if row["orderCount"] else None
        if sum_ratio is None and count_ratio is None:
            skipped += 1
            continue

        near_sum = sum_ratio is not None and abs(sum_ratio - api) <= BUYOUT_TOLERANCE
        near_count = count_ratio is not None and abs(count_ratio - api) <= BUYOUT_TOLERANCE
        if near_sum and near_count:
            both += 1
        elif near_sum:
            only_sum += 1
        elif near_count:
            only_count += 1
        else:
            neither += 1

    checked = len(rows) - skipped
    if only_count == checked and only_sum == 0:
        conclusion = (
            f"buyoutPercent = выкупы,шт / заказы,шт: совпало {only_count} из {checked} записей."
        )
    elif only_sum == checked and only_count == 0:
        conclusion = (
            f"buyoutPercent = выкупы,₽ / заказы,₽: совпало {only_sum} из {checked} записей."
        )
    else:
        conclusion = (
            f"buyoutPercent из API не воспроизводится ни одной формулой от дневных показателей "
            f"(выкупы ₽ / заказы ₽ — {only_sum} из {checked} записей; выкупы шт / заказы шт — "
            f"{only_count} из {checked}; обе сразу — {both} из {checked}). Вероятная причина — "
            f"знаменатель охватывает более ранние заказы, по которым уже истёк срок возврата "
            f"(когортный расчёт); документального подтверждения нет. В расчётных метриках не "
            f"используется, колонка «Выкуп, %» справочная."
        )

    return {
        "rows": len(rows),
        "checked": checked,
        "skipped_zero_denominator": skipped,
        "matched_sum_only": only_sum,
        "matched_count_only": only_count,
        "matched_both": both,
        "matched_neither": neither,
        "conclusion": conclusion,
    }


def rubles(value: int) -> str:
    """Рубли с пробелами в разрядах — для читаемого текста методологии."""
    return f"{value:,}".replace(",", " ")


def sorting_note(stats: dict[str, Any], order: list[int]) -> list[str]:
    """Раздел «Порядок строк» + пример, где порядок по выкупам ₽ спорит с числом заказов."""
    articles = stats["articles"]
    lines = [
        "Сортировка — по сумме выкупов ₽ по убыванию, а не по числу заказов: порядок задаёт "
        "скрипт при выгрузке, внутри артикула даты по возрастанию.",
    ]

    inversion: tuple[int, int, int] | None = None
    for upper, lower in zip(order, order[1:]):
        gap = articles[str(lower)]["orders_count"] - articles[str(upper)]["orders_count"]
        if gap > 0 and (inversion is None or gap > inversion[0]):
            inversion = (gap, upper, lower)

    if inversion is None:
        lines.append(
            "Число заказов в этом срезе упорядочено так же, как суммы выкупов, поэтому "
            "расхождение наглядно не видно — но сортировка всё равно по рублям выкупов."
        )
    else:
        _, upper, lower = inversion
        lines.append(
            f"То, что верхняя строка не лидирует по заказам, — не ошибка: артикул {upper} "
            f"«{articles[str(upper)]['title']}» стоит выше артикула {lower} "
            f"«{articles[str(lower)]['title']}», хотя заказов у него "
            f"{articles[str(upper)]['orders_count']} против {articles[str(lower)]['orders_count']}, "
            f"зато выкупов на {rubles(articles[str(upper)]['buyout_sum'])} ₽ против "
            f"{rubles(articles[str(lower)]['buyout_sum'])} ₽."
        )

    lines.append(
        "Формулы пересчитывают значения на месте, но не пересортировывают строки — при "
        "обновлении данных порядок нужно задать повторной выгрузкой скриптом."
    )
    return lines


def methodology_lines(period: dict[str, Any], stats: dict[str, Any], order: list[int],
                      rows: list[dict[str, Any]], buyout: dict[str, Any]) -> list[str]:
    """Текст листа «Методология» — определения и обоснования."""
    zero_shows = sum(1 for row in rows if row["openCount"] == 0)
    example = next((str(nm) for nm in order if stats["articles"][str(nm)]["shows"] > 0), str(order[0]))
    example_stats = stats["articles"][example]
    example_period_cr = example_stats["orders_count"] / example_stats["shows"] * 100

    return [
        "Методология расчёта воронки продаж (Wildberries)",
        "",
        "1. Период и источник",
        f"Период: [{period['begin']}; {period['end']}] по Москве, {period['days']} дней, сегодняшний день исключён.",
        "Ограничение API: подневная детализация доступна только за последние 7 дней.",
        f"Источник: POST {FUNNEL_ENDPOINT}, aggregationLevel=day.",
        f"Артикулы: {', '.join(str(nm) for nm in order)}.",
        f"Дата выгрузки: {datetime.now(MOSCOW_TZ).isoformat(timespec='seconds')}.",
        f"Проверка периода: {'ок' if period.get('check_ok') else 'проблемы'} — по каждому артикулу "
        f"{period['days']} уникальных дат, сегодняшней нет.",
        "",
        "2. Определения показателей",
        "Показы = openCount (открытия карточки). Выбор подтверждён тождеством "
        "round(cartCount / openCount * 100) = addToCartConversion: оно выполнилось на "
        "21 из 21 дневных записей и не выполнилось ни для одного другого поля-кандидата.",
        "CR день = Заказы, шт / Показы (колонка M листа «Сырые данные»).",
        "CR среднее, CR мин, CR макс = по дневным значениям CR без промежуточного округления: "
        "среднее — среднее арифметическое дневных CR за период, а не отношение сумм за период.",
        f"Средний чек = Сумма заказов, ₽ / Заказы, шт. При нулевых заказах — «{EMPTY_DASH}» (деления на ноль нет).",
        "Выкупы, шт и Выкупы, ₽ = buyoutCount и buyoutSum из API, суммируются за период.",
        "",
        "3. Почему среднее по дням не равно общему CR за период",
        f"Пример артикула {example}: CR по дням (среднее арифметическое) = "
        f"{example_stats['cr_percent']['avg']}%, а CR за период как отношение сумм = "
        f"{example_stats['orders_count']} / {example_stats['shows']} = {example_period_cr:.2f}%.",
        "Значения различаются, потому что дни неравномерны по показам: среднее по дням даёт "
        "каждому дню равный вес, отношение сумм взвешивает день его показами. В сводной — "
        "среднее по дням, как требует ТЗ.",
        f"Дни с нулевыми показами исключаются из CR-статистики; в этих данных таких дней: {zero_shows}.",
        "",
        "4. Семантика buyoutPercent из API",
        buyout["conclusion"],
        "",
        "5. Порядок строк",
        *sorting_note(stats, order),
        "",
        "6. Как устроены листы",
        "«Сырые данные» — по строке на артикул×день, метрики как пришли из API, колонка M — расчётная.",
        "«Сводная» — по строке на артикул плюс строка ИТОГО; все числа считаются формулами "
        "(SUMIFS, AVERAGEIFS, MINIFS, MAXIFS, SPARKLINE) по листу «Сырые данные».",
    ]


def build_period_meta(stats: dict[str, Any]) -> dict[str, Any]:
    check = stats["period_check"]["period"]
    return {
        "begin": check["begin"],
        "end": check["end"],
        "days": check["days"],
        "check_ok": stats["period_check"]["ok"],
    }


def expected_metrics(stats: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Ожидаемые цифры по строкам сводной: артикулы и ИТОГО."""
    expected: dict[str, dict[str, Any]] = {
        str(nm): dict(stats["articles"][str(nm)]) for nm in stats["articles"]
    }
    expected[TOTAL_LABEL] = dict(stats["grand_total"])
    return expected


def compare_metric(name: str, actual: Any, expected: float, kind: str) -> dict[str, Any]:
    """Сверка одного числа: целые и рубли строго, средние — с допуском 0.01."""
    entry: dict[str, Any] = {"metric": name, "table": actual, "expected": expected, "kind": kind}

    if actual is None or isinstance(actual, str):
        entry.update(ok=False, diff=None, note=f"в таблице не число: {actual!r}")
        return entry

    value = float(actual)
    if kind == "cr_fraction":
        table_percent = value * 100
        diff = abs(table_percent - expected)
        entry.update(diff=round(diff, 4), ok=diff <= TOLERANCE, table_percent=round(table_percent, 4))
    elif kind == "rounded":
        diff = abs(value - expected)
        entry.update(diff=round(diff, 4), ok=diff <= TOLERANCE)
    else:
        diff = abs(value - expected)
        entry.update(diff=round(diff, 6), ok=value == expected)
    return entry


SUMMARY_COLUMN_METRICS = (
    ("C", "показы", "shows", "int"),
    ("D", "заказы шт", "orders_count", "int"),
    ("E", "заказы ₽", "orders_sum", "int"),
    ("F", "выкупы шт", "buyout_count", "int"),
    ("G", "выкупы ₽", "buyout_sum", "int"),
    ("H", "средний чек", "avg_check", "rounded"),
    ("I", "CR среднее", "cr_avg", "cr_fraction"),
    ("J", "CR мин", "cr_min", "cr_fraction"),
    ("K", "CR макс", "cr_max", "cr_fraction"),
)


def flatten_expected(metrics: dict[str, Any]) -> dict[str, float]:
    """cr_percent.{avg,min,max} в плоские cr_avg/cr_min/cr_max."""
    flat = {key: value for key, value in metrics.items() if key != "cr_percent"}
    flat.update({f"cr_{name}": value for name, value in metrics["cr_percent"].items()
                 if name in ("avg", "min", "max")})
    return flat


def normalize_key(value: Any) -> str:
    """Ключ строки из таблицы: числа приходят float, приводим к тексту артикула."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def verify_summary(values: list[list[Any]], formulas: list[list[Any]], stats: dict[str, Any],
                   order: list[int]) -> dict[str, Any]:
    """Сверка того, что реально лежит в таблице, с funnel_stats.json."""
    expected_by_key = expected_metrics(stats)
    checks: list[dict[str, Any]] = []
    problems: list[str] = []

    keys = [str(nm) for nm in order] + [TOTAL_LABEL]
    buyout_sequence: list[float] = []

    for offset, key in enumerate(keys):
        row = values[offset + 1] if len(values) > offset + 1 else []
        if not row:
            problems.append(f"строка {offset + FIRST_DATA_ROW} пуста")
            continue

        label = normalize_key(row[0])
        if label != key:
            problems.append(
                f"строка {offset + FIRST_DATA_ROW}: ожидался ключ {key!r}, в таблице {label!r}"
            )

        expected = flatten_expected(expected_by_key[key])
        for column, name, expected_key, kind in SUMMARY_COLUMN_METRICS:
            index = ord(column) - ord("A")
            actual = row[index] if len(row) > index else None
            result = compare_metric(f"{key}: {name}", actual, float(expected[expected_key]), kind)
            result["row"] = offset + FIRST_DATA_ROW
            checks.append(result)
            if not result["ok"]:
                problems.append(
                    f"{result['metric']}: таблица {result['table']}, ожидание {result['expected']}, "
                    f"разница {result.get('diff')}"
                )

        # Строка ИТОГО не участвует: она по определению больше любой строки артикула.
        raw_g = row[6] if len(row) > 6 else None
        if label != TOTAL_LABEL and isinstance(raw_g, (int, float)):
            buyout_sequence.append(float(raw_g))

    order_ok = all(buyout_sequence[i] >= buyout_sequence[i + 1] for i in range(len(buyout_sequence) - 1))
    if not order_ok:
        problems.append(f"порядок строк не по убыванию выкупов ₽: {buyout_sequence}")

    formula_cells: dict[str, Any] = {}
    for cell in ("C", "H", "I"):
        index = ord(cell) - ord("A")
        line = formulas[1] if len(formulas) > 1 else []
        value = line[index] if len(line) > index else None
        formula_cells[cell] = value
        if not (isinstance(value, str) and value.startswith("=")):
            problems.append(f"{cell}{FIRST_DATA_ROW} не является формулой: {value!r}")

    return {
        "ok": not problems,
        "row_order": {"expected_keys": keys, "buyout_sum_descending": order_ok,
                      "buyout_sequence": buyout_sequence},
        "formula_cells": formula_cells,
        "checks": checks,
        "problems": problems,
    }


# ---------------------------------------------------------------------------
# Тонкий сетевой слой: gspread. Порядок строк и формулы сюда не приходят.
# ---------------------------------------------------------------------------

def column_letter(index: int) -> str:
    """A1-буква по номеру столбца (1 -> A)."""
    return chr(ord("A") + index - 1)


def connect() -> Spreadsheet:
    client = gspread.service_account(filename=str(GOOGLE_CREDENTIALS_PATH))
    spreadsheet = client.open_by_key(SPREADSHEET_ID or "")
    logger.info("Таблица открыта: %s", spreadsheet.title)
    return spreadsheet


def prepare_worksheets(spreadsheet: Spreadsheet) -> dict[str, Worksheet]:
    """Нужные три листа: существующий «Лист1» переименовывается, пустой удаляется."""
    sheets = {sheet.title: sheet for sheet in spreadsheet.worksheets()}

    if SHEET_RAW not in sheets and LEGACY_SHEET in sheets:
        sheets[LEGACY_SHEET].update_title(SHEET_RAW)
        logger.info("Лист «%s» переименован в «%s»", LEGACY_SHEET, SHEET_RAW)
        sheets = {sheet.title: sheet for sheet in spreadsheet.worksheets()}

    for title, rows, cols in ((SHEET_RAW, 40, 13), (SHEET_SUMMARY, 20, 12), (SHEET_METHOD, 60, 1)):
        if title not in sheets:
            sheets[title] = spreadsheet.add_worksheet(title, rows=rows, cols=cols)
            logger.info("Создан лист «%s»", title)

    if LEGACY_SHEET in sheets:
        legacy = sheets.pop(LEGACY_SHEET)
        if any(any(cell not in ("", None) for cell in row) for row in legacy.get_values()):
            logger.warning("Лист «%s» непустой — оставляю, не удаляя", LEGACY_SHEET)
            sheets[LEGACY_SHEET] = legacy
        else:
            spreadsheet.del_worksheet(legacy)
            logger.info("Пустой лист «%s» удалён", LEGACY_SHEET)

    return sheets


def write_block(worksheet: Worksheet, block: list[list[Any]], cols: int) -> None:
    """Очистка и запись блока одним вызовом; формулы — через USER_ENTERED."""
    worksheet.clear()
    worksheet.resize(rows=max(len(block) + 1, 10), cols=cols)
    worksheet.update(
        block,
        range_name=f"A1:{column_letter(cols)}{len(block)}",
        value_input_option=ValueInputOption.user_entered,
    )
    logger.info("Лист «%s»: записано %d строк", worksheet.title, len(block))


def column_width_request(sheet_id: int, start: int, end: int, pixels: int) -> dict[str, Any]:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": start,
                "endIndex": end,
            },
            "properties": {"pixelSize": pixels},
            # в Sheets API fieldMask — строка, а не список
            "fields": "pixelSize",
        }
    }


def column_widths(worksheet: Worksheet, spec: list[tuple[int, int, int]]) -> list[dict[str, Any]]:
    return [column_width_request(worksheet.id, start, end, pixels) for start, end, pixels in spec]


def column_index(letter: str) -> int:
    """0-based номер столбца по букве A1 (A -> 0)."""
    return ord(letter) - ord("A")


def repeat_cell_request(sheet_id: int, first_row: int, last_row: int, first_column: int,
                        last_column: int, fields: str, cell_format: dict[str, Any]) -> dict[str, Any]:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": first_row,
                "endRowIndex": last_row,
                "startColumnIndex": first_column,
                "endColumnIndex": last_column,
            },
            "cell": {"userEnteredFormat": cell_format},
            "fields": fields,
        }
    }


def freeze_request(sheet_id: int) -> dict[str, Any]:
    return {
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }
    }


def format_requests(worksheet: Worksheet, last_row: int, header_columns: int,
                    patterns: dict[str, str]) -> list[dict[str, Any]]:
    """Закрепление строки 1, жирный заголовок и форматы чисел одного листа."""
    sheet_id = worksheet.id
    requests: list[dict[str, Any]] = [
        freeze_request(sheet_id),
        repeat_cell_request(sheet_id, 0, 1, 0, header_columns,
                            "userEnteredFormat(textFormat)", {"textFormat": {"bold": True}}),
    ]

    for letters, pattern in patterns.items():
        for letter in letters.split(","):
            index = column_index(letter)
            requests.append(
                repeat_cell_request(
                    sheet_id,
                    FIRST_DATA_ROW - 1,
                    last_row,
                    index,
                    index + 1,
                    "userEnteredFormat.numberFormat",
                    {"numberFormat": {"type": "NUMBER", "pattern": pattern}},
                )
            )
    return requests


def apply_styles(spreadsheet: Spreadsheet, sheets: dict[str, Worksheet],
                 raw_rows: int, summary_rows: int) -> None:
    """Весь лук одним batchUpdate: отдельных write-запросов на формат не шлём."""
    raw = sheets[SHEET_RAW]
    summary = sheets[SHEET_SUMMARY]
    method = sheets[SHEET_METHOD]

    raw_patterns = {
        ",".join(RAW_INT_COLUMNS): PATTERN_INT,
        ",".join(RAW_MONEY_COLUMNS): PATTERN_MONEY,
        ",".join(RAW_API_PERCENT_COLUMNS): PATTERN_API_PERCENT,
        "M": PATTERN_FRACTION_PERCENT,
    }
    summary_patterns = {
        ",".join(SUMMARY_INT_COLUMNS): PATTERN_INT,
        ",".join(SUMMARY_MONEY_COLUMNS): PATTERN_MONEY,
        "H": PATTERN_MONEY_DECIMALS,
        ",".join(SUMMARY_FRACTION_COLUMNS): PATTERN_FRACTION_PERCENT,
    }

    requests: list[dict[str, Any]] = [
        *format_requests(raw, raw_rows, len(RAW_HEADER), raw_patterns),
        *format_requests(summary, summary_rows, len(SUMMARY_HEADER), summary_patterns),
        freeze_request(method.id),
        repeat_cell_request(method.id, 0, 1, 0, 1, "userEnteredFormat(textFormat)",
                           {"textFormat": {"bold": True}}),
        *column_widths(raw, [(0, 1, 120), (1, 2, 112), (2, 12, 92), (12, 13, 88)]),
        *column_widths(summary, [(0, 1, 120), (1, 2, 340), (2, 11, 108), (11, 12, 150)]),
        *column_widths(method, [(0, 1, 980)]),
    ]

    spreadsheet.batch_update({"requests": requests})
    logger.info("Оформление применено одним запросом (%d правил): закрепление, "
                "жирные заголовки, форматы, ширины колонок", len(requests))


def read_back(worksheet: Worksheet, rows: int, cols: int, option: ValueRenderOption) -> list[list[Any]]:
    return worksheet.get_values(f"A1:{column_letter(cols)}{rows}", value_render_option=option)


def log_report(report: dict[str, Any]) -> None:
    for check in report["checks"]:
        if check["ok"]:
            logger.info("ок   %-42s таблица %s = ожидание %s",
                        check["metric"], check["table"], check["expected"])
        else:
            logger.error("РАСХОЖДЕНИЕ %-36s таблица %s, ожидание %s, разница %s",
                         check["metric"], check["table"], check["expected"], check.get("diff"))

    order = report["row_order"]
    logger.info("Порядок строк сводной (выкупы ₽ по убыванию): %s — %s",
                "ок" if order["buyout_sum_descending"] else "нарушен", order["buyout_sequence"])
    logger.info("Ячейки с формулами: %s", report["formula_cells"])
    logger.info("Итог сверки: %s (%d проверок, %d проблем)", "ок" if report["ok"] else "есть расхождения",
                len(report["checks"]), len(report["problems"]))


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    rows = load_funnel_rows()
    stats = load_stats()
    names = load_product_names()
    order = article_order(stats)
    logger.info("Порядок артикулов по сумме выкупов ₽ (убывание): %s",
                ", ".join(str(nm) for nm in order))

    buyout = buyout_percent_semantics(rows)
    logger.info("buyoutPercent: %s", buyout["conclusion"])

    spreadsheet = connect()
    sep = formula_separator(spreadsheet.locale)
    logger.info("Локаль таблицы %s — аргументы формул через «%s»", spreadsheet.locale, sep)

    raw_block = build_raw_block(rows, order, sep)
    summary_block = build_summary_block(order, names, len(rows), sep)
    method_block = [[line] for line in methodology_lines(build_period_meta(stats), stats, order, rows, buyout)]

    sheets = prepare_worksheets(spreadsheet)

    write_block(sheets[SHEET_RAW], raw_block, cols=len(RAW_HEADER))
    write_block(sheets[SHEET_SUMMARY], summary_block, cols=len(SUMMARY_HEADER))
    write_block(sheets[SHEET_METHOD], method_block, cols=1)
    apply_styles(spreadsheet, sheets, len(raw_block), len(summary_block))

    summary_values = read_back(sheets[SHEET_SUMMARY], len(summary_block), len(SUMMARY_HEADER),
                               ValueRenderOption.unformatted)
    summary_formulas = read_back(sheets[SHEET_SUMMARY], len(summary_block), len(SUMMARY_HEADER),
                                 ValueRenderOption.formula)

    report = verify_summary(summary_values, summary_formulas, stats, order)
    report["buyout_percent"] = buyout
    report["written"] = {
        SHEET_RAW: len(raw_block),
        SHEET_SUMMARY: len(summary_block),
        SHEET_METHOD: len(method_block),
    }
    report["verified_at_moscow"] = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")

    VERIFICATION_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    logger.info("Отчёт сверки записан в %s", VERIFICATION_PATH)

    log_report(report)

    if not report["ok"]:
        logger.error("Сверка не прошла — коммит делать нельзя. Полный список проблем:")
        for problem in report["problems"]:
            logger.error("  %s", problem)
        sys.exit(1)

    logger.info("URL: https://docs.google.com/spreadsheets/d/%s/edit", SPREADSHEET_ID)


if __name__ == "__main__":
    main()
