# 커뮤니티 리포트 생성(M3) — 탐지된 커뮤니티(M2)마다 LLM으로 title/summary/rating을 만들어 저장한다.
# 리프 커뮤니티(다른 커뮤니티의 parent_community_id로 지목되지 않는, 즉 자식이 없는 커뮤니티)는 멤버
# 엔티티(이름+설명)와 그 사이 관계를 재료로 삼는다. 상위 레벨(자식이 있는 커뮤니티)은 이미 만들어진
# 자식 커뮤니티들의 리포트를 재료로 bottom-up 종합한다 — community_detector의 계층 재귀 구조상 자식이
# 있는 커뮤니티의 멤버 집합은 항상 자식들 멤버 집합의 합집합이라(leidenalg가 부모 서브그래프를 자식들로
# 완전히 분할), 자식 리포트만으로 이미 그 멤버 정보를 담아 요약할 수 있어 중복 재료가 필요 없다.
# 백엔드는 레벨로 라우팅한다(spec-addendum §A): 레벨 0부터 config.report_cli_top_levels개 레벨(소수·
# 고가치, 기본 최상위 1개 레벨)은 top 백엔드(기본 claude_cli), 나머지(대량)는 bulk 백엔드(기본 ollama).
# 두 값 모두 config에서 "gemini" 등으로 바꿀 수 있다(Gemini는 폐기하지 않음, CEO 지시 spec-addendum §A).
# 개별 커뮤니티의 리포트 생성 실패(LLM 오류/파싱 실패)는 그 커뮤니티만 건너뛰고 나머지는 계속 진행한다
# (M1.5 desc_summarizer와 동형의 장애 격리). 인제스트 핫패스와 완전히 분리된 옵트인 배치 전용 모듈이다.
import hashlib
import json
import logging

from adapters.llm_adapter import generate
from config import settings
from db import graph_manager, sqlite_manager

logger = logging.getLogger(__name__)


# [M5] 리프 커뮤니티의 '리포트 입력'(멤버 이름+설명, 내부 관계, 외부 연결)을 해시해 content_signature를 만든다.
# 프롬프트에 실제로 들어가는 것과 동일한 입력이라, 이 값이 같으면 리포트도 같아야 하므로 재요약을 건너뛸 수 있다.
# (community_id는 멤버 '이름'만 해시해 설명·관계 변화를 못 잡으므로, 재사용 판정엔 이 시그니처를 써야 안전하다.)
def _leaf_signature(entities: list[dict], relations: list[dict], external_relations: list[dict]) -> str:
    payload = json.dumps(
        {
            "members": sorted([e["name"], e.get("description") or ""] for e in entities),
            "internal": sorted(f"{r['source']}|{r['predicate']}|{r['target']}" for r in relations),
            "external": sorted(f"{r['source']}|{r['predicate']}|{r['target']}" for r in external_relations),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# [M5] 상위 커뮤니티의 시그니처 — 자식들의 시그니처 집합을 해시한다. 자식이 하나라도 바뀌면(시그니처 변화)
# 이 값도 바뀌어 상위 리포트가 재생성되고, 자식이 모두 그대로면 상위도 재사용된다(정확한 계층 전파).
def _parent_signature(child_signatures: list[str]) -> str:
    payload = json.dumps({"children": sorted(child_signatures)}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

# 도메인·언어 중립 프롬프트(spec-addendum §B) — 업종 어휘("사업" 등)나 특정 언어를 가정하지 않고,
# 입력(멤버 이름·설명·관계, 또는 하위 리포트)의 언어를 그대로 따라가도록 유도한다.
_LEAF_PROMPT = """\
아래는 하나의 커뮤니티(서로 밀접하게 연결된 항목들의 묶음)를 이루는 항목들과 그 사이 관계다.
이 커뮤니티를 요약하는 리포트를 작성해줘. 항목·관계에 쓰인 언어를 그대로 사용해서 답해.

항목들:
{members}

관계들:
{relations}

이 커뮤니티가 바깥 항목과 맺는 연결(다른 그룹으로 이어지는 관계):
{external_links}

summary에는 이 커뮤니티의 핵심 내용을 담되, 위 '바깥 항목과 맺는 연결'이 있으면 이 커뮤니티가
다른 그룹과 어떻게 이어지는지도 함께 밝혀줘(없으면 생략).
다음 JSON 형식으로만 응답해줘(다른 설명이나 머리말 없이 순수 JSON만):
{{"title": "이 커뮤니티를 대표하는 짧은 제목", "summary": "핵심 내용을 종합한 문단", "rating": 0에서 10 사이 숫자(이 커뮤니티가 얼마나 중요/핵심적인지)}}
"""

_PARENT_PROMPT = """\
아래는 더 큰 커뮤니티를 이루는 하위 커뮤니티들의 리포트다. 이 하위 리포트들을 종합해,
전체를 아우르는 상위 커뮤니티 리포트를 작성해줘. 하위 리포트에 쓰인 언어를 그대로 사용해서 답해.

하위 커뮤니티 리포트들:
{child_reports}

다음 JSON 형식으로만 응답해줘(다른 설명이나 머리말 없이 순수 JSON만):
{{"title": "이 상위 커뮤니티를 대표하는 짧은 제목", "summary": "하위 리포트들을 종합한 문단", "rating": 0에서 10 사이 숫자(이 커뮤니티가 얼마나 중요/핵심적인지)}}
"""


# 리프 커뮤니티 프롬프트를 만든다 — 멤버 엔티티(이름+설명)와 그 사이 내부 관계, 그리고 커뮤니티 경계를
# 넘는 외부 연결(다른 커뮤니티로 이어지는 관계)을 각각 번호 없는 목록으로 넣는다. 외부 연결이 있어야
# 글로벌 검색이 "그룹 간 연결" 질문에 답할 수 있다(리프 리포트에 교차 엣지가 담기므로).
def _build_leaf_prompt(
    entities: list[dict], relations: list[dict], external_relations: list[dict] | None = None
) -> str:
    members = "\n".join(f"- {e['name']}: {e.get('description') or '(설명 없음)'}" for e in entities)
    if relations:
        rel_lines = "\n".join(
            f"- {r['source']} -[{r['predicate']}]-> {r['target']}" for r in relations
        )
    else:
        rel_lines = "(관계 없음)"
    if external_relations:
        ext_lines = "\n".join(
            f"- {r['source']} -[{r['predicate']}]-> {r['target']}" for r in external_relations
        )
    else:
        ext_lines = "(바깥으로 이어지는 관계 없음)"
    return _LEAF_PROMPT.format(
        members=members or "(항목 없음)", relations=rel_lines, external_links=ext_lines
    )


# 상위 레벨 커뮤니티 프롬프트를 만든다 — 이미 생성된 자식 커뮤니티 리포트(title+summary)를 재료로 넣는다.
def _build_parent_prompt(child_reports: list[dict]) -> str:
    numbered = "\n\n".join(
        f"{i}. [{c['title']}] {c['summary']}" for i, c in enumerate(child_reports, start=1)
    )
    return _PARENT_PROMPT.format(child_reports=numbered)


# LLM 원시 응답에서 title/summary/rating을 방어적으로 파싱한다(구조화 출력을 강제할 수 없는 ollama/CLI
# 백엔드 공통 대응, pipeline/ingest.py의 _parse_extraction과 동형). 코드펜스를 제거하고 JSON으로 파싱하며,
# rating이 없거나 숫자로 못 바꾸면 None으로 둔다(리포트 자체는 title/summary만 있어도 유효하다).
def _parse_report(raw_text: str) -> dict:
    cleaned = raw_text.replace("```json", "").replace("```", "").strip()
    data = json.loads(cleaned)
    title = str(data.get("title") or "").strip()
    summary = str(data.get("summary") or "").strip()
    rating = data.get("rating")
    try:
        rating = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        rating = None
    if not title or not summary:
        raise ValueError(f"title/summary가 비어 있는 응답: {data!r}")
    return {"title": title, "summary": summary, "rating": rating}


# 레벨로 백엔드를 정한다(spec-addendum §A) — 레벨 0부터 report_cli_top_levels개 레벨은 top_backend
# (기본 claude_cli, 소수·고가치), 나머지는 bulk_backend(기본 ollama, 대량·무료)로 라우팅한다.
def _backend_for_level(level: int) -> str:
    if level < settings.report_cli_top_levels:
        return settings.community_report_top_backend
    return settings.community_report_bulk_backend


# 한 컬렉션의 모든 커뮤니티(M2가 이미 탐지·저장한 것)에 리포트를 생성해 SQLite에 저장한다.
# 레벨 내림차순(리프에 가까운 깊은 레벨부터 최상위 레벨 0까지)으로 처리해, 상위 레벨을 요약할 때 이미
# 만들어진 자식 리포트를 재료로 쓸 수 있게 한다(bottom-up). 커뮤니티가 하나도 없으면(탐지 결과 0개)
# 아무 것도 하지 않는다.
# [M5] 증분 재계산: 각 커뮤니티의 '리포트 입력'을 content_signature로 해시해, 직전 빌드의 시그니처와 같으면
# LLM 재요약을 건너뛰고 기존 리포트를 그대로 재사용한다(설명·관계 변화까지 반영하는 정확한 재사용 — 자세한
# 근거는 _leaf_signature/_parent_signature 주석). 저장은 여전히 전량 delete→upsert라 낡은 리포트는 정리된다.
# 반환값 = 실제로 저장한 리포트 수.
def generate_reports(collection: str, model: str | None = None) -> int:
    communities = sqlite_manager.get_communities(collection)
    if not communities:
        return 0

    entities_by_name = {e["name"]: e for e in graph_manager.get_all_entities([collection])}
    relations = graph_manager.get_all_relations([collection])

    children_of: dict[str, list[str]] = {}
    for community in communities:
        parent_id = community.get("parent_community_id")
        if parent_id:
            children_of.setdefault(parent_id, []).append(community["community_id"])

    # [M5] 직전 빌드의 리포트를 community_id로 색인(시그니처 비교용).
    existing = {r["community_id"]: r for r in sqlite_manager.get_community_reports(collection)}
    signatures: dict[str, str] = {}  # community_id -> 이번 빌드 시그니처(상위 계산용)
    reused = 0

    reports: dict[str, dict] = {}  # community_id -> {title, summary, rating, community_id, level, content_signature}
    for community in sorted(communities, key=lambda c: -c["level"]):
        community_id = community["community_id"]
        level = community["level"]
        child_ids = children_of.get(community_id, [])

        if child_ids:
            child_reports = [reports[cid] for cid in child_ids if cid in reports]
            if not child_reports:
                logger.warning(
                    "[%s] 커뮤니티 %s: 자식 리포트가 모두 실패해 건너뜀", collection, community_id
                )
                continue
            signature = _parent_signature([r["content_signature"] for r in child_reports])
            prompt = _build_parent_prompt(child_reports)
        else:
            member_names = set(community["entity_names"])
            members = [
                entities_by_name[name] for name in community["entity_names"] if name in entities_by_name
            ]
            member_relations = [
                r for r in relations if r["source"] in member_names and r["target"] in member_names
            ]
            # 경계를 넘는 관계(정확히 한 끝만 이 커뮤니티 멤버) = 다른 그룹으로 이어지는 외부 연결.
            # 이걸 리포트에 담아야 글로벌 검색이 "그룹 간 연결" 질문에 답할 수 있다(상한으로 프롬프트 폭주 방지).
            external_relations = [
                r for r in relations
                if (r["source"] in member_names) != (r["target"] in member_names)
            ][: settings.community_report_external_max]
            signature = _leaf_signature(members, member_relations, external_relations)
            prompt = _build_leaf_prompt(members, member_relations, external_relations)

        signatures[community_id] = signature

        # [M5] 시그니처가 직전 빌드와 같으면 LLM 없이 기존 리포트를 재사용한다.
        prev = existing.get(community_id)
        if prev and prev.get("content_signature") == signature:
            reports[community_id] = {
                "community_id": community_id, "level": level, "title": prev["title"],
                "summary": prev["summary"], "rating": prev["rating"], "content_signature": signature,
            }
            reused += 1
            logger.info("[%s] 커뮤니티 %s(레벨 %d) 리포트 재사용(입력 불변)", collection, community_id, level)
            continue

        backend = _backend_for_level(level)
        try:
            raw = generate(prompt, backend=backend, model=model)
            report = _parse_report(raw)
        except Exception as exc:
            logger.warning(
                "[%s] 커뮤니티 %s(레벨 %d) 리포트 생성 실패, 건너뜀: %s",
                collection, community_id, level, exc,
            )
            continue

        report["community_id"] = community_id
        report["level"] = level
        report["content_signature"] = signature
        reports[community_id] = report
        logger.info(
            "[%s] 커뮤니티 %s(레벨 %d) 리포트 생성 완료: %s", collection, community_id, level, report["title"]
        )

    sqlite_manager.delete_community_reports_by_collection(collection)  # 재생성이라 낡은 행부터 정리
    for report in reports.values():
        sqlite_manager.upsert_community_report(
            collection, report["community_id"], report["level"], report["title"],
            report["summary"], report["rating"], report.get("content_signature"),
        )
    logger.info(
        "[%s] 리포트 %d개 저장 (재사용 %d / 재생성 %d)", collection, len(reports), reused, len(reports) - reused
    )
    return len(reports)
