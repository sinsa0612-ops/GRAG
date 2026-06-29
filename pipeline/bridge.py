# 컬렉션 간 SAME_AS 브릿지 제안 — 서로 다른 사업에 흩어진 '같은 대상'을 임베딩 유사도로 찾아 후보만 내놓는다.
# 컬렉션 내 병합(entity_resolution)과 책임이 다르다: 여기서는 노드를 합치지 않고(파괴적 아님), 컬렉션 경계를
# 넘는 연결선만 '제안'한다. 무관한 사업이 자동으로 엮이지 않도록 실제 연결 여부는 사용자가 결정한다.
import logging

from sklearn.metrics.pairwise import cosine_similarity

from adapters.embedding_adapter import embed_texts
from config import settings
from db import graph_manager

logger = logging.getLogger(__name__)


# 전 컬렉션의 엔티티를 임베딩 유사도로 비교해, '서로 다른 컬렉션' 사이의 브릿지 후보 쌍을 찾는다.
# 임계값을 넘고, 아직 브릿지로 연결되지 않은 쌍만 (컬렉션A, 이름A, 컬렉션B, 이름B, 유사도)로 반환한다.
def find_bridge_candidates(threshold: float | None = None) -> list[tuple[str, str, str, str, float]]:
    threshold = settings.bridge_similarity_threshold if threshold is None else threshold
    entities = graph_manager.get_all_entities()
    if len(entities) < 2:
        return []

    texts = [f"{e['name']}: {e['description']}" for e in entities]
    vectors = embed_texts(texts)
    similarity_matrix = cosine_similarity(vectors)

    candidates: list[tuple[str, str, str, str, float]] = []
    for i in range(len(entities)):
        for j in range(i + 1, len(entities)):
            # 같은 컬렉션 안의 중복은 entity_resolution(병합)의 몫이라 여기서는 건너뛴다.
            if entities[i]["collection"] == entities[j]["collection"]:
                continue
            score = similarity_matrix[i][j]
            if score <= threshold:
                continue
            coll_a, name_a = entities[i]["collection"], entities[i]["name"]
            coll_b, name_b = entities[j]["collection"], entities[j]["name"]
            if graph_manager.is_bridged(coll_a, name_a, coll_b, name_b):
                continue
            candidates.append((coll_a, name_a, coll_b, name_b, float(score)))
    return candidates
