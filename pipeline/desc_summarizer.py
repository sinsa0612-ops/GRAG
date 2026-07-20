# 엔티티 설명 통합 요약(M1.5, 옵트인 배치) — 여러 문서에서 쌓인 설명 후보를 로컬 LLM(기본 Ollama)으로
# 하나의 비중복 설명으로 통합해 그래프의 description을 갱신한다. 인제스트 핫패스와 분리된 별도 배치 명령
# (`graphrag summarize-descriptions`)에서만 호출되므로, 여기서 LLM을 부르는 것이 인제스트 속도에 영향을 주지 않는다.
import logging

from adapters.llm_adapter import generate
from config import settings
from db import graph_manager, sqlite_manager

logger = logging.getLogger(__name__)

# 도메인·언어 중립 프롬프트(spec-addendum §B) — 특정 업종 어휘("사업" 등)나 특정 언어를 가정하지 않는다.
_SUMMARY_PROMPT = """\
아래는 같은 대상("{entity_name}")을 서로 다른 문서에서 설명한 후보 설명들이다.
이 후보들을 종합해 중복 없이 하나의 자연스러운 문단으로 통합해줘.
서로 다른 정보는 모두 보존하고, 겹치는 내용은 한 번만 남겨.
통합한 설명 문단만 출력하고, 다른 설명이나 머리말은 붙이지 마.

후보 설명들:
{candidates}
"""


# 후보 설명 목록을 프롬프트에 넣을 번호 매긴 목록 문자열로 만든다.
def _build_prompt(entity_name: str, candidates: list[str]) -> str:
    numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(candidates, start=1))
    return _SUMMARY_PROMPT.format(entity_name=entity_name, candidates=numbered)


# 한 컬렉션에서 설명 후보가 min_candidates개 이상 쌓인 엔티티만 통합 요약해 description을 갱신한다.
# 후보가 그 미만(기본 1개)인 엔티티는 통합할 게 없으므로 LLM 호출 자체를 하지 않고 건너뛴다(호출 절약).
# backend 기본은 "ollama"(무료·배치용, spec §A 라우팅 정책). 요약 실패/빈 응답은 그 엔티티만 건너뛰고
# 기존 description을 그대로 둔다(다른 엔티티 처리를 막지 않음). 반환값 = 실제로 갱신한 엔티티 수.
def summarize_descriptions(
    collection: str,
    min_candidates: int | None = None,
    backend: str = "ollama",
    model: str | None = None,
) -> int:
    threshold = settings.desc_summary_min_candidates if min_candidates is None else min_candidates
    entity_names = sqlite_manager.get_entities_with_min_candidates(collection, threshold)

    updated = 0
    for name in entity_names:
        candidates = sqlite_manager.get_desc_candidates(collection, name)
        try:
            summary = generate(_build_prompt(name, candidates), backend=backend, model=model).strip()
        except Exception as exc:
            logger.warning("[%s] '%s' 설명 통합 요약 실패, 건너뜀: %s", collection, name, exc)
            continue
        if not summary:
            logger.warning("[%s] '%s' 설명 통합 요약이 빈 응답, 건너뜀", collection, name)
            continue
        graph_manager.update_entity_description(collection, name, summary)
        updated += 1
        logger.info("[%s] '%s' 설명 통합 완료 (후보 %d개)", collection, name, len(candidates))
    return updated
