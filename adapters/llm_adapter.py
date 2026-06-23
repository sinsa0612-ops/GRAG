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


# 프롬프트를 Gemini에 전달하고 응답 텍스트를 반환한다.
# response_schema가 주어지면 JSON 모드로 호출해 스키마에 맞는 순수 JSON 문자열을 받는다(```json 펜스/잡설 없음).
# 안전필터 등으로 응답이 비면 response.text가 None이라 빈 문자열로 방어한다(호출부의 파싱이 None에서 터지지 않게).
# 일시적 오류는 설정된 횟수만큼 점진적으로 대기하며 재시도하고, 영구적 오류는 즉시 포기한다.
# 성공/실패 시도 모두 무료 한도 보호용 대기(llm_request_interval_sec)를 적용한다.
def generate(prompt: str, response_schema: type | None = None) -> str:
    client = _get_client()

    config = None
    if response_schema is not None:
        config = types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=response_schema
        )

    for attempt in range(settings.llm_max_retries + 1):
        try:
            response = client.models.generate_content(
                model=settings.llm_model_name, contents=prompt, config=config
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
            time.sleep(settings.llm_request_interval_sec)

    raise RuntimeError("도달할 수 없는 코드 경로")  # for 루프는 항상 return 또는 raise로 끝난다
