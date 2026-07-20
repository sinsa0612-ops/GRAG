# 커뮤니티 탐지(M2, igraph+leidenalg)가 지켜야 할 불변식을 합성 그래프로 검증한다.
# (a) 커뮤니티는 컬렉션을 절대 넘지 않는다(격벽). (b) 계층(레벨 0..n)이 실제로 형성된다.
# (c) 같은 seed로 재탐지하면 완전히 같은 결과(community_id 포함)가 나온다(결정성).
# (d) SAME_AS 브릿지는 RELATION이 아니므로 탐지에 절대 섞이지 않는다(같은 컬렉션 안에서도).
import pytest

from config import settings
from db import graph_manager
from pipeline import community_detector


# 삼각형(완전연결 3노드) 하나를 만든다. Leiden이 절대 더 못 쪼개는 최소 밀집 단위로 재귀의 '리프' 역할을 한다.
def _add_triangle(collection: str, names: list[str]) -> None:
    for name in names:
        graph_manager.upsert_entity(collection, name, "OTHER", "")
    a, b, c = names
    for x, y in [(a, b), (b, c), (a, c)]:
        graph_manager.upsert_relation(collection, x, y, "RELATED_TO", "", "doc")


# '메가클러스터' n_mega개를 만든다. 각 메가클러스터 = 삼각형 2개 + 그 사이 교차엣지 3개(약하게 결합) —
# 전체 그래프가 충분히 크면(레졸루션 한계) 레벨 0에서는 메가클러스터 단위로만 뭉치고, 그 유도 서브그래프를
# 다시 Leiden에 돌리는 재귀에서야 비로소 두 삼각형으로 갈라진다 — 즉 실제 Leiden 출력으로 계층이 생긴다.
# 메가클러스터끼리는 대표 노드 하나씩을 사슬처럼 엣지 하나로만 이어 전체를 연결 그래프로 만든다.
def _build_mega_graph(collection: str, prefix: str, n_mega: int = 4) -> list[str]:
    all_names: list[str] = []
    reps: list[str] = []
    for m in range(n_mega):
        tri_a = [f"{prefix}M{m}a_{i}" for i in range(3)]
        tri_b = [f"{prefix}M{m}b_{i}" for i in range(3)]
        _add_triangle(collection, tri_a)
        _add_triangle(collection, tri_b)
        # 두 삼각형을 3개의 교차엣지로만 약하게 묶는다(전부 잇지 않음 — 그래야 재귀 시 다시 갈라진다).
        cross_edges = [(a, b) for a in tri_a for b in tri_b][:3]
        for x, y in cross_edges:
            graph_manager.upsert_relation(collection, x, y, "RELATED_TO", "", "doc")
        all_names += tri_a + tri_b
        reps.append(tri_a[0])
    for x, y in zip(reps, reps[1:]):
        graph_manager.upsert_relation(collection, x, y, "RELATED_TO", "", "doc")
    return all_names


# 합성 그래프에서도 재귀가 실제로 일어나도록 임계값을 낮춘다(기본값 30은 이 작은 픽스처엔 너무 크다).
@pytest.fixture(autouse=True)
def _small_thresholds(monkeypatch):
    monkeypatch.setattr(settings, "community_max_size", 4)
    monkeypatch.setattr(settings, "community_max_level", 3)


# (a) 격벽 — 서로 다른 컬렉션에 똑같은 구조를 심어도, 각 컬렉션의 커뮤니티는 그 컬렉션 이름만 담아야 한다.
def test_communities_never_cross_collections():
    graph_manager.init_schema()
    _build_mega_graph("사업A", "a_", n_mega=4)
    _build_mega_graph("사업B", "b_", n_mega=4)

    communities_a, _ = community_detector.detect_communities("사업A")
    communities_b, _ = community_detector.detect_communities("사업B")

    assert communities_a and communities_b
    for community in communities_a:
        assert community["collection"] == "사업A"
        assert all(name.startswith("a_") for name in community["entity_names"])
    for community in communities_b:
        assert community["collection"] == "사업B"
        assert all(name.startswith("b_") for name in community["entity_names"])


# (b) 계층 형성 — 레벨 0(메가클러스터)뿐 아니라 레벨 1(삼각형)까지 실제로 생겨야 한다.
def test_hierarchy_levels_form():
    graph_manager.init_schema()
    all_names = _build_mega_graph("사업A", "a_", n_mega=4)

    communities, _ = community_detector.detect_communities("사업A")

    levels = {c["level"] for c in communities}
    assert levels == {0, 1}

    # 레벨 0의 멤버를 합치면 전체 엔티티 집합과 정확히 같아야 한다(빠짐/중복 없음).
    level0_members = {n for c in communities if c["level"] == 0 for n in c["entity_names"]}
    assert level0_members == set(all_names)

    # 레벨 1(자식) 커뮤니티는 부모 community_id를 갖고, 그 부모는 실제 레벨 0 커뮤니티여야 한다.
    level0_ids = {c["community_id"] for c in communities if c["level"] == 0}
    level1 = [c for c in communities if c["level"] == 1]
    assert level1
    for child in level1:
        assert child["parent_community_id"] in level0_ids
        # 자식 멤버는 자기 부모 커뮤니티의 부분집합이어야 한다(계층 무결성).
        parent = next(c for c in communities if c["community_id"] == child["parent_community_id"])
        assert set(child["entity_names"]) <= set(parent["entity_names"])


# (c) 결정성 — 같은 seed로 다시 탐지하면 community_id까지 완전히 같은 결과가 나와야 한다(재현성).
def test_same_seed_is_deterministic():
    graph_manager.init_schema()
    _build_mega_graph("사업A", "a_", n_mega=4)

    def normalize(comms):
        return sorted((c["level"], c["community_id"], tuple(c["entity_names"])) for c in comms)

    communities_1, sig_1 = community_detector.detect_communities("사업A")
    communities_2, sig_2 = community_detector.detect_communities("사업A")

    assert normalize(communities_1) == normalize(communities_2)
    assert sig_1 == sig_2


# (d-1) SAME_AS 배제 — 같은 컬렉션 안에서도 RELATION이 전혀 없는 두 삼각형을 SAME_AS로만 이으면,
# 그 브릿지가 탐지에 영향을 주지 않아 두 삼각형은 여전히 별개 커뮤니티로 남아야 한다.
def test_same_as_bridge_is_excluded_even_within_same_collection():
    graph_manager.init_schema()
    _add_triangle("사업A", ["x0", "x1", "x2"])
    _add_triangle("사업A", ["y0", "y1", "y2"])
    graph_manager.add_bridge("사업A", "x0", "사업A", "y0")  # RELATION은 없고 SAME_AS만 있음

    communities, _ = community_detector.detect_communities("사업A")

    for community in communities:
        members = set(community["entity_names"])
        assert not ({"x0", "x1", "x2"} <= members and {"y0", "y1", "y2"} <= members)


# (d-2) SAME_AS 배제(컬렉션 간) — 다른 컬렉션 엔티티와 SAME_AS로 이어져 있어도 그 엔티티가 이쪽 탐지 결과에
# 끼어들면 안 된다(격벽과 SAME_AS 배제가 함께 작동하는지 확인).
def test_same_as_bridge_across_collections_does_not_leak_members():
    graph_manager.init_schema()
    graph_manager.upsert_entity("사업A", "김변호사", "Person", "")
    graph_manager.upsert_entity("사업B", "이대리", "Person", "")
    graph_manager.add_bridge("사업A", "김변호사", "사업B", "이대리")

    communities, _ = community_detector.detect_communities("사업A")

    all_members = {n for c in communities for n in c["entity_names"]}
    assert all_members == {"김변호사"}


# 엔티티가 전혀 없는 컬렉션은 빈 목록을 돌려주되, graph_signature는 계산돼야 한다(빈 그래프도 서명 가능).
def test_empty_collection_returns_no_communities_but_has_signature():
    graph_manager.init_schema()
    communities, graph_signature = community_detector.detect_communities("빈사업")

    assert communities == []
    assert graph_signature  # 빈 문자열이 아닌 해시값
