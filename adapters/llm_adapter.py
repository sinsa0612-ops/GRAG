# Gemini API 호출을 격리하는 어댑터 — 다른 모듈은 google-genai의 존재를 모른다.
import logging
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from config import settings

logger = logging.getLogger(__name__)

_client: genai.Client | None = None


# Gemini 클라이언트를 1회만 생성해 재사용한다.
def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


# 일시적 오류(서버 과부하 5xx, 429 요청한도초과)인지 판단한다.
# 그 외 4xx(잘못된 요청, 인증 오류 등)는 재시도해도 똑같이 실패하므로 재시도 대상이 아니다.
def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, genai_errors.ClientError):
        return exc.code == 429
    return True  # APIError가 아닌 네트워크 레벨 예외는 일시적일 가능성이 높음


# 모델이 Gemini의 구조화 출력(response_schema/JSON 모드)을 지원하는지 판단한다.
# Gemma 계열은 Gemini API에서 구조화 출력을 지원하지 않으므로(스키마를 넘기면 400), 스키마를 빼고
# 프롬프트 지시로만 순수 JSON을 받아 호출부가 직접 파싱한다.
def _supports_structured_output(model: str) -> bool:
    return "gemma" not in model.lower()


# 모델별 호출 간격(초)을 고른다. RPM이 더 낮은 Gemma는 더 길게 쉬어 분당 한도를 보호한다.
def _request_interval(model: str) -> float:
    if "gemma" in model.lower():
        return settings.gemma_request_interval_sec
    return settings.llm_request_interval_sec


# 프롬프트를 Gemini/Gemma에 전달하고 응답 텍스트를 반환한다.
# model을 주면 그 모델로(없으면 설정 기본 모델 llm_model_name), response_schema가 주어지고 그 모델이
# 구조화 출력을 지원할 때만 JSON 모드로 호출한다(미지원 모델은 프롬프트 기반 JSON으로 받아 호출부가 파싱).
# 안전필터 등으로 응답이 비면 response.text가 None이라 빈 문자열로 방어한다(호출부의 파싱이 None에서 터지지 않게).
# 일시적 오류는 설정된 횟수만큼 점진적으로 대기하며 재시도하고, 영구적 오류는 즉시 포기한다.
# 성공/실패 시도 모두 무료 한도 보호용 대기(모델별 간격)를 적용한다.
def generate(prompt: str, response_schema: type | None = None, model: str | None = None) -> str:
    client = _get_client()
    model = model or settings.llm_model_name

    config = None
    if response_schema is not None and _supports_structured_output(model):
        config = types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=response_schema
        )
    interval = _request_interval(model)

    for attempt in range(settings.llm_max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model, contents=prompt, config=config
            )
            return response.text or ""
        except Exception as exc:
            if attempt >= settings.llm_max_retries or not _is_retryable(exc):
                raise
            backoff = settings.llm_retry_backoff_sec * (attempt + 1)
            logger.warning(
                "LLM 호출 일시 실패(%d번째 시도), %.0f초 후 재시도: %s", attempt + 1, backoff, exc
            )
            time.sleep(backoff)
        finally:
            time.sleep(interval)

    raise RuntimeError("도달할 수 없는 코드 경로")  # for 루프는 항상 return 또는 raise로 끝난다
