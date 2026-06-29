# 엔티티 자동 병합(Entity Resolution) — 임베딩 유사도로 중복 노드를 찾아 병합한다.
import logging

from sklearn.metrics.pairwise import cosine_similarity

import backup_db
from adapters.embedding_adapter import embed_texts
from config import settings
from db import graph_manager, sqlite_manager

logger = logging.getLogger(__name__)


# 한 컬렉션(사업) 안의 엔티티만 둘러보며 유사도 임계값을 넘는 병합 후보 쌍을 찾는다.
# 병합은 컬렉션 내로만 일어난다 — 무관한 사업끼리 자동으로 엮이지 않게.
def find_merge_candidates(collection: str) -> list[tuple[str, str, float]]:
    entities = graph_manager.get_all_entities([collection])
    if len(entities) < 2:
        return []

    texts = [f"{e['name']}: {e['description']}" for e in entities]
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
# 실제로 병합할 게 있을 때만, 되돌릴 수 있도록 실행 직전에 안전 백업을 한 번 만든다.
def run(collections: list[str] | None = None) -> None:
    target_collections = collections or graph_manager.get_all_collections()
    total = 0
    backed_up = False
    for collection in target_collections:
        candidates = find_merge_candidates(collection)
        if not candidates:
            continue
        if not backed_up:
            backup_path = backup_db.create_backup()
            logger.info("병합 작업 전 안전 백업 생성: %s", backup_path)
            backed_up = True
        apply_merges(collection, candidates)
        total += len(candidates)

    if total == 0:
        logger.info("병합 후보가 없습니다.")
    else:
        logger.info("총 %d쌍의 노드를 병합했습니다.", total)
