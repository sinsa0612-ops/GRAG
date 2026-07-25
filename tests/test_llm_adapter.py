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


# --- backend 라우팅 테스트 (SPEC §1.3 / addendum §D) ---


# backend를 아예 안 주면(기존 호출부와 동일) 새 백엔드는 전혀 건드리지 않고 기존 Gemini 경로가
# 정확히 그대로 호출됨을 증명한다 — hot-path 불변식의 핵심 증거.
def test_generate_without_backend_calls_gemini_path_unchanged(monkeypatch):
    fake_client = _FakeClient(["성공 응답"])
    _patch_common(monkeypatch, fake_client)

    result = llm_adapter.generate("프롬프트")

    assert result == "성공 응답"
    assert fake_client.models.call_count == 1


# backend="gemini"를 명시해도 backend=None과 완전히 동일하게 동작해야 한다.
def test_generate_with_explicit_gemini_backend_same_as_default(monkeypatch):
    fake_client = _FakeClient(["성공 응답"])
    _patch_common(monkeypatch, fake_client)

    result = llm_adapter.generate("프롬프트", backend="gemini")

    assert result == "성공 응답"
    assert fake_client.models.call_count == 1


# backend="ollama"는 local_llm_adapter.generate로 위임되고, Gemini 클라이언트는 전혀 생성되지 않는다.
def test_generate_routes_ollama_backend_to_local_llm_adapter(monkeypatch):
    import adapters.local_llm_adapter as local_llm_adapter

    calls = {}

    def fake_ollama_generate(prompt, model=None, temperature=None, format_json=False):
        calls["prompt"] = prompt
        calls["model"] = model
        return "ollama 응답"

    monkeypatch.setattr(local_llm_adapter, "generate", fake_ollama_generate)
    # Gemini 클라이언트가 실수로라도 생성되면 곧바로 실패하도록 예외를 심어둔다.
    monkeypatch.setattr(
        llm_adapter,
        "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("Gemini 클라이언트가 호출되면 안 됨")),
    )

    result = llm_adapter.generate("프롬프트", model="qwen3:14b", backend="ollama")

    assert result == "ollama 응답"
    assert calls == {"prompt": "프롬프트", "model": "qwen3:14b"}


# backend="claude_cli"는 cli_llm_adapter.generate로 위임되고, Gemini 클라이언트는 생성되지 않는다.
def test_generate_routes_claude_cli_backend_to_cli_llm_adapter(monkeypatch):
    import adapters.cli_llm_adapter as cli_llm_adapter

    calls = {}

    def fake_cli_generate(prompt, backend, model=None):
        calls["prompt"] = prompt
        calls["backend"] = backend
        return "claude_cli 응답"

    monkeypatch.setattr(cli_llm_adapter, "generate", fake_cli_generate)
    monkeypatch.setattr(
        llm_adapter,
        "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("Gemini 클라이언트가 호출되면 안 됨")),
    )

    result = llm_adapter.generate("프롬프트", backend="claude_cli")

    assert result == "claude_cli 응답"
    assert calls == {"prompt": "프롬프트", "backend": "claude_cli"}


# 알 수 없는 backend 문자열은 ValueError로 즉시 거부한다(조용히 Gemini로 새지 않음).
def test_generate_rejects_unknown_backend(monkeypatch):
    monkeypatch.setattr(
        llm_adapter,
        "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("Gemini 클라이언트가 호출되면 안 됨")),
    )

    with pytest.raises(ValueError):
        llm_adapter.generate("프롬프트", backend="does_not_exist")
