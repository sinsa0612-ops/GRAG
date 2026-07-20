# local_llm_adapter.generate(Ollama HTTP 어댑터)를 requests.post mock으로 검증한다.
# 네트워크 없이 실행되며, 실 Ollama 통합 확인은 별도 skipif 테스트(아래 맨 끝)로 격리한다.
import pytest
import requests

import adapters.local_llm_adapter as local_llm_adapter
from config import settings


# requests.Response를 흉내내는 가짜 응답 객체.
class _FakeResponse:
    def __init__(self, json_data: dict, status_code: int = 200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_data


def test_generate_returns_response_text_on_success(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse({"response": "안녕하세요"})

    monkeypatch.setattr(local_llm_adapter.requests, "post", fake_post)

    result = local_llm_adapter.generate("질문")

    assert result == "안녕하세요"
    assert captured["url"] == f"{settings.ollama_base_url}/api/generate"
    assert captured["json"]["model"] == settings.ollama_model_name
    assert captured["json"]["prompt"] == "질문"
    assert captured["json"]["stream"] is False
    # 추론(<think>) 비활성화 — 추출/요약 같은 긴 프롬프트가 청크당 120초 타임아웃 나던 근본 원인 차단(회귀 방지).
    assert captured["json"]["think"] is False


def test_generate_uses_explicit_model_override(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["model"] = json["model"]
        return _FakeResponse({"response": "결과"})

    monkeypatch.setattr(local_llm_adapter.requests, "post", fake_post)

    local_llm_adapter.generate("질문", model="다른모델:7b")

    assert captured["model"] == "다른모델:7b"


def test_generate_raises_local_llm_error_on_connection_failure(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        raise requests.exceptions.ConnectionError("연결 거부")

    monkeypatch.setattr(local_llm_adapter.requests, "post", fake_post)

    with pytest.raises(local_llm_adapter.LocalLLMError):
        local_llm_adapter.generate("질문")


def test_generate_raises_local_llm_error_on_http_error_status(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        return _FakeResponse({}, status_code=500)

    monkeypatch.setattr(local_llm_adapter.requests, "post", fake_post)

    with pytest.raises(local_llm_adapter.LocalLLMError):
        local_llm_adapter.generate("질문")


def test_generate_raises_local_llm_error_on_missing_response_field(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        return _FakeResponse({"unexpected": "형식"})

    monkeypatch.setattr(local_llm_adapter.requests, "post", fake_post)

    with pytest.raises(local_llm_adapter.LocalLLMError):
        local_llm_adapter.generate("질문")


# 실 Ollama 서버가 로컬에 떠 있을 때만 도는 통합 테스트 — 없으면 스킵(네트워크 없이 CI 통과 원칙 준수).
def _ollama_reachable() -> bool:
    try:
        requests.get(f"{settings.ollama_base_url}/api/tags", timeout=1.0)
        return True
    except requests.exceptions.RequestException:
        return False


@pytest.mark.skipif(not _ollama_reachable(), reason="Ollama가 로컬에서 응답하지 않음(미기동)")
def test_generate_against_real_ollama_smoke():
    result = local_llm_adapter.generate("1+1은 몇이야? 숫자만 답해.")
    assert isinstance(result, str)
    assert len(result) > 0
