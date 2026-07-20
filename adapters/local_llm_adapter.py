# Ollama(로컬 LLM) HTTP API를 격리하는 어댑터 — 다른 모듈은 requests/Ollama의 존재를 모른다.
import logging

import requests

from config import settings

logger = logging.getLogger(__name__)


# Ollama 연결 실패·타임아웃·비정상 응답을 감싸는 전용 예외.
# fail-closed: 원인을 메시지에 명확히 담아 즉시 중단시키고, 호출부가 조용히 다른 백엔드로 새지 않게 한다.
# (기본 Gemini 경로는 이 백엔드를 아예 호출하지 않으므로, 여기서 실패해도 인제스트/로컬 질의는 영향 없다.)
class LocalLLMError(RuntimeError):
    pass


# 프롬프트를 Ollama 로컬 모델에 전달하고 응답 텍스트를 반환한다.
# model을 주지 않으면 설정 기본 모델(qwen3:14b)을 쓴다. Ollama는 로컬·무료·무제한이라 Gemini 어댑터의
# 요청한도 보호용 재시도/대기 로직이 필요 없다. 연결 실패·타임아웃·빈 응답은 모두 LocalLLMError로
# 감싸 원인을 명확히 알린다(response_schema는 구조화 출력 강제가 불가해 지원하지 않음 — 호출부가
# 기존 Gemma 대응과 동형으로 원문을 방어적 파싱한다).
def generate(prompt: str, model: str | None = None) -> str:
    model = model or settings.ollama_model_name
    url = f"{settings.ollama_base_url}/api/generate"
    # think=False: qwen3 계열의 추론(<think> 체인)을 꺼 추출·요약을 빠르고 깨끗한 직답으로 받는다.
    # (추론이 켜지면 추출처럼 긴 프롬프트는 청크당 120초 타임아웃 — 이 파이프라인은 로컬 모델의
    #  사고연쇄가 불필요하고, <think> 태그가 방어적 JSON 파서를 오염시킬 수도 있다.)
    payload = {"model": model, "prompt": prompt, "stream": False, "think": False}

    try:
        response = requests.post(url, json=payload, timeout=settings.ollama_request_timeout_sec)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.error("Ollama 호출 실패(fail-closed): %s", exc)
        raise LocalLLMError(
            f"Ollama({url}) 연결 실패 — 'ollama serve'가 실행 중인지, 모델 '{model}'이 "
            f"받아져 있는지 확인하라: {exc}"
        ) from exc

    data = response.json()
    text = data.get("response")
    if not text:
        logger.error("Ollama 응답에 'response' 필드 없음(fail-closed): %r", data)
        raise LocalLLMError(f"Ollama 응답에 유효한 'response' 필드가 없음: {data!r}")
    return text
