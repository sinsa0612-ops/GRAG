# 컬렉션 간 브릿지 제안(find_bridge_candidates)이 '서로 다른 사업'의 같은 대상만 후보로 내놓는지 확인한다.
import pipeline.bridge as bridge
from db import graph_manager


def _fake_embed(texts):
    # '애플'/'Apple'은 같은 방향 벡터, 그 외는 직교 → 교차 컬렉션의 같은 대상만 유사도가 높다.
    out = []
    for t in texts:
        if "애플" in t or "Apple" in t:
            out.append([1.0, 0.0])
        else:
            out.append([0.0, 1.0])
    return out


def test_find_bridge_candidates_only_cross_collection(monkeypatch):
    monkeypatch.setattr(bridge, "embed_texts", _fake_embed)
    graph_manager.init_schema()
    graph_manager.upsert_entity("사업A", "애플", "ORGANIZATION", "스마트폰 제조사")
    graph_manager.upsert_entity("사업B", "Apple", "ORGANIZATION", "스마트폰 제조사")
    graph_manager.upsert_entity("사업A", "고양이", "OTHER", "동물")

    candidates = bridge.find_bridge_candidates(threshold=0.9)

    pairs = {frozenset(((c[0], c[1]), (c[2], c[3]))) for c in candidates}
    assert frozenset((("사업A", "애플"), ("사업B", "Apple"))) in pairs
    # 같은 컬렉션 안(고양이)·직교 대상은 후보가 아니다.
    assert all("고양이" not in (c[1], c[3]) for c in candidates)


def test_find_bridge_candidates_excludes_already_bridged(monkeypatch):
    monkeypatch.setattr(bridge, "embed_texts", _fake_embed)
    graph_manager.init_schema()
    graph_manager.upsert_entity("사업A", "애플", "ORGANIZATION", "스마트폰 제조사")
    graph_manager.upsert_entity("사업B", "Apple", "ORGANIZATION", "스마트폰 제조사")
    graph_manager.add_bridge("사업A", "애플", "사업B", "Apple")

    assert bridge.find_bridge_candidates(threshold=0.9) == []
