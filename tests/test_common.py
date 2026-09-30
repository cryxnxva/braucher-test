"""Unit-тесты retry-логики `common.wb_request`.

Реальных HTTP-запросов нет: транспорт и `time.sleep` подменены, поэтому тесты
проходят мгновенно, несмотря на паузы между попытками.
"""

import pytest
import requests

import common

FAKE_TOKEN = "test-token"


def _make_response(
    status: int,
    headers: dict[str, str] | None = None,
    text: str = "",
) -> requests.Response:
    """Ответ без сети: статус, заголовки и тело задаются напрямую."""
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response._content = text.encode("utf-8")
    return response


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[requests.Response],
) -> tuple[list[dict], list[int]]:
    """Подменяет транспорт и sleep, возвращает журналы вызовов и пауз.

    Последний ответ из списка повторяется, если попыток больше, чем ответов.
    """
    calls: list[dict] = []
    sleeps: list[int] = []

    def fake_request(method: str, url: str, **kwargs: object) -> requests.Response:
        calls.append({"method": method, "url": url, **kwargs})
        return responses[min(len(calls) - 1, len(responses) - 1)]

    monkeypatch.setattr(requests, "request", fake_request)
    monkeypatch.setattr(common.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(common, "WB_TOKEN", FAKE_TOKEN)
    return calls, sleeps


def test_429_is_retried_and_second_attempt_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, sleeps = _install_fakes(
        monkeypatch, [_make_response(429), _make_response(200, text="{}")]
    )

    response = common.wb_request("GET", "https://feedbacks.test/v1/feedbacks")

    assert response.status_code == 200
    assert len(calls) == 2
    assert sleeps == [1]


def test_server_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, sleeps = _install_fakes(
        monkeypatch, [_make_response(500), _make_response(503), _make_response(200)]
    )

    response = common.wb_request("GET", "https://analytics.test/history")

    assert response.status_code == 200
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_retry_after_header_controls_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    _, sleeps = _install_fakes(
        monkeypatch,
        [_make_response(429, headers={"Retry-After": "7"}), _make_response(200)],
    )

    response = common.wb_request("POST", "https://analytics.test/history", body={"nmId": 1})

    assert response.status_code == 200
    assert sleeps == [7]


def test_non_integer_retry_after_falls_back_to_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_date = "Wed, 21 Oct 2026 07:28:00 GMT"
    _, sleeps = _install_fakes(
        monkeypatch,
        [_make_response(429, headers={"Retry-After": http_date}), _make_response(200)],
    )

    common.wb_request("GET", "https://feedbacks.test/v1/feedbacks")

    assert sleeps == [1]


def test_backoff_is_exponential_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    _, sleeps = _install_fakes(monkeypatch, [_make_response(429)])

    with pytest.raises(RuntimeError):
        common.wb_request("GET", "https://feedbacks.test/v1/feedbacks", max_retries=7)

    assert sleeps == [1, 2, 4, 8, 16, 32, 60]


def test_retries_exhausted_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, sleeps = _install_fakes(monkeypatch, [_make_response(429)])

    with pytest.raises(RuntimeError, match="попыток"):
        common.wb_request("GET", "https://feedbacks.test/v1/feedbacks", max_retries=2)

    assert len(calls) == 2
    assert sleeps == [1, 2]


def test_other_client_error_raises_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, sleeps = _install_fakes(
        monkeypatch, [_make_response(401, text='{"error":"unauthorized"}')]
    )

    with pytest.raises(RuntimeError) as excinfo:
        common.wb_request("GET", "https://feedbacks.test/v1/feedbacks")

    assert "401" in str(excinfo.value)
    assert "unauthorized" in str(excinfo.value)
    assert len(calls) == 1
    assert sleeps == []


def test_error_body_is_truncated_to_500_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch, [_make_response(403, text="x" * 900)])

    with pytest.raises(RuntimeError) as excinfo:
        common.wb_request("GET", "https://feedbacks.test/v1/feedbacks")

    assert len(str(excinfo.value)) < 700


def test_network_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = _make_response(200)
    attempts: list[str] = []

    def flaky_request(method: str, url: str, **kwargs: object) -> requests.Response:
        attempts.append(method)
        if len(attempts) == 1:
            raise requests.ConnectionError("connection reset by peer")
        return ok

    sleeps: list[int] = []
    monkeypatch.setattr(requests, "request", flaky_request)
    monkeypatch.setattr(common.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(common, "WB_TOKEN", FAKE_TOKEN)

    assert common.wb_request("GET", "https://feedbacks.test/v1/feedbacks") is ok
    assert sleeps == [1]


def test_missing_token_raises_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, _ = _install_fakes(monkeypatch, [_make_response(200)])
    monkeypatch.setattr(common, "WB_TOKEN", None)

    with pytest.raises(RuntimeError, match="WB_TOKEN"):
        common.wb_request("GET", "https://feedbacks.test/v1/feedbacks")

    assert calls == []


def test_authorization_header_sends_token_without_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, _ = _install_fakes(monkeypatch, [_make_response(200)])

    common.wb_request("GET", "https://feedbacks.test/v1/feedbacks", params={"take": 5})

    assert calls[0]["headers"] == {"Authorization": FAKE_TOKEN}
    assert "Bearer" not in calls[0]["headers"]["Authorization"]
    assert calls[0]["timeout"] == 30
    assert calls[0]["params"] == {"take": 5}


def test_wb_post_passes_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, _ = _install_fakes(monkeypatch, [_make_response(200)])

    common.wb_post("https://analytics.test/history", body={"period": {"offset": 7}})

    assert calls[0]["method"] == "POST"
    assert calls[0]["json"] == {"period": {"offset": 7}}
