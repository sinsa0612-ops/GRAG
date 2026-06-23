# ChromaDB 벡터 매니저의 카운트/조회 함수를 검증한다 (실제 임베딩 모델은 가짜로 대체).
from db import vector_manager


def _fake_embed_texts(texts):
    return [[0.0, 0.0] for _ in texts]


def test_count_chunks_and_get_all_source_ids(monkeypatch):
    monkeypatch.setattr("db.vector_manager.embed_texts", _fake_embed_texts)

    assert vector_manager.count_chunks() == 0
    assert vector_manager.get_all_source_ids() == set()

    vector_manager.add_chunks("doc_1", ["청크 A", "청크 B"], "c1")
    vector_manager.add_chunks("doc_2", ["청크 C"], "c2")

    assert vector_manager.count_chunks() == 3
    assert vector_manager.get_all_source_ids() == {"doc_1", "doc_2"}

    # 컬렉션 필터로 검색 범위를 좁히면 그 컬렉션의 청크만 후보가 된다.
    assert vector_manager.query_similar("쿼리", top_k=5, collections=["c2"]) == ["청크 C"]
