"""Разведка структуры эндпоинта отзывов Wildberries, раунд 2.

За один запуск делает ДВА запроса с паузой между ними:

* A — основной, фильтр `isAnswered=true`: нужен непустой ответ, чтобы увидеть
  поля отдельного отзыва;
* B — зонд: обязателен ли параметр `isAnswered` вообще.

Сырые тела ответов целиком сохраняются в `output/`. Имена полей для парсинга
берутся только оттуда, пагинации и обхода артикулов здесь нет.
"""

import json
import sys
import time
from pathlib import Path
from typing import Any

import requests

from common import NM_IDS, get_logger, wb_get

FEEDBACKS_URL = "https://feedbacks-api.wildberries.ru/api/v1/feedbacks"
OUTPUT_DIR = Path("output")
ANSWERED_SAMPLE_PATH = OUTPUT_DIR / "raw_feedbacks_answered_sample.json"
NOFILTER_SAMPLE_PATH = OUTPUT_DIR / "raw_feedbacks_nofilter_sample.json"
DELAY_BETWEEN_REQUESTS_SECONDS = 1

logger = get_logger(__name__)


def save_raw_sample(payload: Any, path: Path) -> None:
    """Сохраняет ответ целиком, без обрезания полей."""
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    logger.info("Сырой ответ сохранён в %s", path)


def read_json(response: requests.Response, label: str) -> Any:
    """Достаёт JSON из ответа; неразобранное тело — конец работы скрипта."""
    try:
        return response.json()
    except ValueError:
        logger.error("%s: ответ не распарсился как JSON, тело целиком: %s", label, response.text)
        sys.exit(1)


def describe_payload(payload: Any, label: str) -> None:
    """Логирует содержимое `data`: служебные поля и счётчики, массивы и ключи
    первого элемента каждого массива. Ничего не предполагает заранее."""
    if not isinstance(payload, dict):
        logger.error("%s: в корне ожидался JSON-объект, получен %s", label, type(payload).__name__)
        return

    logger.info("%s: ключи верхнего уровня: %s", label, sorted(payload))

    data = payload.get("data")
    if not isinstance(data, dict):
        logger.info("%s: поле data отсутствует или имеет тип %s", label, type(data).__name__)
        return

    logger.info("%s: ключи data: %s", label, sorted(data))
    for key, value in data.items():
        if not isinstance(value, list):
            logger.info("%s: data.%s = %s", label, key, value)
            continue

        logger.info("%s: массив data.%s содержит %d элемент(ов)", label, key, len(value))
        first = value[0] if value else None
        if isinstance(first, dict):
            logger.info("%s: ключи первого элемента data.%s: %s", label, key, sorted(first))
        elif first is not None:
            logger.info("%s: первый элемент data.%s имеет тип %s", label, key, type(first).__name__)
        else:
            logger.warning(
                "%s: массив data.%s пуст: поля отдельного отзыва в этом ответе не видны",
                label,
                key,
            )


def fetch_answered_feedbacks(nm_id: int) -> Any:
    """Запрос A: отзывы с ответом продавца. Ошибка ответа останавливает скрипт."""
    params = {"isAnswered": "true", "nmId": nm_id, "take": 5, "skip": 0}
    logger.info("Запрос A: isAnswered=true, nmId=%s", nm_id)
    try:
        response = wb_get(FEEDBACKS_URL, params)
    except RuntimeError as exc:
        logger.error("Запрос A отклонён, полный текст ответа: %s", exc)
        sys.exit(1)

    logger.info("Запрос A: HTTP-статус %d", response.status_code)
    payload = read_json(response, "Запрос A")
    save_raw_sample(payload, ANSWERED_SAMPLE_PATH)
    describe_payload(payload, "Запрос A")
    return payload


def probe_without_answered_filter(nm_id: int) -> None:
    """Запрос B: тот же запрос без isAnswered.

    4xx здесь — ожидаемый исход зонда: он и означает, что параметр обязателен.
    """
    params = {"nmId": nm_id, "take": 5, "skip": 0}
    logger.info("Зонд B: запрос без isAnswered, nmId=%s", nm_id)
    try:
        response = wb_get(FEEDBACKS_URL, params)
    except RuntimeError as exc:
        logger.warning(
            "Зонд B: ожидаемый исход — без isAnswered API отказывает, дамп не создаётся: %s",
            exc,
        )
        return

    logger.info("Зонд B: HTTP-статус %d — isAnswered не обязателен", response.status_code)
    payload = read_json(response, "Зонд B")
    save_raw_sample(payload, NOFILTER_SAMPLE_PATH)
    describe_payload(payload, "Зонд B")


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    if not NM_IDS:
        logger.error("NM_IDS пуст — брать артикул для разведки неоткуда")
        sys.exit(1)

    fetch_answered_feedbacks(NM_IDS[0])
    time.sleep(DELAY_BETWEEN_REQUESTS_SECONDS)
    probe_without_answered_filter(NM_IDS[0])


if __name__ == "__main__":
    main()
