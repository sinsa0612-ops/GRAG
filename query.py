# 그래프(엔티티/관계) + 벡터 검색 결과만 근거로 질문에 답하는 하이브리드 질의 모듈.
# LLM의 사전 지식이 아니라, 파이프라인이 실제로 추출/저장한 정보만 사용하는지 검증하는 용도.
# [M4] 파일 하단에 커뮤니티 리포트(M3) 위에서 map-reduce로 답하는 글로벌 검색(answer_question_global)을
# 추가했다 — 아래 로컬 검색(answer_question, _ANSWER_PROMPT)은 hot-path 불변 대상이라 한 글자도 손대지 않았다.
import json
import logging

import numpy as np

from adapters.embedding_adapter import embed_texts
from adapters.llm_adapter import generate
from config import settings
from db import graph_manager, sqlite_manager, vector_manager
from schemas import MapEvidence

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

# [글로벌 검색 재설계 — 신뢰성] 리포트 선별을 LLM 채점(비결정적 관문)에서 결정적 임베딩 랭킹으로
# 바꿨다 — 상위 K개(하한 settings.global_search_min_reports)는 무조건 MAP에 투입해 "재료 0" 실패
# 클래스를 원천 차단한다(강제 admission). MAP도 "관련도 채점"이 아니라 "근거 포인트 추출"로 바꿔
# 빈 배열=무관을 자연히 내장했다. 복합질문("A·B·C 각각")은 LLM 없이 코드로 결정적 분해해 서브질문마다
# 독립적으로 랭킹·MAP한다(한 서브질문이 다른 서브질문의 리포트를 밀어내지 않도록).

_MAP_PROMPT = """\
아래는 어떤 자료 묶음(커뮤니티)을 요약한 리포트다. 이 리포트에서 다음 질문에 답이 되는 근거 포인트만 뽑아줘.
관련 내용이 없으면 빈 배열을 반환해. 리포트에 없는 내용은 지어내지 말고, 리포트에 쓰인 언어를 그대로 사용해.

리포트 제목: {title}
리포트 요약: {summary}

질문: {question}

다음 JSON 형식으로만 응답해줘(다른 설명이나 머리말 없이 순수 JSON만):
{{"evidence_points": ["근거 포인트1", "근거 포인트2", ...]}}
"""

_REDUCE_PROMPT = """\
아래는 하나의 질문을 서브질문들로 나누어, 서로 다른 자료 묶음(커뮤니티)에서 뽑아낸 근거 포인트들이다.
각 블록은 [서브질문|출처] 형식으로 어느 서브질문·어느 자료에서 나왔는지 표시되어 있다.
이 근거들을 종합해 아래 질문에 대한 하나의 완결된 답변을 작성해줘. 근거에 없는 내용은 지어내지 말고,
가능하면 어떤 서브질문·출처에 근거했는지 답변에 드러나게 해줘. 근거에 쓰인 언어를 그대로 사용해.

질문: {question}

근거 포인트들:
{evidence_blocks}

위 근거들을 종합한 최종 답변만 작성해줘(다른 설명이나 머리말 없이 답변 본문만).
"""

# 스코프에 리포트가 아예 없을 때(커뮤니티가 한 번도 안 빌드됨) 돌려주는 안내 문자열 — CLI/GUI가 별도
# 안내 로직 없이 이 반환값을 그대로 보여주면 되도록, 빌드 명령까지 여기서 알려준다.
_NO_REPORTS_MESSAGE = (
    "이 범위에는 아직 커뮤니티 리포트가 없습니다. "
    "먼저 `graphrag communities build --collection <이름>`을 실행하세요."
)
# 리포트는 있지만(빌드는 됨) 모든 서브질문·리포트에서 근거 포인트를 하나도 못 찾아 종합할 재료가 없을 때.
_NO_RELEVANT_MESSAGE = "이 범위의 커뮤니티 리포트 중 질문과 관련된 내용을 찾지 못했습니다."
# [D1 수리] MAP 실패(stats["failed"] > 0)가 있을 때만 답변/안내 문자열 뒤에 붙이는 각주. 로그는
# CLI에서만 보이고 GUI/호출부에서는 안 보이므로, 사용자가 실제로 보는 반환 문자열이 "조용한 실패"를
# 드러내는 유일한 관측 표면이다. 실패 0건이면 이 각주는 붙지 않아 반환 문자열이 기존과 바이트 동일하다.
_MAP_FAILURE_NOTE = " ※ MAP 호출 {failed}/{calls}건이 실패했습니다 — 로그를 확인하세요."

# 복합질문 분해에 쓰는 열거형 구분자 화이트리스트. 쉼표(,)와 '와/과'는 일부러 뺐다 — 쉼표는 노이즈가
# 크고(문장 중간에도 흔함), '와/과'는 "A와 B의 관계"처럼 관계형 질문을 잘못 쪼갤 위험이 커서다.
# [D3 보수화] '·'도 뺐다 — 한국어에서 '·'는 열거보다 복합명사(한·미, 가·나·다) 용도가 더 흔해
# (실측 6케이스 중 5건 오분해), '·' 열거형 질문의 분해 이득보다 오분해 위험이 더 크다.
_DECOMPOSE_DELIMITERS = ("、", " 및 ", " 그리고 ")


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


# 복합질문을 열거형 구분자로만 분리하는 순수 함수(LLM 미사용, 완전 결정적). [D3 보수화] 분해는
# all-or-nothing이다 — 구분자가 하나도 없으면 즉시 원질문만 반환하고, 구분자가 있어도 조각 중 하나라도
# "질문 형태"(물음표로 끝나거나 원질문의 마지막 어절로 끝남)가 아니면 분해를 통째로 포기하고 원질문만
# 반환한다. 사전·형태소 분석 없이 "이 조각이 질문인가"를 결정적으로 판정하는 가장 단순한 규칙이며, 조각
# 일부만 살아남는 침묵의 손실(무의미한 조각이 "서브질문"으로 노출되는 것)을 없앤다. 유효 분해 시에도
# 원질문은 항상 결과에 포함한다(안전판). 총 개수는 settings.global_search_max_subqueries로 절단한다.
def _decompose_question(question: str) -> list[str]:
    stripped = question.strip()
    words = stripped.split()
    tail = words[-1] if words else ""
    normalized = question
    for delimiter in _DECOMPOSE_DELIMITERS:
        normalized = normalized.replace(delimiter, "\x00")
    if "\x00" not in normalized:
        return [question]
    fragments = [p.strip() for p in normalized.split("\x00")]
    if not all(len(f) >= 2 and (f.endswith("?") or f.endswith(tail)) for f in fragments):
        return [question]
    subqueries = list(dict.fromkeys([question] + fragments))
    return subqueries[: settings.global_search_max_subqueries]


# 질의 벡터와 각 리포트 벡터의 코사인 유사도를 내림차순으로 매겨 리포트 인덱스 리스트를 돌려준다.
# embed_texts가 정규화 안 된 벡터를 반환하므로 여기서 직접 정규화한다. 순수 함수 — 같은 입력은 항상
# 같은 순서를 낸다(결정성이 이번 신뢰성 수정의 핵심 전제).
def _cosine_scores(query_vec: list[float], report_vecs: list[list[float]]) -> list[float]:
    query = np.asarray(query_vec, dtype=float)
    query = query / (np.linalg.norm(query) or 1.0)
    matrix = np.asarray(report_vecs, dtype=float)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0
    return [float(s) for s in (matrix / norms[:, None]) @ query]


def _cosine_rank(query_vec: list[float], report_vecs: list[list[float]]) -> list[int]:
    scores = _cosine_scores(query_vec, report_vecs)
    return [int(i) for i in np.argsort(-np.asarray(scores))]


# 서브질문 하나에 대해 스코프 내 리포트를 임베딩 코사인 유사도 내림차순으로 정렬해 돌려준다. 질문과
# 모든 리포트 텍스트(title+summary)를 한 번의 embed_texts 콜로 함께 인코딩해 호출 수를 아낀다.
# 유사도는 _RANK_SCORE_KEY로 각 리포트 사본에 실어 보낸다 — 뒤이어 _select_reports가 '상위 몇 개'가
# 아니라 '최고점 대비 얼마나 가까운가'로 자르기 때문에 순서만으로는 부족하다. 원본 dict는 건드리지 않는다.
_RANK_SCORE_KEY = "_rank_score"


def _rank_reports(subquery: str, reports: list[dict]) -> list[dict]:
    texts = [f"{r['title']}\n{r['summary']}" for r in reports]
    vectors = embed_texts([subquery] + texts)
    query_vec, report_vecs = vectors[0], vectors[1:]
    scores = _cosine_scores(query_vec, report_vecs)
    order = _cosine_rank(query_vec, report_vecs)
    return [{**reports[i], _RANK_SCORE_KEY: scores[i]} for i in order]


# 랭킹된 리포트 중 '최고 유사도 * relative_ratio' 이상인 것을 전부 MAP에 투입한다.
# 고정 개수(K)를 쓰지 않는 이유는 질문마다 필요한 폭이 다르기 때문이다 — 좁은 질문은 1위가 압도적으로
# 튀어 소수만 걸리고, 광역 질문은 점수가 몰려 있어 다수가 걸린다(config.global_search_relative_ratio 참조).
# 하한(min_reports)은 "재료 0" 실패를 막고, 상한(max_reports)은 로컬 모델에서 광역 질문 하나가
# 무한정 길어지는 것을 막는다. 상한에 걸려 잘릴 때는 몇 개를 버렸는지 남긴다(조용한 절단 금지).
def _select_reports(ranked: list[dict]) -> list[dict]:
    if not ranked:
        return []
    cutoff = ranked[0].get(_RANK_SCORE_KEY, 0.0) * settings.global_search_relative_ratio
    admitted = [r for r in ranked if r.get(_RANK_SCORE_KEY, 0.0) >= cutoff]
    if len(admitted) < settings.global_search_min_reports:
        admitted = ranked[: settings.global_search_min_reports]
    if len(admitted) > settings.global_search_max_reports:
        logger.warning(
            "MAP 투입 상한(%d) 적용 — 기준을 넘은 리포트 %d개 중 %d개를 제외했습니다.",
            settings.global_search_max_reports,
            len(admitted),
            len(admitted) - settings.global_search_max_reports,
        )
        admitted = admitted[: settings.global_search_max_reports]
    return admitted


# MAP: 리포트 하나 + 서브질문 하나로 "근거 포인트 추출" LLM 1콜을 던진다. temperature=0 + JSON 강제로
# 결정성을 보강한다(진짜 신뢰성 잠금은 _select_top_k의 강제 admission). 코드펜스 제거 후 json.loads →
# MapEvidence로 입구 검증해 evidence_points만 돌려준다. [D1 수리] LLM 호출/JSON 파싱/스키마 검증 중
# 하나라도 실패하면(키 누락·옛 포맷·오타 키 포함) `None`을 돌려주고 경고 로그를 남긴다 — `[]`는 LLM이
# evidence_points를 명시적으로 빈 배열로 준, 진짜 "무관" 판정일 때만 나온다(둘을 타입으로 구분해야
# 호출부가 "조용한 실패"와 "정말 무관"을 구별할 수 있다). backend가 "gemini"로 해석될 때만(config로
# 옵트인 시) 호출마다 RPD 사용량을 기록한다.
def _map_report_evidence(report: dict, subquery: str) -> list[str] | None:
    backend = settings.global_search_map_backend
    prompt = _MAP_PROMPT.format(title=report["title"], summary=report["summary"], question=subquery)
    try:
        raw = generate(
            prompt,
            backend=backend,
            temperature=settings.global_search_map_temperature,
            format_json=True,
        )
        if backend in (None, "gemini"):
            sqlite_manager.record_api_usage(1)
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        evidence = MapEvidence.model_validate(json.loads(cleaned))
    except Exception as exc:
        logger.warning(
            "[%s] 커뮤니티 %s: 글로벌 MAP 실패, 건너뜀: %s",
            report["collection"], report["community_id"], exc,
        )
        return None
    return evidence.evidence_points


# 서브질문마다 독립적으로(전역 K가 아니라 서브질문별 top-K) 리포트를 랭킹·선별해 MAP을 돌리고, 근거
# 포인트가 나온 리포트만 서브질문·출처 라벨을 붙여 모은다. 이렇게 하면 복합질문에서 한 서브질문이
# 다른 서브질문의 리포트를 밀어내는 일이 없다(합의 처방3). [D1 수리] MAP이 `None`(실패)을 돌려주면
# bundles에 넣지 않고 stats["failed"]로 집계한다 — `[]`(명시적 무관)와 구분해야 사용자에게 "몇 건이
# 조용히 실패했는지"를 각주로 알릴 수 있다. stats["examined"]는 MAP에 투입된 서로 다른 리포트 수
# (같은 리포트가 여러 서브질문에 재사용돼도 1번만 센다), stats["calls"]는 MAP 총 호출 수다.
def _collect_evidence(subqueries: list[str], reports: list[dict]) -> tuple[list[dict], dict]:
    bundles: list[dict] = []
    examined: set[tuple[str, str]] = set()
    calls = 0
    failed = 0
    for subquery in subqueries:
        for report in _select_reports(_rank_reports(subquery, reports)):
            calls += 1
            examined.add((report["collection"], report["community_id"]))
            points = _map_report_evidence(report, subquery)
            if points is None:
                failed += 1
                continue
            if not points:
                continue
            bundles.append(
                {
                    "subquery": subquery,
                    "collection": report["collection"],
                    "community_id": report["community_id"],
                    "title": report["title"],
                    "points": points,
                }
            )
    stats = {"examined": len(examined), "calls": calls, "failed": failed}
    return bundles, stats


# REDUCE: 서브질문·출처 라벨이 붙은 근거 포인트 묶음들을 하나의 최종 답변으로 종합한다(LLM 1콜).
# 호출부(answer_question_global)가 bundles가 비어 있지 않음을 이미 보장하므로 여기서는 항상 최소
# 1건 이상을 받는다. backend가 "gemini"로 해석될 때만 RPD 사용량을 기록한다(MAP과 동일 원칙).
def _reduce_evidence(bundles: list[dict], question: str) -> str:
    blocks = "\n\n".join(
        f"[{b['subquery']}|출처={b['collection']}/{b['community_id']}/{b['title']}]\n"
        + "\n".join(f"- {point}" for point in b["points"])
        for b in bundles
    )
    prompt = _REDUCE_PROMPT.format(question=question, evidence_blocks=blocks)
    backend = settings.global_search_reduce_backend
    answer = generate(prompt, backend=backend)
    if backend in (None, "gemini"):
        sqlite_manager.record_api_usage(1)
    return answer


# 커뮤니티 리포트(M3) 위에서 map-reduce로 코퍼스 단위 sensemaking 질문에 답한다.
# collections=None이면 존재하는 모든 컬렉션의 리포트를 모아 종합한다(--all, 컬렉션별 union — 격벽 유지,
# 위 _collect_global_reports 참고). level=None이면 설정 기본 레벨(레벨 0=최상위)을 쓴다. 질문은 코드로
# 결정적 분해해(_decompose_question) 서브질문마다 독립적으로 리포트를 랭킹·MAP한 뒤(_collect_evidence),
# 근거를 REDUCE로 종합한다. 리포트가 비어 있으면(미빌드) 빌드 안내를, 모든 서브질문·리포트에서 근거를
# 못 찾으면 "못 찾음" 안내를 반환한다 — CLI/GUI는 이 반환값을 그대로 보여주기만 하면 되므로 호출부에
# 별도 안내 로직이 필요 없다.
def answer_question_global(
    question: str, collections: list[str] | None = None, level: int | None = None
) -> str:
    target_collections = _resolve_global_collections(collections)
    level_to_use = settings.global_search_default_level if level is None else level
    reports = _collect_global_reports(target_collections, level_to_use)
    if not reports:
        return _NO_REPORTS_MESSAGE
    subqueries = _decompose_question(question)
    bundles, stats = _collect_evidence(subqueries, reports)
    answer = _NO_RELEVANT_MESSAGE if not bundles else _reduce_evidence(bundles, question)
    # [D1 수리] MAP 실패가 하나라도 있으면 그 사실을 답변 뒤에 명시한다(실패 0건이면 바이트 동일 유지).
    if stats["failed"] > 0:
        answer += _MAP_FAILURE_NOTE.format(failed=stats["failed"], calls=stats["calls"])
    return answer
