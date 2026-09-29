"""Сборка промпта для ИИ-сводки по отзывам.

Источник данных — только локальные файлы предыдущего этапа:
`output/reviews.csv` и `output/reviews_stats.json`. Запросов к API здесь нет.

Отбор детерминирован: фиксированный seed, стабильный порядок строк CSV и
крупнейших остатков при делении бюджета, поэтому повторный запуск даёт
побайтово одинаковый файл.
"""

import csv
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

from common import get_logger

logger = get_logger(__name__)

OUTPUT_DIR = Path("output")
REVIEWS_CSV_PATH = OUTPUT_DIR / "reviews.csv"
STATS_JSON_PATH = OUTPUT_DIR / "reviews_stats.json"
PROMPT_PATH = OUTPUT_DIR / "ai_prompt.txt"

TOTAL_BUDGET = 150000
MAX_FIELD_LEN = 300
SEED = 42
TRUNCATION_MARK = "..."

CLASS_WITH_CONS = 0
CLASS_WITH_PROS = 1
CLASS_TEXT_ONLY = 2
CLASS_ORDER = (CLASS_WITH_CONS, CLASS_WITH_PROS, CLASS_TEXT_ONLY)

INSTRUCTION = """Ты — аналитик отзывов маркетплейса Wildberries.

КОНТЕКСТ
Ниже отзывы покупателей по трём товарам одного магазина. По каждому товару
приведены два блока: статистика по ВСЕМ его отзывам (количество, средняя оценка,
распределение оценок 1-5) и выборка содержательных отзывов - тех, где покупатель
написал текст, плюсы или минусы.

ЗАДАЧА
По каждому товару отдельно дай сводку:
1) что хвалят - темы и насколько они массовые;
2) на что жалуются - темы и насколько они массовые;
3) что можно улучшить - конкретно, на основе жалоб;
4) о чём говорят чаще всего.

ПРАВИЛА
- Опирайся только на приведённые отзывы и статистику. Ничего не выдумывай и не
  дополняй общими знаниями о категории.
- Выборка смещена способом отбора: первыми в неё попадают отзывы с минусами,
  затем с плюсами, затем только с текстом - пока хватает места под символы.
  Число упоминаний темы в выборке не равно её частоте среди всех отзывов товара,
  поэтому масштаб тем калибруй по распределению оценок в срезе и по общей
  статистике товара, которая приведена выше среза.
- Темы с 1-2 упоминаниями в срезе помечай как единичные.
- Пиши по-русски.

ФОРМАТ
Для каждого товара - отдельный раздел с четырьмя подразделами по номерам из
ЗАДАЧИ. В конце - сравнение трёх товаров в 2-3 предложениях.
"""


def truncate_field(value: str) -> str:
    """Поле в одну строку: переводы строк и табы сворачиваются в пробел.

    Покупатели разрывают текст на строки, а промпт обещает один отзыв на
    строку — без сворачивания отзыва расползались бы на несколько строк.
    Пустое или null-поле становится пустой строкой, длинное — усечённым.
    """
    text = " ".join((value or "").split())
    if len(text) <= MAX_FIELD_LEN:
        return text
    return text[: MAX_FIELD_LEN - len(TRUNCATION_MARK)].rstrip() + TRUNCATION_MARK


def review_class(text: str, pros: str, cons: str) -> int | None:
    """Класс содержательности отзыва, None если отзыв не содержательный."""
    if cons:
        return CLASS_WITH_CONS
    if pros:
        return CLASS_WITH_PROS
    if text:
        return CLASS_TEXT_ONLY
    return None


def weight_of(row: dict[str, Any]) -> int:
    """Вес отзыва в бюджете — сумма длин его непустых усечённых полей."""
    return len(row["text"]) + len(row["pros"]) + len(row["cons"])


def prepare_rows(raw_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """CSV-строки → подготовленные содержательные отзывы с классом и весом."""
    prepared: list[dict[str, Any]] = []
    for raw in raw_rows:
        row = {
            "nm_id": int(raw["nm_id"]),
            "product_name": (raw.get("product_name") or "").strip(),
            "valuation": _as_int(raw.get("valuation")),
            "text": truncate_field(raw.get("text", "")),
            "pros": truncate_field(raw.get("pros", "")),
            "cons": truncate_field(raw.get("cons", "")),
        }
        row_class = review_class(row["text"], row["pros"], row["cons"])
        if row_class is None:
            continue
        row["review_class"] = row_class
        row["weight"] = weight_of(row)
        prepared.append(row)
    return prepared


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def group_by_article(rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """Группирует по артикулу, сохраняя порядок первой встречи артикула."""
    groups: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["nm_id"], []).append(row)
    return groups


def split_budget(weight_by_article: dict[int, int]) -> dict[int, int]:
    """Делит TOTAL_BUDGET пропорционально объёму содержательных символов.

    Метод largest remainder: целые части всем, затем по одному символу тем, у
    кого дробная часть больше (при равенстве — раньше по списку). Сумма бюджетов
    всегда равна TOTAL_BUDGET.
    """
    total_weight = sum(weight_by_article.values())
    if total_weight <= 0:
        return {nm_id: 0 for nm_id in weight_by_article}

    exact = {nm_id: TOTAL_BUDGET * weight / total_weight for nm_id, weight in weight_by_article.items()}
    budgets = {nm_id: int(exact[nm_id]) for nm_id in exact}
    leftover = TOTAL_BUDGET - sum(budgets.values())

    by_remainder = sorted(exact, key=lambda nm_id: exact[nm_id] - budgets[nm_id], reverse=True)
    for nm_id in by_remainder[:leftover]:
        budgets[nm_id] += 1
    return budgets


def select_rows(rows: list[dict[str, Any]], nm_id: int, budget: int) -> list[dict[str, Any]]:
    """Отбор отзывов артикула в бюджет: по классам от жалоб к простому тексту.

    Внутри класса порядок тасуется закрепённым генератором, в бюджет берётся
    целиком либо не берётся вовсе — строку не режем посередине.
    """
    remaining = budget
    chosen: list[dict[str, Any]] = []

    for class_id in CLASS_ORDER:
        candidates = [row for row in rows if row["review_class"] == class_id]
        random.Random(SEED + nm_id * 10 + class_id).shuffle(candidates)
        for row in candidates:
            if row["weight"] <= remaining:
                chosen.append(row)
                remaining -= row["weight"]

    return chosen


def most_common_name(rows: list[dict[str, Any]]) -> str:
    """Название товара: самое частое, если в выгрузке они различаются."""
    names = Counter(row["product_name"] for row in rows if row["product_name"])
    if not names:
        return "название не указано"
    top = max(names.values())
    return sorted(name for name, count in names.items() if count == top)[0]


def format_distribution(counts: Counter) -> str:
    return ", ".join(f"{mark} - {counts.get(mark, 0)}" for mark in range(1, 6))


def format_review(row: dict[str, Any]) -> str:
    """Строка отзыва для промпта: оценка, затем непустые поля через разделитель."""
    head = f"[{row['valuation']}/5] " if row["valuation"] is not None else ""
    parts = []
    if row["pros"]:
        parts.append(f"+: {row['pros']}")
    if row["cons"]:
        parts.append(f"-: {row['cons']}")
    if row["text"]:
        parts.append(f"текст: {row['text']}")
    return f"{head}{' | '.join(parts)}"


def build_article_section(
    position: int,
    article_count: int,
    nm_id: int,
    all_rows: list[dict[str, Any]],
    chosen: list[dict[str, Any]],
    full_stats: dict[str, Any],
) -> str:
    """Секция одного товара: шапка, статистика, статистика среза, отзывы."""
    chosen_by_class = Counter(row["review_class"] for row in chosen)
    slice_ratings = Counter(row["valuation"] for row in chosen if row["valuation"] is not None)
    full_distribution = {int(k): int(v) for k, v in full_stats.get("valuation_distribution", {}).items()}

    lines = [
        f"### Товар {position} из {article_count}: артикул {nm_id}",
        f"Название: {most_common_name(all_rows)}",
        (
            f"Статистика по всем отзывам: всего {full_stats.get('total', len(all_rows))}, "
            f"средняя оценка {full_stats.get('valuation_average')}, распределение оценок: "
            f"{format_distribution(Counter(full_distribution))}"
        ),
        (
            f"Срез содержательных отзывов: включено {len(chosen)} из {len(all_rows)}; "
            f"в срезе с минусами {chosen_by_class.get(CLASS_WITH_CONS, 0)} / "
            f"с плюсами {chosen_by_class.get(CLASS_WITH_PROS, 0)} / "
            f"только текст {chosen_by_class.get(CLASS_TEXT_ONLY, 0)}; "
            f"распределение оценок в срезе: {format_distribution(slice_ratings)}"
        ),
        "Отзывы:",
    ]
    lines.extend(format_review(row) for row in chosen)
    return "\n".join(lines)


def build_prompt(
    groups: dict[int, list[dict[str, Any]]],
    selections: dict[int, list[dict[str, Any]]],
    full_stats: dict[str, dict[str, Any]],
) -> str:
    """Полный текст промпта: инструкция, секции товаров, служебная строка."""
    sections = [INSTRUCTION.rstrip()]
    articles = list(groups)

    for position, nm_id in enumerate(articles, start=1):
        sections.append(
            build_article_section(
                position,
                len(articles),
                nm_id,
                groups[nm_id],
                selections[nm_id],
                full_stats.get(str(nm_id), {}),
            )
        )

    used = sum(row["weight"] for chosen in selections.values() for row in chosen)
    sections.append(
        "---\n"
        f"Служебное: символов отзывов в промпте {used} из бюджета {TOTAL_BUDGET} "
        f"({round(100 * used / TOTAL_BUDGET, 1)}%); отзывы выше отобраны со смещением "
        "в пользу жалоб, статистика над срезом — по всем отзывам товара."
    )
    return "\n\n".join(sections) + "\n"


def load_full_stats(path: Path) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle).get("articles", {})


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    rows = prepare_rows(load_csv_rows(REVIEWS_CSV_PATH))
    if not rows:
        raise SystemExit("В reviews.csv нет ни одного содержательного отзыва — промпт пустой")

    groups = group_by_article(rows)
    budgets = split_budget(
        {nm_id: sum(row["weight"] for row in article_rows) for nm_id, article_rows in groups.items()}
    )
    selections = {
        nm_id: select_rows(article_rows, nm_id, budgets[nm_id])
        for nm_id, article_rows in groups.items()
    }

    prompt = build_prompt(groups, selections, load_full_stats(STATS_JSON_PATH))
    PROMPT_PATH.write_text(prompt, encoding="utf-8", newline="\n")

    used_total = 0
    meaningful_total = 0
    for nm_id, article_rows in groups.items():
        chosen = selections[nm_id]
        used = sum(row["weight"] for row in chosen)
        used_total += used
        meaningful_total += len(article_rows)
        by_class = Counter(row["review_class"] for row in chosen)
        logger.info(
            "Артикул %s: содержательных %d, включено %d (%.1f%%), символов %d из бюджета %d"
            " (с минусами %d, с плюсами %d, только текст %d)",
            nm_id,
            len(article_rows),
            len(chosen),
            100 * len(chosen) / len(article_rows),
            used,
            budgets[nm_id],
            by_class.get(CLASS_WITH_CONS, 0),
            by_class.get(CLASS_WITH_PROS, 0),
            by_class.get(CLASS_TEXT_ONLY, 0),
        )

    logger.info(
        "Итог: файл %s — %d символов, символов отзывов %d из %d, покрытие среза %.1f%% "
        "(%d из %d содержательных)",
        PROMPT_PATH,
        len(prompt),
        used_total,
        TOTAL_BUDGET,
        100 * sum(len(chosen) for chosen in selections.values()) / meaningful_total,
        sum(len(chosen) for chosen in selections.values()),
        meaningful_total,
    )


if __name__ == "__main__":
    main()
