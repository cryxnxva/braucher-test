"""Конфигурация, логирование и HTTP-клиент API Wildberries.

Единственная точка доступа к секретам из `.env` и единственный способ делать
запросы к WB API: скрипты проекта должны использовать `wb_get` / `wb_post`.
"""

import logging
import os
import sys
import time
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

_MAX_BACKOFF_SECONDS = 60
_REQUEST_TIMEOUT_SECONDS = 30
_ERROR_BODY_CHARS = 500

WB_TOKEN: str | None = os.getenv("WB_TOKEN") or None
SPREADSHEET_ID: str | None = os.getenv("SPREADSHEET_ID") or None
NM_IDS: list[int] = [int(part) for part in os.getenv("NM_IDS", "").split(",") if part.strip()]


def get_logger(name: str) -> logging.Logger:
    """Логгер с выводом в консоль на уровне INFO.

    Повторный вызов с тем же именем не добавляет второй обработчик.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        _use_utf8_console()
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def _use_utf8_console() -> None:
    """Без явного UTF-8 консоль Windows экранирует кириллицу в логах."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


logger = get_logger(__name__)


def _retry_delay_seconds(response: requests.Response | None, attempt: int) -> int:
    """Пауза перед повтором: Retry-After сервера, иначе экспоненциальная."""
    retry_after = response.headers.get("Retry-After") if response is not None else None
    if retry_after:
        try:
            # Retry-After задаёт сам WB — отдаём ровно столько, сколько просит.
            return int(retry_after.strip())
        except ValueError:
            logger.warning("Неразбираемый Retry-After %r, беру экспоненциальную паузу", retry_after)
    return min(2**attempt, _MAX_BACKOFF_SECONDS)


def wb_request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    max_retries: int = 5,
) -> requests.Response:
    """Запрос к WB API с повторами на 429/5xx и сетевых сбоях.

    Токен ставится в заголовок `Authorization` без префикса `Bearer`.
    Бросает RuntimeError, если токен не задан, ответ — прочий 4xx, или попытки
    исчерпаны.
    """
    if not WB_TOKEN:
        raise RuntimeError("WB_TOKEN не задан в .env — запросы к API Wildberries недоступны")

    headers = {"Authorization": WB_TOKEN}

    for attempt in range(max_retries):
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=body,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            delay = _retry_delay_seconds(None, attempt)
            logger.warning(
                "%s %s: сетевая ошибка (%s), попытка %d/%d, пауза %d с",
                method.upper(),
                url,
                exc,
                attempt + 1,
                max_retries,
                delay,
            )
            time.sleep(delay)
            continue

        status = response.status_code
        if status == 200:
            logger.info("%s %s -> %d", method.upper(), url, status)
            return response

        if status == 429 or 500 <= status < 600:
            delay = _retry_delay_seconds(response, attempt)
            logger.warning(
                "%s %s -> %d, попытка %d/%d, пауза %d с",
                method.upper(),
                url,
                status,
                attempt + 1,
                max_retries,
                delay,
            )
            time.sleep(delay)
            continue

        raise RuntimeError(
            f"{method.upper()} {url} вернул {status}: {response.text[:_ERROR_BODY_CHARS]}"
        )

    raise RuntimeError(
        f"{method.upper()} {url}: не удалось получить ответ за {max_retries} попыток"
    )


def wb_get(url: str, params: dict[str, Any] | None = None) -> requests.Response:
    """GET к WB API через общий механизм повторов."""
    return wb_request("GET", url, params=params)


def wb_post(url: str, body: dict[str, Any]) -> requests.Response:
    """POST с JSON-телом к WB API через общий механизм повторов."""
    return wb_request("POST", url, body=body)
