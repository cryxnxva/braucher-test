"""Сбор всех отзывов по артикулам из `NM_IDS` с пагинацией и выгрузкой.

За один прогон обходит обе ветки (`isAnswered=true` и `isAnswered=false`) по
каждому артикулу, складывает сырые ответы, нормализованные строки и статистику
в каталог `output/`. Имена полей взяты из реального дампа
`output/raw_feedbacks_answered_sample.json`.
"""

import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common import NM_IDS, get_logger, wb_get

FEEDBACKS_URL = "https://feedbacks-api.wildberries.ru/api/v1/feedbacks"
OUTPUT_DIR = Path("output")
ANSWERED_SAMPLE_PATH = OUTPUT_DIR / "raw_feedbacks_answered_sample.json"
RAW_OUT_PATH = OUTPUT_DIR / "reviews_raw.json"
CSV_OUT_PATH = OUTPUT_DIR / "reviews.csv"
STATS_OUT_PATH = OUTPUT_DIR / "reviews_stats.json"

PAGE_SIZE = 1000
MAX_PAGES = 50
REQUEST_PAUSE_SECONDS = 0.4
BRANCH_BY_FILTER = {"true": "answered", "false": "unanswered"}
CSV_COLUMNS = [
    "feedback_id",
    "nm_id",
    "product_name",
    "branch",
    "created_date",
    "valuation",
    "text",
    "pros",
    "cons",
]
# Смещение фиксированное: `zoneinfo` на Windows без пакета tzdata ключ
# Europe/Moscow не находит, а новые зависимости запрещены. С 2014 года в Москве
# нет перехода на летнее время, поэтому +03:00 верён для любой даты отзыва.
MOSCOW = timezone(timedelta(hours=3), "Europe/Moscow")

logger = get_logger(__name__)


def _text_of(value: Any) -> str:
    """Отсутствующее или null-поле становится пустой строкой."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _to_moscow(raw_date: Any) -> str:
    """`createdDate` приходит в UTC — переводим в Europe/Moscow."""
    date_str = _text_of(raw_date)
    if not date_str:
        return ""
    try:
        moment = datetime.fromisoformat(date_str)
    except ValueError:
        logger.warning("Не распознал дату отзыва %r — оставляю как есть", date_str)
        return date_str
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(MOSCOW).isoformat()


def normalize_feedback(fb: dict, nm_id: int, branch: str) -> dict | None:
    """Сырой отзыв → плоская строка для CSV. None, если отзыв без `id`.

    Чистая функция: сети здесь нет, поле за полем по структуре из дампа.
    """
    feedback_id = fb.get("id")
    if not feedback_id:
        logger.warning("Отзыв без поля id — строка отброшена")
        return None

    details = fb.get("productDetails") or {}
    return {
        "feedback_id": str(feedback_id),
        "nm_id": nm_id,
        "product_name": _text_of(details.get("productName")),
        "branch": branch,
        "created_date": _to_moscow(fb.get("createdDate")),
        "valuation": fb.get("productValuation"),
        "text": _text_of(fb.get("text")),
        "pros": _text_of(fb.get("pros")),
        "cons": _text_of(fb.get("cons")),
    }


def _feedbacks_of(payload: Any, label: str) -> list[dict]:
    """Достаёт `data.feedbacks`, не предполагая ничего о соседних полях."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        logger.warning("%s: поле data отсутствует или имеет неожиданный тип", label)
        return []
    items = data.get("feedbacks")
    if not isinstance(items, list):
        logger.warning("%s: поля data.feedbacks нет или это не список", label)
        return []
    return [item for item in items if isinstance(item, dict)]


def fetch_branch(nm_id: int, is_answered: str) -> list[dict]:
    """Собирает одну ветку одного артикула постранично.

    Пустая ветка — штатный итог, а не ошибка. Ошибка HTTP останавливает прогон:
    недособранные данные опаснее, чем упавший скрипт.
    """
    branch = BRANCH_BY_FILTER[is_answered]
    label = f"Артикул {nm_id}, ветка {branch}"
    collected: list[dict] = []
    skip = 0

    for page in range(1, MAX_PAGES + 1):
        params = {"isAnswered": is_answered, "nmId": nm_id, "take": PAGE_SIZE, "skip": skip}
        try:
            response = wb_get(FEEDBACKS_URL, params)
        except RuntimeError as exc:
            logger.error("%s: страница %d отклонена, прогон остановлен: %s", label, page, exc)
            raise
        time.sleep(REQUEST_PAUSE_SECONDS)

        items = _feedbacks_of(response.json(), f"{label}, страница {page}")
        collected.extend(items)
        logger.info(
            "%s: страница %d получена (take=%d, skip=%d) — %d на странице, %d всего",
            label,
            page,
            PAGE_SIZE,
            skip,
            len(items),
            len(collected),
        )

        if not items:
            break
        if len(items) < PAGE_SIZE:
            break
        skip += PAGE_SIZE
    else:
        logger.warning(
            "%s: достигнут потолок MAX_PAGES=%d — ветка могла собрать не всё",
            label,
            MAX_PAGES,
        )

    return collected


def dedup_feedbacks(items: list[dict], nm_id: int, branch: str) -> list[dict]:
    """Убирает повторные `id`, оставляя первое вхождение."""
    seen: set[str] = set()
    unique: list[dict] = []
    for item in items:
        feedback_id = str(item.get("id") or "")
        if not feedback_id:
            unique.append(item)
            continue
        if feedback_id in seen:
            logger.warning(
                "Артикул %s, ветка %s: дубль feedback_id=%s — оставляю первый",
                nm_id,
                branch,
                feedback_id,
            )
            continue
        seen.add(feedback_id)
        unique.append(item)
    return unique


def _counters_of(payload: Any) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if not isinstance(value, list)}


def _read_previous_counters() -> dict[str, Any] | None:
    """Счётчики из дампа ЭТАПА 1.1 — базовый замер для сравнения."""
    if not ANSWERED_SAMPLE_PATH.exists():
        logger.warning("Нет базового дампа %s — сравнивать счётчики не с чем", ANSWERED_SAMPLE_PATH)
        return None
    try:
        with ANSWERED_SAMPLE_PATH.open(encoding="utf-8") as handle:
            return _counters_of(json.load(handle))
    except (OSError, ValueError) as exc:
        logger.warning("Базовый дамп не читается (%s) — пропускаю сравнение", exc)
        return None


def probe_counters() -> dict[str, Any] | None:
    """Зонд гипотезы «счётчики `count*` — по всему магазину, а не по артикулу».

    Спрашивает другой артикул и другую ветку и сравнивает `countArchive` /
    `countUnanswered` с базовым замером. Диагностический: при сбое сбор не
    останавливается.
    """
    if len(NM_IDS) < 2:
        logger.warning("В NM_IDS меньше двух артикулов — зонд счётчиков не проводится")
        return None

    previous = _read_previous_counters()
    params = {"isAnswered": "false", "nmId": NM_IDS[1], "take": 1, "skip": 0}
    logger.info("Зонд счётчиков: nmId=%s, isAnswered=false", NM_IDS[1])
    try:
        response = wb_get(FEEDBACKS_URL, params)
    except RuntimeError as exc:
        logger.warning("Зонд счётчиков не выполнен: %s", exc)
        return None
    time.sleep(REQUEST_PAUSE_SECONDS)

    current = _counters_of(response.json())
    logger.info("Зонд счётчиков: новый ответ %s", current)

    if previous is None:
        conclusion = "базовый замер недоступен, вывод сделать нельзя"
        matches = None
    else:
        logger.info("Зонд счётчиков: базовый дамп %s", previous)
        shared = sorted(set(previous) & set(current))
        matches = bool(shared) and all(previous[key] == current[key] for key in shared)
        if matches:
            conclusion = (
                "Счётчики совпали при другом nmId и другой ветке isAnswered — значит "
                "`countArchive`/`countUnanswered` глобальные по магазину и не годятся "
                "как поартикульные количества. Сколько отзывов по артикулу — считать по "
                "фактически собранным страницам."
            )
        else:
            conclusion = (
                "Счётчики разошлись между запросами — они зависят от артикула или от "
                "фильтра, использовать их как количество по артикулу нельзя без "
                "дополнительной проверки."
            )

    logger.info("Зонд счётчиков, вывод: %s", conclusion)
    return {
        "baseline": {
            "source": str(ANSWERED_SAMPLE_PATH),
            "nm_id": NM_IDS[0],
            "is_answered": "true",
            "counters": previous,
        },
        "probe": {
            "nm_id": NM_IDS[1],
            "is_answered": "false",
            "params": params,
            "counters": current,
        },
        "counters_match": matches,
        "conclusion": conclusion,
    }


def verify_collected_totals(collected_by_nm: dict[str, int]) -> dict[str, Any]:
    """Сверка фактического числа собранных отзывов с `countArchive` по артикулу.

    Это и есть ответ на гипотезу из `probe_counters`: если `countArchive`
    равен числу реально собранных отзывов одного артикула — счётчик
    поартикульный, а не по всему магазину.
    """
    verification: dict[str, Any] = {}
    diffs: list[int] = []

    for nm_id in NM_IDS:
        key = str(nm_id)
        params = {"isAnswered": "true", "nmId": nm_id, "take": 1, "skip": 0}
        try:
            response = wb_get(FEEDBACKS_URL, params)
        except RuntimeError as exc:
            logger.warning("Сверка по артикулу %s не выполнена: %s", nm_id, exc)
            continue
        time.sleep(REQUEST_PAUSE_SECONDS)

        api_total = _counters_of(response.json()).get("countArchive")
        collected = collected_by_nm.get(key, 0)
        if not isinstance(api_total, int):
            logger.warning("Сверка по артикулу %s: поля countArchive нет в ответе", nm_id)
            continue

        diff = collected - api_total
        diffs.append(diff)
        logger.info(
            "Сверка по артикулу %s: countArchive=%d, собрано=%d, разница %+d",
            nm_id,
            api_total,
            collected,
            diff,
        )
        verification[key] = {"count_archive": api_total, "collected": collected, "diff": diff}

    if not verification:
        return {}

    worst = max(abs(diff) for diff in diffs)
    if worst == 0:
        conclusion = (
            "countArchive точно равен числу собранных отзывов по каждому артикулу: "
            "счётчик поартикульный, им можно пользоваться как количеством отзывов."
        )
    elif worst <= 2:
        conclusion = (
            f"countArchive расходится с числом собранных отзывов максимум на {worst} — "
            "это новые отзывы, успевшие появиться между сбором и сверкой. Счётчик "
            "поартикульный, но для отчётов брать фактическое собранное количество."
        )
    else:
        conclusion = (
            "countArchive расходится с фактическим числом собранных отзывов сильнее, "
            "чем на пару записей, — как количество по артикулу он ненадёжен, "
            "опираться нужно на собранные страницы."
        )
    logger.info("Итог сверки счётчиков: %s", conclusion)
    verification["conclusion"] = conclusion
    return verification


def build_stats(rows: list[dict]) -> dict[str, Any]:
    """Сводка по набору нормализованных строк."""
    total = len(rows)
    valuations = [row["valuation"] for row in rows if isinstance(row["valuation"], int)]
    distribution = {str(mark): 0 for mark in range(1, 6)}
    for value in valuations:
        if str(value) in distribution:
            distribution[str(value)] += 1

    rated = [value for value in valuations if 1 <= value <= 5]
    stats: dict[str, Any] = {
        "total": total,
        "by_branch": {
            branch: sum(1 for row in rows if row["branch"] == branch)
            for branch in ("answered", "unanswered")
        },
        "valuation_distribution": distribution,
        "valuation_average": round(sum(rated) / len(rated), 2) if rated else None,
        "outside_1_5_ratings": len(valuations) - len(rated),
        "text_chars_total": sum(len(row["text"]) for row in rows),
    }
    for field in ("text", "pros", "cons"):
        present = sum(1 for row in rows if row[field].strip())
        stats[f"has_{field}_share"] = round(present / total, 4) if total else 0.0
    return stats


def write_raw_json(by_nm: dict[str, dict[str, list[dict]]]) -> None:
    with RAW_OUT_PATH.open("w", encoding="utf-8") as handle:
        json.dump(by_nm, handle, indent=2, ensure_ascii=False)
    logger.info("Сырые отзывы записаны в %s", RAW_OUT_PATH)


def write_csv(rows: list[dict]) -> None:
    with CSV_OUT_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow(["" if row[column] is None else row[column] for column in CSV_COLUMNS])
    logger.info("Нормализованные отзывы записаны в %s (%d строк)", CSV_OUT_PATH, len(rows))


def write_stats(stats: dict[str, Any]) -> None:
    with STATS_OUT_PATH.open("w", encoding="utf-8") as handle:
        json.dump(stats, handle, indent=2, ensure_ascii=False)
    logger.info("Статистика записана в %s", STATS_OUT_PATH)


def log_stats(label: str, stats: dict[str, Any]) -> None:
    logger.info(
        "%s: всего %d (отвеченные %d, без ответа %d), средняя оценка %s, "
        "текст в %.1f%%, плюсы в %.1f%%, минусы в %.1f%%, символов в текстах %d",
        label,
        stats["total"],
        stats["by_branch"]["answered"],
        stats["by_branch"]["unanswered"],
        stats["valuation_average"],
        stats["has_text_share"] * 100,
        stats["has_pros_share"] * 100,
        stats["has_cons_share"] * 100,
        stats["text_chars_total"],
    )
    logger.info("%s: распределение оценок %s", label, stats["valuation_distribution"])


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    if not NM_IDS:
        logger.error("NM_IDS пуст — собирать неоткуда")
        sys.exit(1)

    probe = probe_counters()

    by_nm: dict[str, dict[str, list[dict]]] = {}
    rows: list[dict] = []
    for nm_id in NM_IDS:
        branches: dict[str, list[dict]] = {}
        for is_answered, branch in BRANCH_BY_FILTER.items():
            items = dedup_feedbacks(fetch_branch(nm_id, is_answered), nm_id, branch)
            branches[branch] = items
            for fb in items:
                row = normalize_feedback(fb, nm_id, branch)
                if row is not None:
                    rows.append(row)
        by_nm[str(nm_id)] = branches

    collected_totals: dict[str, int] = {
        nm_key: sum(len(items) for items in branches.values())
        for nm_key, branches in by_nm.items()
    }
    verification = verify_collected_totals(collected_totals)

    stats: dict[str, Any] = {
        "generated_at_moscow": datetime.now(MOSCOW).isoformat(),
        "counters_probe": probe,
        "totals_verification": verification,
        "articles": {
            str(nm_id): build_stats([row for row in rows if row["nm_id"] == nm_id])
            for nm_id in NM_IDS
        },
        "grand_total": build_stats(rows),
    }

    write_raw_json(by_nm)
    write_csv(rows)
    write_stats(stats)

    for nm_id in NM_IDS:
        log_stats(f"Артикул {nm_id}", stats["articles"][str(nm_id)])
    log_stats("ИТОГО по всем артикулам", stats["grand_total"])


if __name__ == "__main__":
    main()
