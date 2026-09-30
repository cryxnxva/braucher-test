"""Unit-тесты разбиения промпта на части. Только синтетические данные, без сети."""

from pathlib import Path

import pytest

import split_ai_prompt as sp

LIMIT = 200
ARTICLES = (111, 222, 333)
REVIEWS_PER_ARTICLE = 10


def make_prompt() -> str:
    """Мини-промпт: инструкция, три товара по 10 отзывов, служебный блок в конце."""
    lines = [
        "Ты — аналитик отзывов. Это тестовая инструкция.",
        "",
        "ПРАВИЛА",
        "- Пиши по-русски.",
    ]
    for position, nm_id in enumerate(ARTICLES, start=1):
        lines += [
            "",
            f"### Товар {position} из 3: артикул {nm_id}",
            f"Название: товар {nm_id}",
            f"Срез: включено {REVIEWS_PER_ARTICLE} из {REVIEWS_PER_ARTICLE}",
            "Отзывы:",
        ]
        for index in range(REVIEWS_PER_ARTICLE):
            lines.append(f"[{5 - (index % 5)}/5] текст: тестовый отзыв номер {position}{index:02d}")
    lines += ["", "---", "Служебное: символов отзывов в промпте 900 из бюджета 1000"]
    return "\n".join(lines) + "\n"


def body_lines() -> list[str]:
    return sp.strip_service_block(make_prompt().splitlines())


def all_reviews() -> list[str]:
    return [line for line in body_lines() if sp.is_review_line(line)]


@pytest.fixture
def parts() -> list[list[str]]:
    return sp.build_parts(body_lines(), LIMIT)


def test_service_block_is_dropped(tmp_path: Path) -> None:
    path = tmp_path / "ai_prompt.txt"
    path.write_text(make_prompt(), encoding="utf-8", newline="\n")

    body = sp.load_body_lines(path)

    assert not any("Служебное" in line for line in body)
    assert "---" not in body
    assert body[0].startswith("Ты — аналитик отзывов")


def test_instruction_and_articles_split() -> None:
    instruction, articles = sp.split_instruction(body_lines())

    assert instruction[0].startswith("Ты — аналитик")
    assert articles[0].startswith("### Товар 1 из 3")


def test_no_review_is_lost_or_duplicated(parts: list[list[str]]) -> None:
    collected = [line for part in parts for line in part if sp.is_review_line(line)]

    assert len(all_reviews()) == 30
    assert sorted(collected) == sorted(all_reviews())
    assert len(set(collected)) == len(collected)


def test_parts_contain_only_whole_source_lines(parts: list[list[str]]) -> None:
    """Ни одна строка не появляется в частях побитой: только целые строки входа."""
    allowed = set(body_lines())

    for index, part in enumerate(parts, start=1):
        for line in part:
            if line == sp.continuation_header(index, len(parts)):
                continue
            assert line in allowed


def test_parts_overflow_limit_by_at_most_one_line(parts: list[list[str]]) -> None:
    longest = max(len(line) for line in body_lines())

    for part in parts:
        assert len("\n".join(part)) - LIMIT <= longest


def test_part_one_starts_with_instruction_and_first_article(parts: list[list[str]]) -> None:
    first = parts[0]

    assert first[0].startswith("Ты — аналитик")
    assert any(line.startswith("### Товар 1 из 3") for line in first)
    assert not any(line.startswith("### Товар 2 из 3") for line in first)


def test_continuation_headers_are_numbered_correctly(parts: list[list[str]]) -> None:
    total = len(parts)
    assert total > 1

    for index, part in enumerate(parts, start=1):
        if index == 1:
            assert not part[0].startswith("[Продолжение данных")
        else:
            assert part[0] == f"[Продолжение данных, часть {index} из {total}]"


def test_split_is_deterministic() -> None:
    body = body_lines()

    assert sp.build_parts(body, LIMIT) == sp.build_parts(body, LIMIT)


def test_integrity_guard_raises_when_review_is_lost(parts: list[list[str]]) -> None:
    assert sp.assert_reviews_preserved(body_lines(), parts) == 30

    broken = [part[:-1] for part in parts]
    with pytest.raises(RuntimeError, match="Потерялись отзывы"):
        sp.assert_reviews_preserved(body_lines(), broken)


def test_write_parts_names_and_content(parts: list[list[str]], tmp_path: Path) -> None:
    paths = sp.write_parts(parts, tmp_path)

    assert [path.name for path in paths] == [
        f"ai_prompt_part{index:02d}.txt" for index in range(1, len(parts) + 1)
    ]
    for path, part in zip(paths, parts):
        assert path.read_text(encoding="utf-8") == "\n".join(part) + "\n"


def test_delivery_plan_contains_verbatim_messages(parts: list[list[str]], tmp_path: Path) -> None:
    paths = sp.write_parts(parts, tmp_path)
    plan = sp.build_delivery_plan(parts, paths)
    total = len(parts)

    assert sp.PREAMBLE_MESSAGE in plan
    assert sp.FINAL_MESSAGE in plan
    assert sp.CONTINUATION_MESSAGE.format(k=2, n=total) in plan
    assert paths[0].name in plan
    assert f"ai_prompt_part{total:02d}.txt" in plan
    assert "ai_summary.md" in plan
    assert "Служебное: символов отзывов в промпте 900" not in plan
