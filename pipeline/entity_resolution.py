# 엔티티 자동 병합(Entity Resolution) — ①표기 정규화로 공짜 병합 ②임베딩 유사도로 의미 병합.
import logging
import re

from sklearn.metrics.pairwise import cosine_similarity

import backup_db
from adapters.embedding_adapter import embed_texts
from config import settings
from db import graph_manager, sqlite_manager

logger = logging.getLogger(__name__)


# 이름을 비교용 키로 정규화한다 — 공백·구두점·기호를 없애고 소문자로. 한글/영문/숫자만 남긴다.
# 표기만 다른 같은 대상("연료전지 시스템"/"연료전지시스템")을 같은 키로 모으되,
# 구성 문자 자체가 다른 것("GC/FID"≠"GC/FID/TCD")은 키가 달라 섞이지 않는다(안전한 무료 병합).
def _normalize_name(name: str) -> str:
    return re.sub(r"[\s\W_]+", "", name, flags=re.UNICODE).lower()


# 한 컬렉션(사업) 안의 엔티티만 둘러보며 유사도 임계값을 넘는 병합 후보 쌍을 찾는다.
# 병합은 컬렉션 내로만 일어난다 — 무관한 사업끼리 자동으로 엮이지 않게.
def find_merge_candidates(collection: str) -> list[tuple[str, str, float]]:
    entities = graph_manager.get_all_entities([collection])
    if len(entities) < 2:
        return []

    # 임베딩 입력은 '이름만' 쓴다 — 설명문을 섞으면 같은 대상이라도 청크마다 설명이 달라
    # 유사도가 깎여 병합이 안 되는 문제가 있었다(실측: 설명 포함 0.83 → 이름만 0.98).
    texts = [e["name"] for e in entities]
    vectors = embed_texts(texts)
    similarity_matrix = cosine_similarity(vectors)

    candidates: list[tuple[str, str, float]] = []
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            score = similarity_matrix[i][j]
            if score <= settings.merge_similarity_threshold:
                continue
            name_a, name_b = entities[i]["name"], entities[j]["name"]
            if sqlite_manager.is_merge_blacklisted(collection, name_a, name_b):
                logger.info("병합 예외 규칙 적용됨[%s]: %s / %s", collection, name_a, name_b)
                continue
            candidates.append((name_a, name_b, float(score)))
    return candidates


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
