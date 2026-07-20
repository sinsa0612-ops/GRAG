# 커뮤니티 탐지(M2) — igraph+leidenalg로 컬렉션별 계층(Leiden) 커뮤니티를 찾는다.
# 순수 CPU 연산이라 LLM 호출이 전혀 없다(요약/리포트는 M3, 글로벌 검색은 M4 — 이 모듈은 탐지만 한다).
# 격벽: 반드시 한 컬렉션의 Entity + RELATION 엣지만으로 그래프를 구성한다. SAME_AS는 graph_manager.
# get_all_relations()가 애초에 RELATION 타입 관계만 반환하므로(Kuzu는 SAME_AS를 별도 REL 테이블로 분리해
# 저장한다) 이 모듈에 절대 섞여 들어오지 않는다 — 코드로 다시 걸러낼 필요가 없는 구조적 보장이다.
import hashlib
import json

import igraph
import leidenalg

from config import settings
from db import graph_manager


# 컬렉션의 엔티티 이름 집합 + 관계(source, predicate, target, valid_from) 집합을 해시해 그래프 서명을 만든다.
# 탐지 이후 그래프가 바뀌었는지(재빌드 필요 여부) 판단하는 데 쓴다(M5 증분 재계산의 재료).
def _compute_graph_signature(collection: str, entity_names: list[str], relations: list[dict]) -> str:
    relation_keys = sorted(
        f"{r['source']}|{r['predicate']}|{r['target']}|{r['valid_from']}" for r in relations
    )
    payload = json.dumps(
        {"collection": collection, "entities": sorted(entity_names), "relations": relation_keys},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# community_id를 (collection, level, 정렬된 멤버 엔티티명 집합)의 결정적 해시로 부여한다(콘텐츠 주소화).
# 랜덤/순번이 아니므로 재탐지 후에도 멤버셋이 같으면 같은 id가 나온다 — 그래야 "이 커뮤니티는 안 바뀌었다"를
# 멤버십만 보고 판정할 수 있다(M5가 이 id로 재요약 필요 여부를 가른다).
def _compute_community_id(collection: str, level: int, member_names: list[str]) -> str:
    payload = json.dumps(
        {"collection": collection, "level": level, "members": sorted(member_names)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# 엔티티 이름 목록 + 관계 목록으로 igraph 그래프를 만든다. 정점은 이름 자체를 vs["name"]으로 갖는다
# (induced_subgraph로 잘라내도 이름이 그대로 딸려가, 재귀 중에 별도 인덱스 매핑을 유지할 필요가 없다).
def _build_igraph(names: list[str], relations: list[dict]) -> igraph.Graph:
    g = igraph.Graph()
    g.add_vertices(names)
    name_set = set(names)
    edges = [
        (r["source"], r["target"])
        for r in relations
        if r["source"] in name_set and r["target"] in name_set
    ]
    if edges:
        g.add_edges(edges)
    return g


# 주어진 (서브)그래프에 Leiden을 한 번 돌려 커뮤니티(정점 인덱스 묶음)들을 반환한다.
# seed를 고정해, 같은 그래프에 대해서는 몇 번을 다시 돌려도 같은 결과가 나오게 한다(결정성).
def _partition(graph: igraph.Graph) -> list[list[int]]:
    if graph.vcount() == 0:
        return []
    if graph.vcount() == 1:
        return [[0]]
    partition = leidenalg.find_partition(
        graph, leidenalg.RBConfigurationVertexPartition, seed=settings.leiden_seed
    )
    return [list(cluster) for cluster in partition]


# clusters(현재 그래프에 대해 이미 계산된 파티션)를 커뮤니티 레코드로 기록하고, 크기 임계를 넘는
# 커뮤니티는 그 유도 서브그래프에 Leiden을 다시 돌려 하위 레벨(level+1)로 재귀 확장한다(계층 형성).
# Leiden이 더 못 쪼개는 경우(서브파티션이 1개뿐)는 그대로 리프로 확정해 무한 재귀를 막는다.
def _expand(
    graph: igraph.Graph,
    clusters: list[list[int]],
    level: int,
    parent_id: str | None,
    collection: str,
    graph_signature: str,
    out: list[dict],
) -> None:
    for cluster_indices in clusters:
        members = sorted(graph.vs[i]["name"] for i in cluster_indices)
        community_id = _compute_community_id(collection, level, members)
        out.append(
            {
                "collection": collection,
                "community_id": community_id,
                "level": level,
                "parent_community_id": parent_id,
                "entity_names": members,
                "size": len(members),
                "graph_signature": graph_signature,
            }
        )

        if len(members) <= settings.community_max_size or level >= settings.community_max_level:
            continue  # 리프 — 더 쪼개지 않는다

        subgraph = graph.induced_subgraph(cluster_indices)
        sub_clusters = _partition(subgraph)
        if len(sub_clusters) <= 1:
            continue  # Leiden이 더 못 쪼갬 — 여기서 멈춰 무한 재귀 방지, 리프로 확정

        _expand(subgraph, sub_clusters, level + 1, community_id, collection, graph_signature, out)


# 이미 가져온 엔티티/관계 데이터로 커뮤니티를 탐지하는 순수 함수(DB 미접근 — 합성 그래프로 빠르게 단위테스트 가능).
# entities/relations는 이미 해당 collection 범위로 좁혀진 것을 넘겨받는다고 가정한다.
# 반환: (레벨 0..n의 커뮤니티 레코드 목록, 이번 탐지에 쓰인 graph_signature).
def detect_communities_from_graph(
    collection: str, entities: list[dict], relations: list[dict]
) -> tuple[list[dict], str]:
    names = sorted({e["name"] for e in entities if e["collection"] == collection})
    graph_signature = _compute_graph_signature(collection, names, relations)
    if not names:
        return [], graph_signature

    igraph_graph = _build_igraph(names, relations)
    top_clusters = _partition(igraph_graph)
    out: list[dict] = []
    _expand(igraph_graph, top_clusters, 0, None, collection, graph_signature, out)
    return out, graph_signature


# 지정한 컬렉션의 엔티티+RELATION 엣지를 graph_manager에서 가져와 커뮤니티를 탐지한다(CLI `communities build`가 호출).
def detect_communities(collection: str) -> tuple[list[dict], str]:
    entities = graph_manager.get_all_entities([collection])
    relations = graph_manager.get_all_relations([collection])
    return detect_communities_from_graph(collection, entities, relations)
