# llm_adapter.generate의 재시도 로직(일시적 오류만 재시도, 영구적 오류는 즉시 포기)을 검증한다.
import time

import pytest
from google.genai import errors as genai_errors

import adapters.llm_adapter as llm_adapter
from config import settings


# 테스트용 가짜 응답 객체 (실제 google-genai 응답의 .text 속성만 흉내냄).
class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


# 호출마다 미리 정해둔 결과(예외 또는 텍스트)를 순서대로 돌려주는 가짜 모델 클라이언트.
class _FakeModels:
    def __init__(self, side_effects: list):
        self._side_effects = list(side_effects)
        self.call_count = 0

    def generate_content(self, model, contents, config=None):
        self.call_count += 1
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return _FakeResponse(effect)


class _FakeClient:
    def __init__(self, side_effects: list):
        self.models = _FakeModels(side_effects)


def _server_error(code: int = 503) -> genai_errors.ServerError:
    return genai_errors.ServerError(code, {"error": {"code": code, "message": "과부하", "status": "UNAVAILABLE"}})


def _client_error(code: int) -> genai_errors.ClientError:
    return genai_errors.ClientError(code, {"error": {"code": code, "message": "오류", "status": "ERROR"}})


def _patch_common(monkeypatch, fake_client):
    monkeypatch.setattr(llm_adapter, "_get_client", lambda: fake_client)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    monkeypatch.setattr(settings, "llm_max_retries", 2)
    monkeypatch.setattr(settings, "llm_retry_backoff_sec", 0.01)
    monkeypatch.setattr(settings, "llm_request_interval_sec", 0.0)


def test_generate_retries_on_server_error_then_succeeds(monkeypatch):
    fake_client = _FakeClient([_server_error(), "성공 응답"])
    _patch_common(monkeypatch, fake_client)

    result = llm_adapter.generate("프롬프트")

    assert result == "성공 응답"
    assert fake_client.models.call_count == 2


def test_generate_retries_on_429_rate_limit_then_succeeds(monkeypatch):
    fake_client = _FakeClient([_client_error(429), "성공 응답"])
    _patch_common(monkeypatch, fake_client)

    result = llm_adapter.generate("프롬프트")

    assert result == "성공 응답"
    assert fake_client.models.call_count == 2


def test_generate_does_not_retry_on_bad_request(monkeypatch):
    fake_client = _FakeClient([_client_error(400)])
    _patch_common(monkeypatch, fake_client)

    with pytest.raises(genai_errors.ClientError):
        llm_adapter.generate("프롬프트")

    assert fake_client.models.call_count == 1


def test_generate_gives_up_after_max_retries(monkeypatch):
    fake_client = _FakeClient([_server_error(), _server_error(), _server_error()])
    _patch_common(monkeypatch, fake_client)

    with pytest.raises(genai_errors.ServerError):
        llm_adapter.generate("프롬프트")

    assert fake_client.models.call_count == 3
