"""Unit-тесты нормализации отзыва. Сети не требуют — функции чистые."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

import fetch_reviews

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "feedback_samples.json"
GREETING = re.compile(r"Здравствуйте,([^!\.]+)")


@pytest.fixture(scope="module")
def fixture_feedbacks() -> list[dict]:
    """Реальные отзывы из дампа ЭТАПА 1.1, обезличенные вручную."""
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _raw_feedback(**overrides: Any) -> dict[str, Any]:
    feedback: dict[str, Any] = {
        "id": "abc123",
        "productValuation": 4,
        "createdDate": "2026-09-29T17:48:30.313Z",
        "text": "Крутит как надо",
        "pros": "Мощный",
        "cons": "Тяжёлый",
        "productDetails": {"nmId": 386248405, "productName": "Шуруповёрт 21В"},
    }
    feedback.update(overrides)
    return feedback


def test_normalize_feedback_extracts_every_column() -> None:
    row = fetch_reviews.normalize_feedback(_raw_feedback(), 386248405, "answered")

    assert row is not None
    assert sorted(row) == sorted(fetch_reviews.CSV_COLUMNS)
    assert row["feedback_id"] == "abc123"
    assert row["nm_id"] == 386248405
    assert row["product_name"] == "Шуруповёрт 21В"
    assert row["branch"] == "answered"
    assert row["valuation"] == 4
    assert row["text"] == "Крутит как надо"
    assert row["pros"] == "Мощный"
    assert row["cons"] == "Тяжёлый"


def test_normalize_feedback_converts_created_date_to_moscow() -> None:
    row = fetch_reviews.normalize_feedback(_raw_feedback(), 386248405, "answered")

    assert row is not None
    # 17:48 UTC — это 20:48 в Москве
    assert row["created_date"].startswith("2026-09-29T20:48:30")
    assert row["created_date"].endswith("+03:00")


def test_normalize_feedback_turns_nulls_into_empty_strings() -> None:
    raw = _raw_feedback(
        pros=None,
        cons=None,
        text=None,
        createdDate=None,
        productDetails=None,
        productValuation=None,
    )

    row = fetch_reviews.normalize_feedback(raw, 147866642, "unanswered")

    assert row is not None
    assert row["pros"] == ""
    assert row["cons"] == ""
    assert row["text"] == ""
    assert row["product_name"] == ""
    assert row["created_date"] == ""
    assert row["valuation"] is None


def test_normalize_feedback_keeps_empty_strings_as_empty() -> None:
    """В живом дампе `pros`/`cons` приходят пустыми строками, а не null."""
    row = fetch_reviews.normalize_feedback(
        _raw_feedback(pros="", cons="", text=""), 1138944672, "answered"
    )

    assert row is not None
    assert (row["pros"], row["cons"], row["text"]) == ("", "", "")


def test_normalize_feedback_rejects_feedback_without_id() -> None:
    assert fetch_reviews.normalize_feedback({"text": "без id"}, 1, "answered") is None


def test_real_fixture_reviews_normalize(fixture_feedbacks: list[dict]) -> None:
    assert len(fixture_feedbacks) >= 2

    for feedback in fixture_feedbacks:
        row = fetch_reviews.normalize_feedback(
            feedback, feedback["productDetails"]["nmId"], "answered"
        )

        assert row is not None
        assert sorted(row) == sorted(fetch_reviews.CSV_COLUMNS)
        assert row["feedback_id"] == feedback["id"]
        assert row["product_name"]
        assert isinstance(row["valuation"], int)
        assert row["created_date"].endswith("+03:00")


def test_fixture_is_anonymized(fixture_feedbacks: list[dict]) -> None:
    """Ссылок и имён реальных людей в фикстуре быть не должно.

    Проверяем структурно, а не списком конкретных имён: сам список имён и был
    бы утечкой в исходниках.
    """
    blob = json.dumps(fixture_feedbacks, ensure_ascii=False)

    assert "http" not in blob
    assert "www." not in blob
    assert "@" not in blob

    for feedback in fixture_feedbacks:
        assert feedback["userName"] == "Тест"
        assert feedback["productDetails"]["supplierName"] == "ИП Тест"
        assert feedback["lastOrderShkId"] == 0
        assert not feedback["photoLinks"]
        assert not feedback["video"]

        answer = feedback["answer"]
        if answer:
            greeting = GREETING.search(answer["text"])
            assert greeting is None or greeting.group(1).strip() == "Тест"
