"""Разведка эндпоинта воронки продаж Wildberries.

Запрос на [T-7; T-1] с агрегацией по дням, сырой ответ целиком в
`output/raw_funnel_sample.json`, в лог — структура ответа. Имена полей ответа
нигде не предполагаются: списки и их ключи находятся обходом JSON, артикул и дата
распознаются по значениям.

Цепочка ошибок, приведшая тело запроса в текущий вид (каждая правка была
названа текстом 400, ничего не подбиралось наугад):
  1. тело из ТЗ -> 400 `invalid: selectedPeriod (field required), nmIds (field required)`
     -> `nmIDs` переименовано в `nmIds`;
  2. -> 400 то же требование для `selectedPeriod` -> `period` переименован в
     `selectedPeriod`;
  3. -> 400 `decode field "selectedPeriod": invalid: start (field required)`
     -> внутри периода `begin` переименован в `start`;
  4. -> 200, тело больше не правилось.
"""

import json
import re
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import requests

from common import NM_IDS, get_logger, msk_today, wb_post

FUNNEL_URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products/history"
OUTPUT_DIR = Path("output")
RAW_SAMPLE_PATH = OUTPUT_DIR / "raw_funnel_sample.json"
AGGREGATION_LEVEL = "day"

# Разрешённый Tech Lead бюджет дополнительных запросов и пауза между ними.
MAX_ADDITIONAL_REQUESTS = 5
PAUSE_BETWEEN_REQUESTS_SECONDS = 1.0

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}")
REQUIRED_FIELD = re.compile(r"([A-Za-z][A-Za-z0-9_]*) \(field required\)")
# Эндпоинт экранирует кавычки в тексте ошибки: decode field \"selectedPeriod\"
DECODE_CONTEXT = re.compile(r'decode field \\?"([A-Za-z][A-Za-z0-9_]*)\\?"')
AUTH_STATUS = re.compile(r"вернул (401|403)")
# Синонимы границ периода: имя, которое требует API -> наши возможные ключи.
PERIOD_SYNONYMS = {
    "start": ("begin", "from", "datefrom", "startdate"),
    "end": ("finish", "stop", "to", "dateto", "enddate"),
    "begin": ("start",),
    "datefrom": ("begin", "start"),
    "dateto": ("end", "finish"),
}

logger = get_logger(__name__)


def funnel_period() -> tuple[str, str]:
    """Последние 7 дней без сегодняшнего: [T-7; T-1] по Москве."""
    today = msk_today()
    begin = today - timedelta(days=7)
    end = today - timedelta(days=1)
    logger.info("Сегодня по Москве %s, период [%s; %s]", today, begin, end)
    return begin.isoformat(), end.isoformat()


def build_body(begin: str, end: str) -> dict[str, Any]:
    """Тело, которое эндпоинт принял (цепочка правок задокументирована в шапке).

    Формат дат `YYYY-MM-DD` принят без замечаний, даты в дампе пришли ровно
    [T-7; T-1] — сдвига по таймзоне нет. Петля правок ниже остаётся как страховка
    на случай, если эндпоинт начнёт требовать другое.
    """
    return {
        "nmIds": NM_IDS,
        "selectedPeriod": {"start": begin, "end": end},
        "aggregationLevel": AGGREGATION_LEVEL,
    }


def required_fields(message: str) -> list[str]:
    """Поля, которые эндпоинт объявил обязательными, по порядку упоминания."""
    seen: list[str] = []
    for match in REQUIRED_FIELD.finditer(message):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def decode_container(message: str, body: dict[str, Any]) -> str | None:
    """В каком объекте эндпоинт ищет поле (контекст `decode field "X"`).

    Фолбэк нужен на случай, если кавычки в ошибке пришли в другом виде: берём
    только тот ключ, который и так есть в нашем теле и назван в тексте ошибки.
    """
    matches = DECODE_CONTEXT.findall(message)
    if matches:
        return matches[-1]

    lowered = message.lower()
    for key, value in body.items():
        if not isinstance(value, dict) or "invalid:" not in lowered:
            continue
        # ключ должен стоять отдельно: period не должен ловиться внутри selectedPeriod
        if re.search(rf"(?<![a-z0-9]){re.escape(key.lower())}(?![a-z0-9])", lowered):
            return key
    return None


def find_replaceable(scope: dict[str, Any], required: str) -> str | None:
    """Наш ключ, который эндпоинт просит назвать иначе, либо None."""
    lowered = {key.lower(): key for key in scope}
    if required.lower() in lowered:
        candidate = lowered[required.lower()]
        return candidate if candidate != required else None

    for synonym in PERIOD_SYNONYMS.get(required.lower(), ()):
        if synonym in lowered:
            return lowered[synonym]
    return None


def apply_error_dictated_change(body: dict[str, Any], message: str) -> dict[str, Any] | None:
    """Ровно ОДНА правка тела — та, которую назвала ошибка. Иначе None."""
    container = decode_container(message, body)
    scope = body.get(container) if container else None
    if not isinstance(scope, dict):
        container, scope = None, body

    for required in required_fields(message):
        if required in scope:
            continue
        ours = find_replaceable(scope, required)
        if ours is None:
            logger.warning(
                "Ошибка требует поле %s (контейнер: %s), но заменить нечем — "
                "подбором имен заниматься не будем",
                required,
                container or "корень",
            )
            continue

        corrected = dict(body)
        if container:
            inner = dict(scope)
            inner[required] = inner.pop(ours)
            corrected[container] = inner
        else:
            corrected[required] = corrected.pop(ours)

        logger.warning(
            "Правка по тексту ошибки (%s): %s -> %s",
            container or "корень",
            ours,
            required,
        )
        return corrected

    return None


def request_history(begin: str, end: str) -> requests.Response:
    """Итеративный запрос: одна правка по тексту ошибки на запрос, бюджет 5."""
    body = build_body(begin, end)
    chain: list[str] = []

    for attempt in range(1, MAX_ADDITIONAL_REQUESTS + 1):
        logger.info(
            "Запрос %d из %d: POST %s, тело %s",
            attempt,
            MAX_ADDITIONAL_REQUESTS,
            FUNNEL_URL,
            json.dumps(body, ensure_ascii=False),
        )
        try:
            response = wb_post(FUNNEL_URL, body)
        except RuntimeError as exc:
            message = str(exc)
            chain.append(message)
            logger.error("Запрос %d не удался: %s", attempt, message)

            if AUTH_STATUS.search(message):
                log_chain(chain)
                logger.error("401/403 — это авторизация, правкой тела не лечится")
                sys.exit(1)
            if chain.count(message) >= 2 and chain[-1] == chain[-2]:
                log_chain(chain)
                logger.error("Одна и та же ошибка дважды подряд — правка не сработала")
                sys.exit(1)
            if attempt == MAX_ADDITIONAL_REQUESTS:
                log_chain(chain)
                logger.error("Бюджет %d запросов исчерпан, 200 не получен", MAX_ADDITIONAL_REQUESTS)
                sys.exit(1)

            corrected = apply_error_dictated_change(body, message)
            if corrected is None:
                log_chain(chain)
                logger.error("Ошибка не указывает поле, которое можно править — стоп")
                sys.exit(1)

            body = corrected
            time.sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)
            continue

        logger.info("Эндпоинт принял тело: %s", json.dumps(body, ensure_ascii=False))
        return response

    log_chain(chain)
    raise RuntimeError("Бюджет запросов исчерпан")


def log_chain(chain: list[str]) -> None:
    logger.error("Цепочка ошибок целиком (%d записей):", len(chain))
    for index, message in enumerate(chain, start=1):
        logger.error("  %d) %s", index, message)


def find_lists(node: Any, path: str, found: list[tuple[str, list[Any]]]) -> None:
    """Все пути JSON, по которым лежит список."""
    if isinstance(node, dict):
        for key, value in node.items():
            find_lists(value, f"{path}.{key}", found)
    elif isinstance(node, list):
        found.append((path, node))
        for item in node[:1]:
            find_lists(item, f"{path}[]", found)


def is_date_value(value: Any) -> bool:
    return isinstance(value, str) and bool(DATE_PATTERN.match(value))


def article_of(node: dict[str, Any]) -> str | None:
    """Значение поля, совпадающее с одним из артикулов из `.env`."""
    public = {str(nm) for nm in NM_IDS}
    for key, value in node.items():
        if str(value) in public:
            return f"{key}={value}"
    return None


def describe_structure(payload: Any) -> None:
    if isinstance(payload, dict):
        logger.info("Ключи верхнего уровня: %s", sorted(payload))
    elif isinstance(payload, list):
        logger.info("Корень ответа — список из %d элементов", len(payload))
    else:
        logger.error("В корне ожидался объект или список, получен %s", type(payload).__name__)
        return

    lists: list[tuple[str, list[Any]]] = []
    find_lists(payload, "$", lists)

    for path, items in lists:
        rows = [item for item in items if isinstance(item, dict)]
        logger.info("Путь %s: список, %d элементов, словарей %d", path, len(items), len(rows))
        if rows:
            logger.info("   ключи первого элемента %s: %s", path, sorted(rows[0]))
            dates = [key for key in rows[0] if is_date_value(rows[0].get(key))]
            if dates:
                logger.info("   вероятные поля с датой в %s: %s", path, dates)


def find_article(node: Any) -> str | None:
    """Первое значение в поддереве, совпадающее с артикулом из `.env`."""
    if isinstance(node, dict):
        direct = article_of(node)
        if direct:
            return direct
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None

    for child in children:
        found = find_article(child)
        if found:
            return found
    return None


def collect_dates(node: Any, bucket: dict[str, int]) -> None:
    """Собирает все даты вида YYYY-MM-DD из поддерева в счётчик."""
    values: list[Any] = []
    if isinstance(node, dict):
        values = list(node.values())
    elif isinstance(node, list):
        values = node

    for value in values:
        if is_date_value(value):
            bucket[value[:10]] = bucket.get(value[:10], 0) + 1
        else:
            collect_dates(value, bucket)


def summarize_by_article(payload: Any) -> None:
    """По каждому артикулу: какие даты пришли и сколько записей на дату."""
    items = payload if isinstance(payload, list) else [payload]

    for item in items:
        article = find_article(item)
        if article is None:
            logger.warning("В элементе ответа не найден артикул: ключи %s", sorted(item) if isinstance(item, dict) else type(item).__name__)
            continue
        days: dict[str, int] = {}
        collect_dates(item, days)
        if not days:
            logger.warning("Артикул %s: дневных дат не найдено", article)
            continue
        logger.info(
            "Артикул %s: %d уникальных дат, диапазон %s .. %s, записей с датой %d",
            article,
            len(days),
            min(days),
            max(days),
            sum(days.values()),
        )


def save_raw(payload: Any) -> None:
    with RAW_SAMPLE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    logger.info("Сырой ответ сохранён в %s (%d байт)", RAW_SAMPLE_PATH, RAW_SAMPLE_PATH.stat().st_size)


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    if not NM_IDS:
        logger.error("NM_IDS пуст — запрашивать не по чему")
        sys.exit(1)

    begin, end = funnel_period()
    response = request_history(begin, end)
    logger.info("HTTP-статус: %d", response.status_code)

    try:
        payload = response.json()
    except ValueError:
        logger.error("Ответ не JSON, тело целиком: %s", response.text)
        sys.exit(1)

    save_raw(payload)
    describe_structure(payload)
    summarize_by_article(payload)


if __name__ == "__main__":
    main()
