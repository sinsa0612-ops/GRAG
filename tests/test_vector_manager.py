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


def test_count_chunks_respects_collection_scope(monkeypatch):
    # status가 컬렉션 범위를 지정하면 청크 수도 그 사업만 세어야 한다(다른 집계와 숫자 일치).
    monkeypatch.setattr("db.vector_manager.embed_texts", _fake_embed_texts)

    vector_manager.add_chunks("doc_1", ["청크 A", "청크 B"], "c1")
    vector_manager.add_chunks("doc_2", ["청크 C"], "c2")

    assert vector_manager.count_chunks() == 3
    assert vector_manager.count_chunks(["c1"]) == 2
    assert vector_manager.count_chunks(["c2"]) == 1
    assert vector_manager.count_chunks(["c1", "c2"]) == 3


def test_add_chunks_is_idempotent_on_reingest_with_same_source_id(monkeypatch):
    # 재개 가능 인제스트(B)가 크래시 후 같은 source_id로 add_chunks를 다시 호출한다(청크 id가 이미 존재).
    # 카운트만으로는 add/upsert를 구분 못 한다 — 이 chromadb 버전의 add()는 중복 id를 에러 없이 '조용히
    # 무시'해 옛 내용을 그대로 남기므로(실측 확인), upsert의 증거는 내용이 실제로 최신으로 갱신되는지로
    # 확인해야 한다(implementation_plan.md ③ 정합성 1 — 재개 시 재add해도 최신 내용을 반영해야 함).
    monkeypatch.setattr("db.vector_manager.embed_texts", _fake_embed_texts)

    vector_manager.add_chunks("doc_1", ["청크 A", "청크 B"], "c1")
    assert vector_manager.count_chunks() == 2

    vector_manager.add_chunks("doc_1", ["갱신된 청크 A", "갱신된 청크 B"], "c1")  # 같은 source_id로 재호출(재개 시나리오)

    assert vector_manager.count_chunks() == 2  # 청크 수가 늘어나지 않음(중복 id)
    collection = vector_manager._get_collection()
    stored = collection.get(ids=["doc_1_chunk_0", "doc_1_chunk_1"], include=["documents"])
    assert set(stored["documents"]) == {"갱신된 청크 A", "갱신된 청크 B"}  # upsert라서 최신 내용으로 갱신됨
