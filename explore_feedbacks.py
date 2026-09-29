"""Разведка структуры ответа эндпоинта отзывов Wildberries.

Делает РОВНО ОДИН запрос, сохраняет сырое тело ответа в
`output/raw_feedbacks_sample.json` и логирует структуру: ключи верхнего
уровня, ключи `data`, где лежит массив отзывов и сколько в нём элементов,
ключи первого отзыва. Пагинации и обхода артикулов здесь нет.
"""

import json
from pathlib import Path
from typing import Any

from common import NM_IDS, get_logger, wb_get

FEEDBACKS_URL = "https://feedbacks-api.wildberries.ru/api/v1/feedbacks"
OUTPUT_DIR = Path("output")
RAW_SAMPLE_PATH = OUTPUT_DIR / "raw_feedbacks_sample.json"

logger = get_logger(__name__)


def save_raw_sample(payload: Any) -> None:
    """Сохраняет ответ целиком, без обрезания полей."""
    with RAW_SAMPLE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    logger.info("Сырой ответ сохранён в %s", RAW_SAMPLE_PATH)


def describe_structure(payload: Any) -> None:
    """Логирует структуру ответа, ничего не предполагая заранее."""
    if not isinstance(payload, dict):
        logger.error("В корне ответа ожидался JSON-объект, получен %s", type(payload).__name__)
        return

    logger.info("Ключи верхнего уровня: %s", sorted(payload))

    data = payload.get("data")
    if not isinstance(data, dict):
        logger.info("Поле data отсутствует или имеет тип %s", type(data).__name__)
        return

    logger.info("Ключи data: %s", sorted(data))

    for key, value in data.items():
        if not isinstance(value, list):
            continue
        logger.info("Массив data.%s содержит %d элемент(ов)", key, len(value))
        first = value[0] if value else None
        if first is None:
            logger.warning(
                "Массив data.%s пуст: поля отдельного отзыва в этом ответе не видны, "
                "имена полей для парсинга взять неоткуда",
                key,
            )
        elif isinstance(first, dict):
            logger.info("Ключи первого элемента data.%s: %s", key, sorted(first))
        elif first is not None:
            logger.info("Первый элемент data.%s имеет тип %s", key, type(first).__name__)


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    if not NM_IDS:
        logger.error("NM_IDS пуст — брать артикул для разведки неоткуда")
        raise SystemExit(1)

    params = {
        "isAnswered": "false",
        "nmId": NM_IDS[0],
        "take": 5,
        "skip": 0,
    }
    logger.info("Запрос структуры отзывов по артикулу %s", NM_IDS[0])

    try:
        response = wb_get(FEEDBACKS_URL, params)
    except RuntimeError as exc:
        # 400 и прочие 4xx приходят сюда текстом ошибки: логируем его целиком
        # и останавливаемся, правки наугад не делаем.
        logger.error("Запрос отклонён, полный текст ответа: %s", exc)
        raise SystemExit(1) from exc

    logger.info("HTTP-статус: %d", response.status_code)

    try:
        payload = response.json()
    except ValueError:
        logger.error("Ответ не распарсился как JSON, тело целиком: %s", response.text)
        raise SystemExit(1)

    save_raw_sample(payload)
    describe_structure(payload)


if __name__ == "__main__":
    main()
