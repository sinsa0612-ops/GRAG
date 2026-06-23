# 그래프(엔티티/관계) + 벡터 검색 결과만 근거로 질문에 답하는 하이브리드 질의 모듈.
# LLM의 사전 지식이 아니라, 파이프라인이 실제로 추출/저장한 정보만 사용하는지 검증하는 용도.
import logging

from adapters.llm_adapter import generate
from db import graph_manager, vector_manager

logger = logging.getLogger(__name__)

_ANSWER_PROMPT = """\
아래 [그래프 정보]와 [관련 본문 조각]만 근거로 질문에 답해.
거기 없는 내용은 사전 지식이나 추측으로 채우지 말고, 정보가 부족하면 부족하다고 말해.

[그래프 정보]
{graph_context}

[관련 본문 조각]
{vector_context}

질문: {question}
"""


# 질문 안에 그래프의 기존 엔티티 이름이 등장하면, 그 엔티티의 설명과 양방향 관계를 모은다.
# collections로 범위를 지정하면 그 사업(들)만, None이면 전체 컬렉션을 가로질러 모은다(행정 종합).
def _gather_graph_context(question: str, collections: list[str] | None = None) -> str:
    # 같은 이름이 컬렉션마다 따로 있을 수 있으므로 (collection, name) 단위로 매칭한다.
    matched = [
        (e["collection"], e["name"])
        for e in graph_manager.get_all_entities(collections)
        if e["name"] and e["name"] in question
    ]

    if not matched:
        return "(질문과 일치하는 엔티티를 찾지 못함)"

    lines = []
    for collection, name in matched:
        entity = graph_manager.get_entity(collection, name)
        lines.append(f"- {entity['name']} [{entity['type']}] ({collection}): {entity['description']}")
        for r in graph_manager.get_outgoing_relations(collection, name):
            lines.append(f"  - {name} -[{r['predicate']}]-> {r['target']}")
        for r in graph_manager.get_incoming_relations(collection, name):
            lines.append(f"  - {r['source']} -[{r['predicate']}]-> {name}")
    return "\n".join(lines)


# 벡터 검색으로 질문과 의미적으로 가까운 본문 조각을 모은다(컬렉션 범위 안에서).
def _gather_vector_context(question: str, top_k: int = 8, collections: list[str] | None = None) -> str:
    chunks = vector_manager.query_similar(question, top_k=top_k, collections=collections)
    if not chunks:
        return "(관련 본문을 찾지 못함)"
    return "\n---\n".join(chunks)


# 그래프+벡터 정보만 근거로 질문에 답한다.
# collections=None이면 전체 컬렉션을 종합(행정 종합), 지정하면 그 사업(들) 범위 안에서만 답한다.
def answer_question(question: str, collections: list[str] | None = None, top_k: int = 8) -> str:
    graph_context = _gather_graph_context(question, collections)
    vector_context = _gather_vector_context(question, top_k=top_k, collections=collections)
    prompt = _ANSWER_PROMPT.format(
        graph_context=graph_context, vector_context=vector_context, question=question
    )
    logger.info("그래프 컨텍스트:\n%s", graph_context)
    return generate(prompt)
