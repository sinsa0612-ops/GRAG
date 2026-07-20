# 그래프(엔티티/관계) + 벡터 검색 결과만 근거로 질문에 답하는 하이브리드 질의 모듈.
# LLM의 사전 지식이 아니라, 파이프라인이 실제로 추출/저장한 정보만 사용하는지 검증하는 용도.
# [M4] 파일 하단에 커뮤니티 리포트(M3) 위에서 map-reduce로 답하는 글로벌 검색(answer_question_global)을
# 추가했다 — 아래 로컬 검색(answer_question, _ANSWER_PROMPT)은 hot-path 불변 대상이라 한 글자도 손대지 않았다.
import json
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
    question: str,
    collections: list[str] | None = None,
    top_k: int | None = None,
    backend: str | None = None,
) -> str:
    if top_k is None:
        top_k = settings.retrieval_top_k
    # 답변 합성 백엔드: 지정 없으면 설정값(기본 ollama = 무과금). "gemini"로 해석되면 아래 record/generate가
    # 기존 hot-path와 100% 동일하게 동작한다(오케스트레이션만 추가, 프롬프트·검색 로직 불변).
    backend = backend or settings.answer_backend
    # 벡터 검색을 먼저 1회 수행해, 찾은 본문 조각을 (a)근거 컨텍스트와 (b)그래프 매칭용 힌트로 함께 쓴다.
    chunks = vector_manager.query_similar(question, top_k=top_k, collections=collections)
    vector_context = _build_vector_context(chunks)
    # 그래프 매칭(엔티티 substring)에는 라벨이 없는 원문 청크를 넘긴다.
    graph_context = _gather_graph_context(question, collections, extra_text="\n".join(chunks))
    prompt = _ANSWER_PROMPT.format(
        graph_context=graph_context, vector_context=vector_context, question=question
    )
    logger.info("그래프 컨텍스트:\n%s", graph_context)
    # Gemini만 RPD 한도에 잡히므로 그때만 사용량을 기록한다(ollama/CLI는 로컬·구독이라 무관).
    if backend in (None, "gemini"):
        sqlite_manager.record_api_usage(1)
    return generate(prompt, backend=backend)


# ══════════════════════ M4: 글로벌(map-reduce) 검색 ══════════════════════
# 커뮤니티 리포트(M3가 생성해 SQLite에 저장한 것) 위에서 map-reduce로 코퍼스 단위 sensemaking 질문
# ("이 자료 전체의 주제는?" 류)에 답한다. 로컬 검색(answer_question)과 달리 원문 조각이 아니라 이미
# 요약된 리포트를 재료로 쓰므로, 개별 사실보다 "전체를 종합한 그림"에 강하다.
# 도메인·언어 중립 프롬프트(spec-addendum §B) — community_reporter.py의 프롬프트와 동일한 톤으로,
# 업종 어휘를 가정하지 않고 입력(리포트/부분답변)에 쓰인 언어를 그대로 따라가도록 유도한다.

_MAP_PROMPT = """\
아래는 어떤 자료 묶음(커뮤니티)을 요약한 리포트다. 이 리포트가 다음 질문에 답하는 데 도움이 되는지 판단해줘.
리포트에 쓰인 언어를 그대로 사용해서 답해.

리포트 제목: {title}
리포트 요약: {summary}

질문: {question}

다음 JSON 형식으로만 응답해줘(다른 설명이나 머리말 없이 순수 JSON만):
{{"relevance": 이 리포트가 질문에 얼마나 도움되는지 나타내는 0에서 10 사이 정수(전혀 관련 없으면 0), "partial_answer": "이 리포트만 근거로 한 부분 답변(관련 없으면 빈 문자열)"}}
"""

_REDUCE_PROMPT = """\
아래는 하나의 질문에 대해 서로 다른 자료 묶음(커뮤니티)에서 나온 부분 답변들이다(관련도 높은 순으로 나열).
이 부분 답변들을 종합해 질문에 대한 하나의 완결된 답변을 작성해줘. 부분 답변에 쓰인 언어를 그대로 사용해서 답해.

질문: {question}

부분 답변들(관련도 높은 순):
{partial_answers}

위 부분 답변들을 종합한 최종 답변만 작성해줘(다른 설명이나 머리말 없이 답변 본문만).
"""

# 스코프에 리포트가 아예 없을 때(커뮤니티가 한 번도 안 빌드됨) 돌려주는 안내 문자열 — CLI/GUI가 별도
# 안내 로직 없이 이 반환값을 그대로 보여주면 되도록, 빌드 명령까지 여기서 알려준다.
_NO_REPORTS_MESSAGE = (
    "이 범위에는 아직 커뮤니티 리포트가 없습니다. "
    "먼저 `graphrag communities build --collection <이름>`을 실행하세요."
)
# 리포트는 있지만(빌드는 됨) MAP 결과가 전부 관련도 0(또는 파싱 실패)이라 종합할 부분답변이 없을 때.
_NO_RELEVANT_MESSAGE = "이 범위의 커뮤니티 리포트 중 질문과 관련된 내용을 찾지 못했습니다."


# collections가 None이면(--all) 문서·그래프 어느 쪽에든 존재가 확인된 모든 컬렉션을 대상으로 한다
# (graphrag_cli._print_communities_status와 동일 관례). 명시되면 그 목록을 그대로 쓴다.
def _resolve_global_collections(collections: list[str] | None) -> list[str]:
    if collections is not None:
        return collections
    return sorted(
        set(sqlite_manager.get_collection_doc_counts()) | set(graph_manager.get_all_collections())
    )


# 스코프 내 커뮤니티 리포트를 모은다. 컬렉션마다 따로 조회해 리스트를 이어붙일 뿐이므로(union),
# 서로 다른 컬렉션의 멤버가 하나의 커뮤니티로 섞이는 일은 없다 — 격벽은 이미 탐지 단계
# (community_detector, M2)에서 컬렉션별로 지켜졌고, 여기서는 그 결과물(리포트)을 나열만 한다.
def _collect_global_reports(collections: list[str], level: int) -> list[dict]:
    reports: list[dict] = []
    for collection in collections:
        reports.extend(sqlite_manager.get_community_reports(collection, level=level))
    return reports


# MAP 단계 LLM 원시 응답에서 relevance/partial_answer를 방어적으로 파싱한다
# (community_reporter._parse_report와 동형 — 코드펜스 제거 후 json.loads). relevance는 정수로
# 강제하고 음수는 0으로 clamp한다(파싱 실패는 예외로 알려 호출부가 그 리포트만 건너뛰게 한다).
def _parse_map_response(raw_text: str) -> dict:
    cleaned = raw_text.replace("```json", "").replace("```", "").strip()
    data = json.loads(cleaned)
    relevance = max(0, int(data.get("relevance") or 0))
    partial_answer = str(data.get("partial_answer") or "").strip()
    return {"relevance": relevance, "partial_answer": partial_answer}


# MAP: 리포트 하나마다 "질문에 도움되는가?" LLM 질의를 던져 관련도 점수+부분답변을 얻는다.
# 개별 리포트 실패(LLM 오류/파싱 실패)는 그 리포트만 건너뛰고 계속한다(community_reporter와 동형의 장애
# 격리). 관련도 0(또는 파싱 실패로 취급)인 리포트는 버린다. backend가 "gemini"로 해석될 때만(config로
# 옵트인 시) 호출마다 RPD 사용량을 기록한다 — Ollama는 무료라 기본 설정에서는 기록되지 않는다.
def _map_reports(reports: list[dict], question: str) -> list[dict]:
    backend = settings.global_search_map_backend
    scored: list[dict] = []
    for report in reports:
        prompt = _MAP_PROMPT.format(title=report["title"], summary=report["summary"], question=question)
        try:
            raw = generate(prompt, backend=backend)
            if backend in (None, "gemini"):
                sqlite_manager.record_api_usage(1)
            parsed = _parse_map_response(raw)
        except Exception as exc:
            logger.warning(
                "[%s] 커뮤니티 %s: 글로벌 MAP 실패, 건너뜀: %s",
                report["collection"], report["community_id"], exc,
            )
            continue
        if parsed["relevance"] <= 0 or not parsed["partial_answer"]:
            continue
        scored.append(parsed)
    return scored


# REDUCE: 관련도 상위 부분답변들을 하나의 최종 답변으로 종합한다(LLM 1콜). 호출부(answer_question_global)가
# scored가 비어 있지 않음을 이미 보장하므로 여기서는 항상 최소 1건 이상을 받는다. backend가 "gemini"로
# 해석될 때만 RPD 사용량을 기록한다(MAP과 동일 원칙).
def _reduce_answers(scored: list[dict], question: str) -> str:
    scored.sort(key=lambda s: -s["relevance"])
    partial_answers = "\n\n".join(f"- (관련도 {s['relevance']}) {s['partial_answer']}" for s in scored)
    prompt = _REDUCE_PROMPT.format(question=question, partial_answers=partial_answers)
    backend = settings.global_search_reduce_backend
    answer = generate(prompt, backend=backend)
    if backend in (None, "gemini"):
        sqlite_manager.record_api_usage(1)
    return answer


# 커뮤니티 리포트(M3) 위에서 map-reduce로 코퍼스 단위 sensemaking 질문에 답한다.
# collections=None이면 존재하는 모든 컬렉션의 리포트를 모아 종합한다(--all, 컬렉션별 union — 격벽 유지,
# 위 _collect_global_reports 참고). level=None이면 설정 기본 레벨(레벨 0=최상위)을 쓴다.
# 리포트가 비어 있으면(미빌드) 빌드 안내를, MAP 결과가 전부 무관하면 "못 찾음" 안내를 반환한다 —
# CLI/GUI는 이 반환값을 그대로 보여주기만 하면 되므로 호출부에 별도 안내 로직이 필요 없다.
def answer_question_global(
    question: str, collections: list[str] | None = None, level: int | None = None
) -> str:
    target_collections = _resolve_global_collections(collections)
    level_to_use = settings.global_search_default_level if level is None else level
    reports = _collect_global_reports(target_collections, level_to_use)
    if not reports:
        return _NO_REPORTS_MESSAGE
    scored = _map_reports(reports, question)
    if not scored:
        return _NO_RELEVANT_MESSAGE
    return _reduce_answers(scored, question)
