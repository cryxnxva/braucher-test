"""Разбивание готового промпта на части, безопасные для поля ввода чата.

Вход — только `output/ai_prompt.txt`. Строка отзыва атомарна: режем исключительно
по границам строк, поэтому отзыв никогда не делится между частями. Служебная
строка в конце исходного файла в части не попадает — модели она не нужна.
"""

import re
from pathlib import Path

from common import get_logger

logger = get_logger(__name__)

OUTPUT_DIR = Path("output")
PROMPT_PATH = OUTPUT_DIR / "ai_prompt.txt"
PLAN_PATH = OUTPUT_DIR / "ai_prompt_delivery_plan.md"

MAX_PART_SYMBOLS = 50000
PART_NAME_TEMPLATE = "ai_prompt_part{index:02d}.txt"
CONTINUATION_HEADER = "[Продолжение данных, часть {k} из {n}]"
SERVICE_MARKER = "Служебное:"
SEPARATOR = "---"
ARTICLE_HEADER = "### Товар"
REVIEW_LINE = re.compile(r"^\[\d/5\] ")
PREAMBLE_MESSAGE = (
    "Я пришлю данные для анализа частями. Не начинай анализ, пока я не пришлю "
    'сообщение "Данные закончились".'
)
CONTINUATION_MESSAGE = "Продолжение, часть {k} из {n}."
FINAL_MESSAGE = (
    "Данные закончились. Дай сводку по каждому из трёх товаров в формате из инструкции "
    "(калибровка масштаба + 4 подраздела) и в конце — сравнение трёх товаров "
    "в 2–3 предложениях."
)
_CONSERVATIVE_PARTS_GUESS = 99
_MAX_PACK_ITERATIONS = 6


def is_review_line(line: str) -> bool:
    """Отзыв ли это: строка вида «[5/5] ...», не заголовок продолжения данных."""
    return bool(REVIEW_LINE.match(line))


def strip_service_block(lines: list[str]) -> list[str]:
    """Убирает финальный служебный блок после последней «---»."""
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() != SEPARATOR:
            continue
        dropped = [line for line in lines[index:] if line.strip()]
        if not any(SERVICE_MARKER in line for line in dropped):
            continue
        logger.info(
            "Отброжено служебное (%d строк, %d символов): %s",
            len(dropped),
            sum(len(line) for line in dropped),
            dropped[0][:60],
        )
        return lines[:index]
    logger.warning("Служебного блока после «%s» не нашёл — беру строки целиком", SEPARATOR)
    return lines


def load_body_lines(path: Path) -> list[str]:
    """Строки промпта без финального служебного блока после последней «---»."""
    return strip_service_block(path.read_text(encoding="utf-8").splitlines())


def assert_reviews_preserved(body: list[str], parts: list[list[str]]) -> int:
    """Проверка целостности: отзывы не должны потеряться или продублироваться."""
    in_source = count_reviews(body)
    in_parts = sum(count_reviews(part) for part in parts)
    if in_source != in_parts:
        raise RuntimeError(
            f"Потерялись отзывы: в исходном файле {in_source}, по частям {in_parts}"
        )
    return in_source


def split_instruction(lines: list[str]) -> tuple[list[str], list[str]]:
    """Инструкция до первого заголовка товара и дальше данные по товарам."""
    for index, line in enumerate(lines):
        if line.startswith(ARTICLE_HEADER):
            return lines[:index], lines[index:]
    raise RuntimeError("В промпте нет ни одного заголовка товара — делить нечего")


def continuation_header(part_number: int, parts_total: int) -> str:
    if part_number == 1:
        return ""
    return CONTINUATION_HEADER.format(k=part_number, n=parts_total)


def pack_lines(body: list[str], max_part_symbols: int, parts_total: int) -> list[list[str]]:
    """Собирает части по лимиту; заголовок продолжения учитывается в бюджете.

    Новая часть начинается на границе строки, поэтому отзыв не рвётся. Строка
    длиннее лимита остаётся целой — иначе атомарность нарушится.
    """
    parts: list[list[str]] = []
    current: list[str] = []
    used = 0

    for line in body:
        cost = len(line) + 1
        if current and used + cost > max_part_symbols:
            parts.append(current)
            current, used = [], 0

        if not current:
            header = continuation_header(len(parts) + 1, parts_total)
            if header:
                current.append(header)
                used = len(header) + 1

        current.append(line)
        used += cost

    if current:
        parts.append(current)
    return parts


def build_parts(body: list[str], max_part_symbols: int = MAX_PART_SYMBOLS) -> list[list[str]]:
    """Число частей подобрано так, чтобы «часть k из N» в заголовке было верным."""
    parts_total = _CONSERVATIVE_PARTS_GUESS
    parts = pack_lines(body, max_part_symbols, parts_total)

    for _ in range(_MAX_PACK_ITERATIONS):
        if len(parts) == parts_total:
            return parts
        parts_total = len(parts)
        parts = pack_lines(body, max_part_symbols, parts_total)

    logger.warning(
        "Число частей сошлось не за %d итераций: N в заголовках = %d, фактических частей %d",
        _MAX_PACK_ITERATIONS,
        parts_total,
        len(parts),
    )
    return parts


def count_reviews(lines: list[str]) -> int:
    return sum(1 for line in lines if is_review_line(line))


def part_name(index: int) -> str:
    return PART_NAME_TEMPLATE.format(index=index)


def write_parts(parts: list[list[str]], output_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for index, part in enumerate(parts, start=1):
        path = output_dir / part_name(index)
        path.write_text("\n".join(part) + "\n", encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


def part_size(part: list[str]) -> int:
    """Размер части как он лежит в файле: строки плюс переводы строки."""
    return len("\n".join(part)) + 1


def build_delivery_plan(parts: list[list[str]], paths: list[Path]) -> str:
    """Текст плана отправки с дословными сообщениями для копирования."""
    parts_total = len(parts)
    lines = [
        "# План отправки промпта в чат",
        "",
        f"`output/ai_prompt.txt` разбит на части, чтобы текст дошёл до модели "
        f"целиком. Всего частей: {parts_total}. Части идут строго по порядку, каждая "
        "одним сообщением: сначала служебная фраза (если она нужна), затем содержимое файла.",
        "",
        "## Части",
        "",
        "| Часть | Файл | Символов | Строк отзыва |",
        "| --- | --- | --- | --- |",
    ]

    for index, (part, path) in enumerate(zip(parts, paths), start=1):
        lines.append(f"| {index} | `{path.name}` | {part_size(part)} | {count_reviews(part)} |")

    total_reviews = sum(count_reviews(part) for part in parts)
    total_symbols = sum(part_size(part) for part in parts)
    lines += [
        "",
        f"Итого: {total_symbols} символов, {total_reviews} строк отзыва.",
        "",
        "## Пошагово",
        "",
        f"### 1. Открой новый чат и отправь сообщение:",
        "",
        "```text",
        PREAMBLE_MESSAGE,
        "```",
        "",
        f"### 2. Открой `{paths[0].name}` и отправь его содержимое следующим сообщением.",
        "",
        "Внутри файла уже есть первая строка инструкции и шапка товара 1 — ничего "
        "дописывать не нужно.",
        "",
    ]

    if parts_total > 2:
        lines += ["### 3. Для каждой следующей части (кроме последней) — сначала фраза:", ""]
        for index in range(2, parts_total):
            message = CONTINUATION_MESSAGE.format(k=index, n=parts_total)
            lines += [
                "```text",
                message,
                "```",
                "",
                f"затем содержимое `{paths[index - 1].name}`;",
                "",
            ]
        step = 4
    else:
        step = 3

    last_name = paths[-1].name
    last_header = continuation_header(parts_total, parts_total)
    lines += [
        f"### {step}. Последняя часть — `{last_name}`.",
        "",
        f"Отдельную фразу перед ней можно не писать: первая строка уже внутри файла "
        f"(`{last_header}`). Отправь содержимое файла как есть.",
        "",
        f"### {step + 1}. Сразу после последней части отправь:",
        "",
        "```text",
        FINAL_MESSAGE,
        "```",
        "",
        "## Что сохранить",
        "",
        "- Полный ответ модели сохрани в `output/ai_summary.md`, заменив черновой вариант.",
        "- Первой строкой файла укажи модель, которая давала ответ, например: "
        "`Модель: <название и версия>`.",
        "",
        "## Примечания",
        "",
        "- Служебная строка промпта (`Служебное: символов отзывов...`) в части не входит: "
        "это заметка про сборку файла, модели она не нужна.",
        f"- Лимит на часть — {MAX_PART_SYMBOLS} символов. Если в чате поле ввода "
        "вмещает меньше, уменьши `MAX_PART_SYMBOLS` в `split_ai_prompt.py` и запусти "
        "`python split_ai_prompt.py` ещё раз: `output/ai_prompt.txt` при этом не "
        "пересобирается.",
        "- Если модель всё равно отвечает по одному товару, уменьши лимит ещё — "
        "причина обычно в обрезке длинного сообщения, а не в самих данных.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    body = load_body_lines(PROMPT_PATH)
    if not body:
        raise RuntimeError(f"{PROMPT_PATH} пуст — делить нечего")

    instruction, articles = split_instruction(body)
    parts = build_parts(instruction + articles)
    reviews_in_source = assert_reviews_preserved(body, parts)

    paths = write_parts(parts, OUTPUT_DIR)
    PLAN_PATH.write_text(build_delivery_plan(parts, paths), encoding="utf-8", newline="\n")

    total_symbols = 0
    for index, (part, path) in enumerate(zip(parts, paths), start=1):
        size = part_size(part)
        total_symbols += size
        logger.info(
            "Часть %d: %s — %d символов, %d строк отзыва",
            index,
            path.name,
            size,
            count_reviews(part),
        )

    logger.info(
        "Итог: %d частей, %d символов суммарно, %d строк отзыва, план отправки — %s",
        len(parts),
        total_symbols,
        reviews_in_source,
        PLAN_PATH,
    )


if __name__ == "__main__":
    main()
