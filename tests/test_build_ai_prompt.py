"""Unit-тесты генератора промпта. Только синтетические данные, без сети и API."""

from pathlib import Path
from typing import Any

import pytest

import build_ai_prompt as builder

FIXTURE_CSV = Path(__file__).parent / "fixtures" / "sample_reviews.csv"
ALL_FIXTURE_IDS = tuple(f"F{index}" for index in range(1, 11))


def _prepared() -> list[dict[str, Any]]:
    return builder.prepare_rows(builder.load_csv_rows(FIXTURE_CSV))


@pytest.fixture
def groups() -> dict[int, list[dict[str, Any]]]:
    return builder.group_by_article(_prepared())


def test_empty_review_is_dropped(groups: dict[int, list[dict[str, Any]]]) -> None:
    """Из 10 строк фикстуры содержательных 9: одна без текста, плюсов и минусов."""
    assert sum(len(rows) for rows in groups.values()) == 9


def test_small_budget_takes_complaints_before_plain_text(
    groups: dict[int, list[dict[str, Any]]]
) -> None:
    """Бюджет 80 покрывает классы 0 и 1 (73 символа), но не влезает ни один text-only."""
    chosen = builder.select_rows(groups[111], 111, 80)
    classes = {row["review_class"] for row in chosen}

    assert sum(row["weight"] for row in chosen) <= 80
    assert sum(1 for row in chosen if row["review_class"] == builder.CLASS_WITH_CONS) == 2
    assert sum(1 for row in chosen if row["review_class"] == builder.CLASS_WITH_PROS) == 2
    assert builder.CLASS_TEXT_ONLY not in classes


def test_truncate_field_contract() -> None:
    assert builder.truncate_field("") == ""
    assert builder.truncate_field("   ") == ""
    assert builder.truncate_field("  привет  ") == "привет"

    short = "а" * (builder.MAX_FIELD_LEN - 3)
    assert builder.truncate_field(short) == short

    cut = builder.truncate_field("а" * 400)
    assert len(cut) == builder.MAX_FIELD_LEN
    assert cut.endswith(builder.TRUNCATION_MARK)


def test_long_fields_are_truncated_with_marker() -> None:
    longest = max(_prepared(), key=lambda row: len(row["text"]))

    # срез идёт по границе слова, поэтому перед маркером мог отойти пробел
    assert longest["text"].endswith(builder.TRUNCATION_MARK)
    assert builder.MAX_FIELD_LEN - 5 <= len(longest["text"]) <= builder.MAX_FIELD_LEN
    for row in _prepared():
        for field in ("text", "pros", "cons"):
            assert len(row[field]) <= builder.MAX_FIELD_LEN


def test_multiline_review_stays_on_one_line(
    groups: dict[int, list[dict[str, Any]]]
) -> None:
    """Покупатели рвут текст на строки, а промпт обещает один отзыв на строку."""
    assert builder.truncate_field("первая\nвторая\tтретья") == "первая вторая третья"

    for rows in groups.values():
        for row in rows:
            for field in ("text", "pros", "cons"):
                assert "\n" not in row[field]
                assert "\t" not in row[field]

    chosen = {nm: builder.select_rows(rows, nm, 10_000) for nm, rows in groups.items()}
    prompt = builder.build_prompt(groups, chosen, {})
    review_lines = [line for line in prompt.splitlines() if line.startswith("[")]

    assert len(review_lines) == sum(len(rows) for rows in chosen.values())


def test_budget_split_is_exhaustive_and_proportional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builder, "TOTAL_BUDGET", 1000)

    budgets = builder.split_budget({111: 503, 222: 54})

    assert sum(budgets.values()) == 1000
    assert budgets[111] > budgets[222]
    assert budgets[111] == pytest.approx(1000 * 503 / 557, abs=1)


def test_selection_never_exceeds_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builder, "TOTAL_BUDGET", 90)
    groups = builder.group_by_article(_prepared())
    budgets = builder.split_budget(
        {nm: sum(row["weight"] for row in rows) for nm, rows in groups.items()}
    )

    assert sum(budgets.values()) == 90
    for nm, rows in groups.items():
        chosen = builder.select_rows(rows, nm, budgets[nm])
        assert sum(row["weight"] for row in chosen) <= budgets[nm]


def test_prompt_is_byte_identical_between_runs() -> None:
    """Два независимых прогона дают побайтово одинаковый текст."""

    def build_once() -> str:
        rows = builder.prepare_rows(builder.load_csv_rows(FIXTURE_CSV))
        groups = builder.group_by_article(rows)
        budgets = builder.split_budget(
            {nm: sum(row["weight"] for row in article) for nm, article in groups.items()}
        )
        selections = {
            nm: builder.select_rows(article, nm, budgets[nm]) for nm, article in groups.items()
        }
        return builder.build_prompt(groups, selections, {})

    first = build_once()

    assert first == build_once()
    assert first.encode("utf-8") == build_once().encode("utf-8")


def test_prompt_omits_feedback_ids(groups: dict[int, list[dict[str, Any]]]) -> None:
    chosen = {nm: builder.select_rows(rows, nm, 10_000) for nm, rows in groups.items()}

    prompt = builder.build_prompt(groups, chosen, {})

    for feedback_id in ALL_FIXTURE_IDS:
        assert feedback_id not in prompt


def test_prompt_sections_and_review_lines(groups: dict[int, list[dict[str, Any]]]) -> None:
    chosen = {nm: builder.select_rows(rows, nm, 10_000) for nm, rows in groups.items()}
    stats = {
        "111": {
            "total": 42,
            "valuation_average": 3.5,
            "valuation_distribution": {"1": 5, "2": 4, "3": 6, "4": 9, "5": 18},
        }
    }

    prompt = builder.build_prompt(groups, chosen, stats)

    assert prompt.startswith("Ты — аналитик отзывов маркетплейса Wildberries.")
    assert "### Товар 1 из 2: артикул 111" in prompt
    assert "### Товар 2 из 2: артикул 222" in prompt
    assert "Название: Товёрт тестовый" in prompt
    assert "Статистика по всем отзывам: всего 42, средняя оценка 3.5" in prompt
    assert "1 - 5, 2 - 4, 3 - 6, 4 - 9, 5 - 18" in prompt
    assert "Срез содержательных отзывов: включено 7 из 7" in prompt
    assert "[2/5] -: люфт ручки" in prompt
    assert "[1/5] -: греется | текст: короткий текст" in prompt
    assert "Служебное: символов отзывов в промпте" in prompt
