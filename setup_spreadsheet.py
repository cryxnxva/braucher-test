"""Проверка доступа к Google Таблице под сервисным аккаунтом.

Одноразовая утилита: проверяет, что credentials-файл и `SPREADSHEET_ID` на месте,
что сервисный аккаунт поднимается и что таблица открывается, печатает заголовок,
URL и список листов. Если передан e-mail (первым аргументом), выдаёт этому
аккаунту роль Редактора.

Содержимое `.env` и `credentials.json` в лог не попадает; адрес сервисного
аккаунта (`client_email`) печатается намеренно — это не секрет, он и нужен для
выдачи доступа. Провал любой проверки — не ошибка скрипта: логируется точная
блокировка, выход всегда 0.
"""

import json
import sys
from pathlib import Path
from typing import Any

import gspread
from gspread.exceptions import APIError, SpreadsheetNotFound

from common import GOOGLE_CREDENTIALS_PATH, SPREADSHEET_ID, get_logger

logger = get_logger(__name__)
SHEET_URL_TEMPLATE = "https://docs.google.com/spreadsheets/d/{key}/edit"
WRITER_ROLE = "writer"


def credentials_path() -> Path:
    return Path(GOOGLE_CREDENTIALS_PATH or "credentials.json")


def credentials_file_ready() -> bool:
    path = credentials_path()
    if not path.is_file():
        logger.error("Файл сервисного аккаунта не найден по пути %s — положи credentials.json в корень проекта", path)
        return False
    logger.info("Файл сервисного аккаунта найден: %s", path)
    return True


def service_account_email() -> str | None:
    """Адрес сервисного аккаунта из credentials — его и нужно добавить в доступы."""
    try:
        payload: dict[str, Any] = json.loads(credentials_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.error("credentials не читается: %s: %s", type(exc).__name__, str(exc)[:200])
        return None
    email = payload.get("client_email")
    if not email:
        logger.error("В credentials нет поля client_email — это не ключ сервисного аккаунта?")
    return str(email) if email else None


def spreadsheet_id_ready() -> bool:
    if not (SPREADSHEET_ID or "").strip():
        logger.error("SPREADSHEET_ID в .env пуст — открывать таблицу не по чему")
        return False
    logger.info("SPREADSHEET_ID задан (длина %d символов)", len(SPREADSHEET_ID or ""))
    return True


def connect() -> gspread.Client | None:
    try:
        client = gspread.service_account(filename=str(GOOGLE_CREDENTIALS_PATH))
    except Exception as exc:
        logger.error("Сервисный аккаунт не авторизован: %s: %s", type(exc).__name__, exc)
        return None
    logger.info("Сервисный аккаунт авторизован")
    return client


def open_sheet(client: gspread.Client, account: str | None) -> gspread.Spreadsheet | None:
    try:
        return client.open_by_key(SPREADSHEET_ID or "")
    except SpreadsheetNotFound:
        logger.error("Таблица с ключом из SPREADSHEET_ID не найдена — проверь ID в .env")
        return None
    except APIError as exc:
        logger.error("Таблица не открылась: %s", str(exc)[:400])
        logger.error(
            "Скорее всего нет доступа: выдай сервисному аккаунту %s роль Редактор "
            "на таблицу (кнопка Настройки доступа)",
            account or "из credentials",
        )
        return None


def describe(sheet: gspread.Spreadsheet) -> None:
    logger.info("Заголовок таблицы: %s", sheet.title)
    logger.info("URL: %s", SHEET_URL_TEMPLATE.format(key=SPREADSHEET_ID))
    logger.info("Листы: %s", [worksheet.title for worksheet in sheet.worksheets()])


def grant_writer(sheet: gspread.Spreadsheet, email: str) -> None:
    try:
        sheet.share(email, perm_type="user", role=WRITER_ROLE)
    except APIError as exc:
        logger.error("Роль не выдана: %s", exc)
        return
    logger.info("Выдана роль %s пользователю %s", WRITER_ROLE, email)


def main() -> None:
    email = sys.argv[1].strip() if len(sys.argv) > 1 else ""

    if not credentials_file_ready():
        return
    account = service_account_email()
    if account:
        logger.info("Сервисный аккаунт: %s", account)
    if not spreadsheet_id_ready():
        return
    client = connect()
    if client is None:
        return
    sheet = open_sheet(client, account)
    if sheet is None:
        return

    describe(sheet)

    if email:
        grant_writer(sheet, email)
    else:
        logger.info("E-mail не передан — роль Редактор никому не выдавалась")


if __name__ == "__main__":
    main()
