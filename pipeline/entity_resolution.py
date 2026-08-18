# 엔티티 자동 병합(Entity Resolution) — ①표기 정규화로 공짜 병합 ②임베딩 유사도로 의미 병합.
import logging
import re

from sklearn.metrics.pairwise import cosine_similarity

import backup_db
from adapters.embedding_adapter import embed_texts
from config import settings
from db import graph_manager, sqlite_manager

logger = logging.getLogger(__name__)


# 이름 끝에 붙은 한국어 조사(주격 이/가, 보조사 은/는, 목적격 을/를 등)를 떼어 같은 대상의 표기 변형을
# 합칠 수 있게 한다. 예: "길동이" -> "길동"(서술 중 "길동이가"처럼 이름에 조사가 눌어붙는 한국어 서사 특유의
# 파편화 대응). 이름 자체가 조사 글자로 끝나는 짧은 이름("순이" 등)의 오삭제를 막으려 3글자 이상만 처리한다.
_KOREAN_JOSA = ("이", "가", "은", "는", "을", "를", "과", "와")


def strip_trailing_josa(name: str) -> str:
    if len(name) > 2 and name[-1] in _KOREAN_JOSA:
        return name[:-1]
    return name


# 이름을 비교용 키로 정규화한다 — 끝 조사 제거 후 공백·구두점·기호를 없애고 소문자로. 한글/영문/숫자만 남긴다.
# 표기만 다른 같은 대상("연료전지 시스템"/"연료전지시스템", "길동"/"길동이")을 같은 키로 모으되,
# 구성 문자 자체가 다른 것("GC/FID"≠"GC/FID/TCD")은 키가 달라 섞이지 않는다(안전한 무료 병합).
def _normalize_name(name: str) -> str:
    return re.sub(r"[\s\W_]+", "", strip_trailing_josa(name), flags=re.UNICODE).lower()


# 한 컬렉션 안의 엔티티 이름을 서로 비교해, keep_score가 참인 점수의 쌍만 돌려준다(블랙리스트는 제외).
# 비교는 컬렉션 내로만 일어난다 — 무관한 사업끼리 자동으로 엮이지 않게.
def _find_pairs(collection: str, keep_score) -> list[tuple[str, str, float]]:
    entities = graph_manager.get_all_entities([collection])
    if len(entities) < 2:
        return []

    # 임베딩 입력은 '이름만' 쓴다 — 설명문을 섞으면 같은 대상이라도 청크마다 설명이 달라
    # 유사도가 깎여 병합이 안 되는 문제가 있었다(실측: 설명 포함 0.83 → 이름만 0.98).
    texts = [e["name"] for e in entities]
    vectors = embed_texts(texts)
    similarity_matrix = cosine_similarity(vectors)

    pairs: list[tuple[str, str, float]] = []
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            score = float(similarity_matrix[i][j])
            if not keep_score(score):
                continue
            name_a, name_b = entities[i]["name"], entities[j]["name"]
            if sqlite_manager.is_merge_blacklisted(collection, name_a, name_b):
                logger.info("병합 예외 규칙 적용됨[%s]: %s / %s", collection, name_a, name_b)
                continue
            pairs.append((name_a, name_b, score))
    return pairs


# 자동 병합해도 안전할 만큼 유사도가 높은(임계값 초과) 쌍만 찾는다.
def find_merge_candidates(collection: str) -> list[tuple[str, str, float]]:
    return _find_pairs(collection, lambda s: s > settings.merge_similarity_threshold)


# 표기(공백·구두점·대소문자)만 다른 같은 이름들을 임베딩 없이 묶어 병합쌍 목록으로 만든다.
# 정규화 키가 같은 노드끼리만 묶으므로 의미가 다른데 우연히 겹칠 위험이 거의 없다(무료·안전).
# 각 그룹에서 연결이 가장 많은(가장 중심적인) 노드를 보존하고 나머지를 그쪽으로 합친다.
# 동률이면 더 짧은 이름을, 그래도 같으면 사전순 앞을 보존해 결정적으로 동작한다.
def find_normalized_duplicates(collection: str) -> list[tuple[str, str]]:
    entities = graph_manager.get_all_entities([collection])
    if len(entities) < 2:
        return []

    # 노드별 연결 수(degree)를 한 번에 계산해 보존 노드 선정 기준으로 쓴다.
    degree: dict[str, int] = {}
    for relation in graph_manager.get_all_relations([collection]):
        degree[relation["source"]] = degree.get(relation["source"], 0) + 1
        degree[relation["target"]] = degree.get(relation["target"], 0) + 1

    groups: dict[str, list[str]] = {}
    for entity in entities:
        key = _normalize_name(entity["name"])
        if not key:
            continue
        groups.setdefault(key, []).append(entity["name"])

    pairs: list[tuple[str, str]] = []
    for names in groups.values():
        if len(names) < 2:
            continue
        keep = max(names, key=lambda n: (degree.get(n, 0), -len(n), n))
        for drop in names:
            if drop == keep:
                continue
            if sqlite_manager.is_merge_blacklisted(collection, keep, drop):
                logger.info("병합 예외 규칙 적용됨[%s]: %s / %s", collection, keep, drop)
                continue
            pairs.append((keep, drop))
    return pairs


# 찾아낸 후보 쌍들을 실제로 그래프 DB에서 병합 실행한다(해당 컬렉션 안에서).
# drop된 이름은 keep 엔티티의 alias로 남겨둬서, 다음에 같은 표현이 또 나오면
# (느린 임베딩 비교 없이) 정확매칭만으로 즉시 같은 엔티티로 인식되게 한다.
def apply_merges(collection: str, candidates: list[tuple[str, str, float]]) -> None:
    for name_a, name_b, score in candidates:
        logger.info("병합 실행[%s]: '%s' <- '%s' (유사도 %.1f%%)", collection, name_a, name_b, score * 100)
        graph_manager.add_alias(collection, name_a, name_b)
        graph_manager.merge_entity_into(collection, keep_name=name_a, drop_name=name_b)


# 병합 후보 탐색부터 실행까지 전체 과정을 수행한다.
# collections=None이면 그래프에 있는 모든 컬렉션을 각각(컬렉션 내) 병합한다.
# 컬렉션마다 ①표기 정규화 병합(무료) → ②임베딩 의미 병합 순으로 적용한다(정규화로 노드를 먼저
# 줄여두면 임베딩 비교 대상도 줄어든다). 실제로 병합할 게 있을 때만, 되돌릴 수 있도록 안전 백업을 한 번 만든다.
def run(collections: list[str] | None = None) -> None:
    target_collections = collections or graph_manager.get_all_collections()
    state = {"backed_up": False}

    # 첫 실제 병합 직전에 딱 한 번만 안전 백업을 만든다(병합할 게 없으면 백업도 안 함).
    def ensure_backup() -> None:
        if not state["backed_up"]:
            backup_path = backup_db.create_backup()
            logger.info("병합 작업 전 안전 백업 생성: %s", backup_path)
            state["backed_up"] = True

    total = 0
    for collection in target_collections:
        merged_in_collection = 0

        # ① 표기만 다른 중복(공백·구두점)을 임베딩 없이 먼저 합친다.
        normalized_pairs = find_normalized_duplicates(collection)
        if normalized_pairs:
            ensure_backup()
            for keep, drop in normalized_pairs:
                logger.info("정규화 병합[%s]: '%s' <- '%s'", collection, keep, drop)
                graph_manager.add_alias(collection, keep, drop)
                graph_manager.merge_entity_into(collection, keep_name=keep, drop_name=drop)
            total += len(normalized_pairs)
            merged_in_collection += len(normalized_pairs)

        # ② 남은 노드를 임베딩 유사도로 비교해 의미가 같은 것을 합친다.
        candidates = find_merge_candidates(collection)
        if candidates:
            ensure_backup()
            apply_merges(collection, candidates)
            total += len(candidates)
            merged_in_collection += len(candidates)

        # [M2] 그래프 구조가 바뀌었으니 이 컬렉션의 커뮤니티는 재빌드가 필요하다(수동 병합·블랙리스트
        # 해제 후 재병합 모두 이 run()을 거치므로, addendum §C-3이 요구하는 두 경우를 여기서 함께 커버한다).
        if merged_in_collection:
            sqlite_manager.mark_communities_dirty(collection)

    if total == 0:
        logger.info("병합 후보가 없습니다.")
    else:
        logger.info("총 %d쌍의 노드를 병합했습니다.", total)


# --- 검토 회색대(반자동 병합) — 이름만으로는 기계가 못 가르는 구간을 사람에게 넘긴다 ---
# 배경(실측, bge-m3): 합쳐야 하는 "국민연금공단"↔"국민연금기금"(0.870)이 합치면 안 되는
# "국민연금공단"↔"국민건강보험공단"(0.874)보다 점수가 낮다. 두 분포가 겹치는 게 아니라 역전이라
# merge_similarity_threshold를 낮추는 처방은 교차문서 연결을 얻는 대신 오병합을 더 얻는다.
# 그래서 회색대는 자동 판정하지 않고, 판단 근거(설명·관계)를 붙여 사용자 승인 목록으로 내보낸다.


# 후보 노드가 뭘 하는 대상인지 한눈에 보이도록 설명과 대표 관계를 붙인다(사용자 판단 재료).
def _entity_context(
    name: str,
    descriptions: dict[str, str],
    relations: list[dict],
    limit: int = 3,
) -> dict:
    lines = [
        f"{r['source']} -[{r['predicate']}]-> {r['target']}"
        for r in relations
        if name in (r["source"], r["target"])
    ]
    return {
        "description": descriptions.get(name, ""),
        "relations": lines[:limit],
        "degree": len(lines),
    }


# 자동 병합 임계값과 검토 하한 사이에 있는 쌍을, 사용자가 판단할 수 있는 맥락과 함께 돌려준다.
# 보존(keep) 후보는 find_normalized_duplicates와 같은 규칙 — 연결이 많은 쪽, 동률이면 짧은 이름,
# 그래도 같으면 사전순 앞 — 으로 결정적으로 고른다. 점수 높은 순 정렬.
def find_review_candidates(collection: str) -> list[dict]:
    pairs = _find_pairs(
        collection,
        lambda s: settings.merge_review_threshold < s <= settings.merge_similarity_threshold,
    )
    if not pairs:
        return []

    relations = graph_manager.get_all_relations([collection])
    descriptions = {e["name"]: e.get("description") or "" for e in graph_manager.get_all_entities([collection])}
    degree: dict[str, int] = {}
    for relation in relations:
        degree[relation["source"]] = degree.get(relation["source"], 0) + 1
        degree[relation["target"]] = degree.get(relation["target"], 0) + 1

    reviews: list[dict] = []
    for name_a, name_b, score in pairs:
        keep, drop = sorted((name_a, name_b), key=lambda n: (-degree.get(n, 0), len(n), n))
        reviews.append(
            {
                "keep": keep,
                "drop": drop,
                "score": score,
                "keep_context": _entity_context(keep, descriptions, relations),
                "drop_context": _entity_context(drop, descriptions, relations),
            }
        )
    return sorted(reviews, key=lambda r: -r["score"])


# 사용자가 검토한 결과를 한 번에 반영한다.
# 승인(approved)은 실제 병합 + alias 등록, 거부(rejected)는 병합 블랙리스트에 남겨 다시 묻지 않게 한다.
# 실제로 병합할 게 있을 때만 되돌릴 수 있도록 안전 백업을 한 번 만든다(run()과 같은 규율).
def apply_review_decisions(
    collection: str,
    approved: list[tuple[str, str]],
    rejected: list[tuple[str, str]],
) -> None:
    if approved:
        backup_path = backup_db.create_backup()
        logger.info("검토 병합 전 안전 백업 생성: %s", backup_path)
        for keep, drop in approved:
            logger.info("검토 승인 병합[%s]: '%s' <- '%s'", collection, keep, drop)
            graph_manager.add_alias(collection, keep, drop)
            graph_manager.merge_entity_into(collection, keep_name=keep, drop_name=drop)
        sqlite_manager.mark_communities_dirty(collection)

    for name_a, name_b in rejected:
        sqlite_manager.add_merge_blacklist(collection, name_a, name_b, "검토에서 다른 대상으로 확정")
