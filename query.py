# 그래프(엔티티/관계) + 벡터 검색 결과만 근거로 질문에 답하는 하이브리드 질의 모듈.
# LLM의 사전 지식이 아니라, 파이프라인이 실제로 추출/저장한 정보만 사용하는지 검증하는 용도.
import logging

from adapters.llm_adapter import generate
from config import settings
from db import graph_manager, sqlite_manager, vector_manager

logger = logging.getLogger(__name__)

_ANSWER_PROMPT = """\
아래 [본문 조각]을 1차 근거로, [그래프 힌트]는 보조로만 써서 질문에 답해.

지킬 원칙:
1. [본문 조각]에 실제로 적혀 있는 내용만 사실로 단정해. 본문에 없으면 사전 지식이나 추측으로 채우지 말고 "정보가 부족하다"고 밝혀.
2. [그래프 힌트]는 추출된 요약일 뿐 원문이 아니다. 본문과 어긋나면 반드시 본문을 따르고, 그래프에만 있고 본문에 근거가 없는 내용은 사실로 단정하지 마(불확실하면 빼거나 "그래프상"이라고 표시).
3. 질문에 충실하고 빠짐없이 답하되, 없는 내용을 지어내지 마.

[본문 조각]
{vector_context}

[그래프 힌트]
{graph_context}

질문: {question}
"""


# 한 엔티티의 양방향 관계를 컨텍스트 줄에 덧붙인다 (본인 항목과 브릿지된 쌍에 공통으로 쓴다).
def _append_relations(lines: list[str], collection: str, name: str) -> None:
    for r in graph_manager.get_outgoing_relations(collection, name):
        lines.append(f"  - {name} -[{r['predicate']}]-> {r['target']}")
    for r in graph_manager.get_incoming_relations(collection, name):
        lines.append(f"  - {r['source']} -[{r['predicate']}]-> {name}")


# 질문(또는 벡터로 찾은 본문 조각)에 그래프의 기존 엔티티 이름이 등장하면, 그 엔티티의 설명과 양방향 관계를 모은다.
# collections로 범위를 지정하면 그 사업(들)만, None이면 전체 컬렉션을 가로질러 모은다(행정 종합).
# extra_text: 벡터 검색으로 찾은 본문 조각. 질문에 이름이 안 적혀도 관련 본문에 등장한 엔티티를 그래프로 끌어와,
#   풍부하게 추출된 그래프가 실제 답변에 도달하게 한다(벡터→그래프 브릿지). 질문 직접 매칭을 항상 우선한다.
# 교차 인사이트: 매칭 엔티티가 SAME_AS 브릿지로 다른 사업의 같은 대상과 연결돼 있으면(명시적 연결만),
# '몇 개 사업에 걸쳐 있는지'와 그쪽 사업에서의 관계까지 함께 보여준다. 단순 동명이인은 섞지 않는다.
def _gather_graph_context(
    question: str, collections: list[str] | None = None, extra_text: str = ""
) -> str:
    # 같은 이름이 컬렉션마다 따로 있을 수 있으므로 (collection, name) 단위로 매칭한다.
    # 질문에 직접 등장한 엔티티를 우선하고, 본문 조각(extra_text)에만 등장한 엔티티는 그 뒤에 보강한다.
    in_question: list[tuple[str, str]] = []
    in_chunks: list[tuple[str, str]] = []
    for e in graph_manager.get_all_entities(collections):
        name = e["name"]
        if not name:
            continue
        if name in question:
            in_question.append((e["collection"], name))
        # 한 글자 이름은 본문 substring 매칭에서 오탐이 커 제외한다(질문 직접 매칭에는 기존대로 제한 없음).
        elif extra_text and len(name) >= 2 and name in extra_text:
            in_chunks.append((e["collection"], name))

    # 질문 매칭을 먼저 채운 뒤 본문 매칭으로 상한까지 보강한다(그래프 컨텍스트 폭주 방지).
    matched = (in_question + in_chunks)[: settings.graph_context_max_entities]

    if not matched:
        return "(질문과 일치하는 엔티티를 찾지 못함)"

    matched_set = set(matched)
    lines: list[str] = []
    for collection, name in matched:
        entity = graph_manager.get_entity(collection, name)
        # 브릿지된 같은 대상은 질의 스코프 안의 것만 따라간다(스코프 밖 사업은 끌어오지 않음 — 격벽 존중).
        twins = graph_manager.get_bridges(collection, name, collections)
        lines.append(f"- {entity['name']} [{entity['type']}] ({collection}): {entity['description']}")
        if twins:
            span = sorted({collection} | {t["collection"] for t in twins})
            lines.append(f"  ※ 이 대상은 {len(span)}개 사업에 걸쳐 있습니다(브릿지): {', '.join(span)}")
        _append_relations(lines, collection, name)
        for twin in twins:
            # 브릿지 상대가 이미 독립 항목으로 매칭됐다면 거기서 다루므로 중복 출력하지 않는다.
            if (twin["collection"], twin["name"]) in matched_set:
                continue
            lines.append(f"  ↔ (브릿지) [{twin['collection']}] {twin['name']}:")
            _append_relations(lines, twin["collection"], twin["name"])
    return "\n".join(lines)


# 벡터로 찾은 본문 조각을 프롬프트용 컨텍스트 문자열로 만든다.
# 완전 중복 조각을 제거하고 [본문 N] 번호를 달아, 근거의 경계를 분명히 한다(합성 시 본문 우선 판단을 돕는다).
def _build_vector_context(chunks: list[str]) -> str:
    seen: set[str] = set()
    lines: list[str] = []
    for chunk in chunks:
        key = chunk.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        lines.append(f"[본문 {len(lines) + 1}] {chunk}")
    return "\n---\n".join(lines) if lines else "(관련 본문을 찾지 못함)"


# 그래프+벡터 정보만 근거로 질문에 답한다.
# collections=None이면 전체 컬렉션을 종합(행정 종합), 지정하면 그 사업(들) 범위 안에서만 답한다.
# top_k를 주지 않으면 설정값(settings.retrieval_top_k)을 쓴다.
def answer_question(
    question: str, collections: list[str] | None = None, top_k: int | None = None
) -> str:
    if top_k is None:
        top_k = settings.retrieval_top_k
    # 벡터 검색을 먼저 1회 수행해, 찾은 본문 조각을 (a)근거 컨텍스트와 (b)그래프 매칭용 힌트로 함께 쓴다.
    chunks = vector_manager.query_similar(question, top_k=top_k, collections=collections)
    vector_context = _build_vector_context(chunks)
    # 그래프 매칭(엔티티 substring)에는 라벨이 없는 원문 청크를 넘긴다.
    graph_context = _gather_graph_context(question, collections, extra_text="\n".join(chunks))
    prompt = _ANSWER_PROMPT.format(
        graph_context=graph_context, vector_context=vector_context, question=question
    )
    logger.info("그래프 컨텍스트:\n%s", graph_context)
    # 질문 1건당 LLM 호출 1번이 나가므로 오늘 사용량에 기록한다(RPD 추적).
    sqlite_manager.record_api_usage(1)
    return generate(prompt)
