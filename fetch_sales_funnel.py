"""Сбор воронки продаж за последние 7 дней (без сегодня) по артикулам из `.env`.

Один POST на тот же эндпоинт и с тем же телом, которое приняла разведка
(`explore_sales_funnel.py`). Имена полей ответа взяты из живого дампа
`output/raw_funnel_sample.json`: корень — список товаров, у товара `product`
(с `nmId`, `title`) и `history` — список дневных записей с полями `date`,
`openCount`, `cartCount`, `orderCount`, `orderSum`, `buyoutCount`, `buyoutSum`,
`buyoutPercent`, `addToCartConversion`, `cartToOrderConversion`,
`addToWishlistCount`.

Выгрузки: `output/funnel_raw.json`, `output/funnel.csv`, `output/funnel_stats.json`.
Сырой ответ пишется до вердикта проверки периода; сама проверка блокирует работу:
при дубле дневной строки, дате вне периода, сегодняшней дате или пропавшем
артикуле скрипт выходит с кодом 1 и не обновляет CSV и контрольные цифры.

Определения для методологии (ЭТАП 5 сверяет с ними формулы таблицы):
  * Средний чек = сумма заказов в рублях / количество заказов в штуках за
    период. Если заказов за период нет — средний чек равен null (не делим на
    ноль и не подменяем нулём).
  * CR (конверсия в заказ) по дням = заказы в штуках / показы * 100. Показы —
    `openCount` (открытия карточки, выбор обоснован ниже). Дневные значения CR
    считаются из целых счётчиков, без предварительного округления; в итоговом
    отчёте округление до двух знаков. Дни с нулевыми показами из статистики CR
    исключаются, их количество фиксируется отдельно.
"""

import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from common import MOSCOW_TZ, NM_IDS, get_logger, msk_today, wb_post

logger = get_logger(__name__)

FUNNEL_URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products/history"
OUTPUT_DIR = Path("output")
RAW_OUT_PATH = OUTPUT_DIR / "funnel_raw.json"
CSV_OUT_PATH = OUTPUT_DIR / "funnel.csv"
STATS_OUT_PATH = OUTPUT_DIR / "funnel_stats.json"

AGGREGATION_LEVEL = "day"
PERIOD_DAYS = 7
CR_DECIMALS = 2
# Служебные поля строки и производные показатели: показы ищутся не среди них.
ID_FIELDS = ("nmId", "nm_id", "date")
DERIVED_SUFFIXES = ("Percent", "Conversion")
SHOWS_FALLBACK_PRIORITY = ("openCount", "addToWishlistCount", "cartCount")


def funnel_period() -> tuple[date, date]:
    """Последние 7 дней без сегодняшнего: [T-7; T-1] по Москве."""
    today = msk_today()
    return today - timedelta(days=PERIOD_DAYS), today - timedelta(days=1)


def build_body(begin: date, end: date) -> dict[str, Any]:
    """Тело запроса, принятое эндпоинтом на разведке, дословно."""
    return {
        "nmIds": NM_IDS,
        "selectedPeriod": {"start": begin.isoformat(), "end": end.isoformat()},
        "aggregationLevel": AGGREGATION_LEVEL,
    }


def request_funnel(begin: date, end: date) -> Any:
    body = build_body(begin, end)
    logger.info("POST %s, тело %s", FUNNEL_URL, json.dumps(body, ensure_ascii=False))
    try:
        response = wb_post(FUNNEL_URL, body)
    except RuntimeError as exc:
        logger.error("Эндпоинт отклонил запрос, разведанное тело не подошло: %s", exc)
        sys.exit(1)

    logger.info("HTTP-статус: %d", response.status_code)
    try:
        return response.json()
    except ValueError:
        logger.error("Ответ не распарсился как JSON, тело целиком: %s", response.text)
        sys.exit(1)


def items_of(payload: Any) -> list[dict[str, Any]]:
    """Список товаров в ответе: корень-список или `data` со списком."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def history_of(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Дневные записи товара; поле-список ищем по содержимому, а не по имени."""
    for value in item.values():
        if isinstance(value, list) and value and isinstance(value[0], dict) and "date" in value[0]:
            return [row for row in value if isinstance(row, dict)]
    return []


def nm_id_of(item: dict[str, Any]) -> int | None:
    for value in item.values():
        if isinstance(value, dict) and isinstance(value.get("nmId"), int):
            return value["nmId"]
        if isinstance(value, int) and value in set(NM_IDS):
            return value
    return None


def title_of(item: dict[str, Any]) -> str:
    for value in item.values():
        if isinstance(value, dict) and value.get("title"):
            return str(value["title"])
    return ""


def normalize_rows(payload: Any) -> list[dict[str, Any]]:
    """Одна строка на пару артикул × дата, метрики — как пришли из API."""
    rows: list[dict[str, Any]] = []
    for item in items_of(payload):
        nm_id = nm_id_of(item)
        if nm_id is None:
            logger.warning("В элементе ответа не найден артикул, ключи: %s", sorted(item))
            continue
        for day in history_of(item):
            row: dict[str, Any] = {"nm_id": nm_id, "date": str(day.get("date", ""))[:10]}
            for key, value in day.items():
                if key != "date":
                    row[key] = value
            rows.append(row)
    return rows


def detect_shows_field(rows: list[dict[str, Any]]) -> tuple[str, str]:
    """Поле показов выбирается по арифметике самого дампа, а не по имени.

    Приоритет — открытиям карточки. Если в ответе есть `cartCount` и
    `addToCartConversion`, знаменатель первой ступени воронки проверяется
    тождеством round(cartCount / кандидат * 100) == addToCartConversion: оно
    выполнивается ровно для одного поля.
    """
    if not rows:
        return SHOWS_FALLBACK_PRIORITY[0], "дневных записей нет — поле взято по приоритету имён"

    numeric = [
        key
        for key in rows[0]
        if isinstance(rows[0][key], int) and key not in ID_FIELDS and not key.endswith(DERIVED_SUFFIXES)
    ]
    nonzero = [key for key in numeric if any(row.get(key) for row in rows)]

    if "cartCount" in rows[0] and "addToCartConversion" in rows[0]:
        for candidate in nonzero:
            if candidate == "cartCount":
                continue
            tested = [row for row in rows if row.get(candidate)]
            if not tested:
                continue
            if all(
                round(row["cartCount"] / row[candidate] * 100) == row["addToCartConversion"]
                for row in tested
            ):
                return candidate, (
                    f"round(cartCount / {candidate} * 100) == addToCartConversion выполнилось на "
                    f"{len(tested)} из {len(rows)} дневных записей; ненулевые кандидаты: {nonzero}"
                )
        fallback_note = (
            f"тождество с addToCartConversion не выполнилось ни для одного кандидата ({nonzero}), "
            "взято по приоритету открытий карточки"
        )
    else:
        fallback_note = f"в дампе нет пары cartCount/addToCartConversion для проверки, кандидаты: {numeric}"

    for candidate in SHOWS_FALLBACK_PRIORITY:
        if candidate in numeric:
            return candidate, fallback_note

    return SHOWS_FALLBACK_PRIORITY[0], f"{fallback_note}; знакового поля не найдено"


def check_period(
    rows: list[dict[str, Any]],
    begin: date,
    end: date,
    today: date,
    requested: list[int] | None = None,
) -> dict[str, Any]:
    """Верификация периода: 7 дат, ровно по одной строке на пару артикул×день.

    Проверяются артикулы, которые реально пришли, а `requested` (по умолчанию
    артикулы из `.env`) нужен, чтобы заметить отсутствующий товар. Число
    уникальных дат само по себе дубликат не ловит: строка-дубль раздувает суммы
    за период, оставляя счётчик дат правильным, поэтому сравниваются и число
    строк, и число уникальных дат.
    """
    requested_ids = list(NM_IDS) if requested is None else list(requested)
    articles: dict[str, Any] = {}
    problems_all: list[str] = []

    present = sorted({row["nm_id"] for row in rows})
    for nm_id in present:
        key = str(nm_id)
        article_rows = [row for row in rows if row["nm_id"] == nm_id]
        day_counts: dict[str, int] = {}
        for row in article_rows:
            day_counts[row["date"]] = day_counts.get(row["date"], 0) + 1
        dates = sorted(day_counts)
        duplicates = sorted(day for day, size in day_counts.items() if size > 1)
        problems: list[str] = []
        if len(dates) != PERIOD_DAYS:
            problems.append(f"уникальных дат {len(dates)}, ожидалось {PERIOD_DAYS}")
        if len(article_rows) != len(dates):
            problems.append(
                f"строк {len(article_rows)} при {len(dates)} уникальных датах — "
                f"дубликаты по дням: {duplicates}"
            )
        for value in dates:
            day = date.fromisoformat(value)
            if not begin <= day <= end:
                problems.append(f"дата {value} вне периода [{begin}; {end}]")
            if day == today:
                problems.append(f"дата {value} — сегодня, а сегодня исключено")

        if problems:
            problems_all.extend(f"{key}: {problem}" for problem in problems)
        articles[key] = {
            "unique_dates": len(dates),
            "rows": len(article_rows),
            "dates": dates,
            "duplicate_dates": duplicates,
            "ok": not problems,
            "problems": problems,
        }

    missing = [str(nm) for nm in requested_ids if nm not in present]
    for nm in missing:
        problems_all.append(f"{nm}: артикул не пришёл в ответе")

    return {
        "ok": not problems_all,
        "period": {"begin": begin.isoformat(), "end": end.isoformat(), "days": PERIOD_DAYS},
        "today_excluded": today.isoformat(),
        "missing_articles": missing,
        "articles": articles,
        "problems": problems_all,
    }


def control_figures(rows: list[dict[str, Any]], shows_field: str) -> dict[str, Any]:
    """Контрольные цифры за период по набору дневных строк."""
    shows = sum(int(row.get(shows_field) or 0) for row in rows)
    orders_count = sum(int(row.get("orderCount") or 0) for row in rows)
    orders_sum = sum(int(row.get("orderSum") or 0) for row in rows)
    buyout_count = sum(int(row.get("buyoutCount") or 0) for row in rows)
    buyout_sum = sum(int(row.get("buyoutSum") or 0) for row in rows)

    # Средний чек: при нулевых заказах — null, деления на ноль не делаем.
    avg_check = round(orders_sum / orders_count, CR_DECIMALS) if orders_count else None

    daily_ratios: list[float] = []
    zero_shows = 0
    for row in rows:
        day_shows = int(row.get(shows_field) or 0)
        if day_shows == 0:
            zero_shows += 1
            continue
        daily_ratios.append(int(row.get("orderCount") or 0) / day_shows * 100)

    # Среднее, минимум и максимум считаются по НЕ округлённым дневным CR:
    # округление только одно, в конце, иначе среднее расходится с формулой
    # таблицы (=AVERAGE(заказы/показы)) на сотые доли.
    cr = {
        "avg": round(sum(daily_ratios) / len(daily_ratios), CR_DECIMALS) if daily_ratios else None,
        "min": round(min(daily_ratios), CR_DECIMALS) if daily_ratios else None,
        "max": round(max(daily_ratios), CR_DECIMALS) if daily_ratios else None,
        "days_counted": len(daily_ratios),
        "days_with_zero_shows": zero_shows,
    }

    return {
        "days": len({row["date"] for row in rows}),
        "shows_field": shows_field,
        "shows": shows,
        "orders_count": orders_count,
        "orders_sum": orders_sum,
        "buyout_count": buyout_count,
        "buyout_sum": buyout_sum,
        "avg_check": avg_check,
        "cr_percent": cr,
        "daily_cr": [round(value, CR_DECIMALS) for value in daily_ratios],
    }


def csv_columns(rows: list[dict[str, Any]]) -> list[str]:
    """Колонки CSV: служебные + все метрики дневной записи по порядку дампа."""
    columns = ["nm_id", "date"]
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns


def write_csv(rows: list[dict[str, Any]], columns: list[str]) -> None:
    with CSV_OUT_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row.get(column, "") for column in columns])
    logger.info("Воронка по дням записана в %s (%d строк)", CSV_OUT_PATH, len(rows))


def write_raw(payload: Any) -> None:
    with RAW_OUT_PATH.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    logger.info("Сырой ответ записан в %s (%d байт)", RAW_OUT_PATH, RAW_OUT_PATH.stat().st_size)


def write_stats(stats: dict[str, Any]) -> None:
    with STATS_OUT_PATH.open("w", encoding="utf-8") as handle:
        json.dump(stats, handle, indent=2, ensure_ascii=False)
    logger.info("Контрольные цифры записаны в %s", STATS_OUT_PATH)


def log_figures(label: str, figures: dict[str, Any]) -> None:
    cr = figures["cr_percent"]
    logger.info(
        "%s: дней %d, показы %d, заказы %d шт / %d ₽, выкупы %d шт / %d ₽, "
        "средний чек %s",
        label,
        figures["days"],
        figures["shows"],
        figures["orders_count"],
        figures["orders_sum"],
        figures["buyout_count"],
        figures["buyout_sum"],
        figures["avg_check"],
    )
    logger.info(
        "%s: CR по дням среднее %s, мин %s, макс %s (дней в расчёте %d, "
        "дней с нулевыми показами %d)",
        label,
        cr["avg"],
        cr["min"],
        cr["max"],
        cr["days_counted"],
        cr["days_with_zero_shows"],
    )


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    if not NM_IDS:
        logger.error("NM_IDS пуст — запрашивать не по чему")
        sys.exit(1)

    begin, end = funnel_period()
    today = msk_today()
    logger.info("Сегодня по Москве %s, период [%s; %s]", today, begin, end)

    payload = request_funnel(begin, end)
    # Дамп пишется до любых вердиктов: при провале разбирать нужно именно ответ API.
    write_raw(payload)
    rows = normalize_rows(payload)
    if not rows:
        logger.error("В ответе нет ни одной дневной строки — сырой ответ лежит в %s", RAW_OUT_PATH)
        sys.exit(1)

    arrived = sorted({row["nm_id"] for row in rows})
    for nm_id in NM_IDS:
        if nm_id not in arrived:
            logger.warning("Артикул %s не пришёл в ответе — проверка периода это заблокирует", nm_id)

    shows_field, rationale = detect_shows_field(rows)
    logger.info("Поле показов: %s — %s", shows_field, rationale)

    period = check_period(rows, begin, end, today)
    if period["ok"]:
        logger.info("Проверка периода: ок — по %d артикулам по %d уникальных дат по одной строке, "
                    "сегодня исключена", len(period["articles"]), PERIOD_DAYS)
    else:
        logger.error("Проверка периода не пройдена: %s", period["problems"])
        logger.error("Файлы воронки не обновляю: сводка по неверному периоду вводит заказчика "
                     "в заблуждение. Сырой ответ лежит в %s", RAW_OUT_PATH)
        sys.exit(1)

    by_article = {nm_id: [row for row in rows if row["nm_id"] == nm_id] for nm_id in arrived}
    titles = {nm_id: title_of(item) for item in items_of(payload) if (nm_id := nm_id_of(item)) is not None}
    stats: dict[str, Any] = {
        "generated_at_moscow": datetime.now(MOSCOW_TZ).isoformat(),
        "request_body": build_body(begin, end),
        "shows_field": {"field": shows_field, "rationale": rationale},
        "period_check": period,
        "articles": {
            str(nm_id): {
                "title": titles.get(nm_id, ""),
                **control_figures(article_rows, shows_field),
            }
            for nm_id, article_rows in by_article.items()
        },
        "grand_total": control_figures(rows, shows_field),
    }

    columns = csv_columns(rows)
    write_csv(rows, columns)
    write_stats(stats)

    for nm_id in arrived:
        log_figures(f"Артикул {nm_id}", stats["articles"][str(nm_id)])
    log_figures("ИТОГО по всем артикулам", stats["grand_total"])


if __name__ == "__main__":
    main()
