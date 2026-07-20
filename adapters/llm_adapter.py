# LLM 백엔드 얇은 라우터 — backend 문자열로 각 어댑터에 디스패치만 한다(SPEC §1.3).
# Gemini(기본) 경로는 이 파일 안에 그대로 있고(다른 모듈은 google-genai의 존재를 모른다),
# ollama/claude_cli/codex_cli는 각자의 전용 어댑터 모듈(local_llm_adapter/cli_llm_adapter)이 격리한다.
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


# 프롬프트를 지정된 backend로 전달하고 응답 텍스트를 반환한다.
# backend=None(기본)/"gemini" -> 아래의 기존 Gemini/Gemma 경로 그대로(바이트 동일, hot-path 불변).
# backend="ollama" -> local_llm_adapter(Ollama HTTP)로 위임. backend="claude_cli"/"codex_cli" ->
# cli_llm_adapter(subprocess File-IPC)로 위임. 두 신규 백엔드 모두 response_schema를 지원하지 않으므로
# (구조화 출력 강제 불가) 무시하고, 호출부가 기존 Gemma 대응과 동형으로 원문을 방어적 파싱한다.
# 어댑터 모듈은 실제로 그 backend가 쓰일 때만 import한다(불필요한 의존 로드를 피하고, 한 백엔드의
# import 실패가 다른 백엔드/기본 경로에 영향을 주지 않게 한다).
def generate(
    prompt: str,
    response_schema: type | None = None,
    model: str | None = None,
    backend: str | None = None,
) -> str:
    if backend == "ollama":
        from adapters.local_llm_adapter import generate as _ollama_generate

        return _ollama_generate(prompt, model=model)
    if backend in ("claude_cli", "codex_cli"):
        from adapters.cli_llm_adapter import generate as _cli_generate

        return _cli_generate(prompt, backend=backend, model=model)
    if backend not in (None, "gemini"):
        raise ValueError(f"알 수 없는 backend: {backend!r}")

    # --- 이하 기존 Gemini/Gemma 경로 (수정 없음, backend 미지정 시 100% 동일 동작) ---
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
