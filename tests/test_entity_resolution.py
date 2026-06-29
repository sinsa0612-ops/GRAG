# 엔티티 자동 병합(entity_resolution)이 컬렉션 내에서 유사도 임계값과 블랙리스트를 올바르게 반영하는지 확인한다.
import pipeline.entity_resolution as entity_resolution
from db import graph_manager, sqlite_manager

C = "c1"


# 텍스트 내용에 따라 의도적으로 비슷하거나 다른 벡터를 돌려주는 가짜 임베딩 함수.
def _fake_embed_texts(texts):
    vectors = []
    for text in texts:
        if "애플" in text or "Apple" in text:
            vectors.append([1.0, 0.01])
        else:
            vectors.append([0.0, 1.0])
    return vectors


def test_find_merge_candidates_detects_duplicates(monkeypatch):
    monkeypatch.setattr(entity_resolution, "embed_texts", _fake_embed_texts)
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "애플", "Asset", "스마트폰 제조사")
    graph_manager.upsert_entity(C, "Apple", "Asset", "스마트폰 제조사 기업")
    graph_manager.upsert_entity(C, "고양이", "Animal", "집에서 기르는 동물")

    candidates = entity_resolution.find_merge_candidates(C)

    pairs = {frozenset((a, b)) for a, b, _ in candidates}
    assert frozenset(("애플", "Apple")) in pairs
    assert all("고양이" not in pair for pair in pairs)


def test_blacklist_prevents_merge(monkeypatch):
    monkeypatch.setattr(entity_resolution, "embed_texts", _fake_embed_texts)
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "애플", "Asset", "스마트폰 제조사")
    graph_manager.upsert_entity(C, "Apple", "Asset", "스마트폰 제조사 기업")
    sqlite_manager.add_merge_blacklist(C, "애플", "Apple", "사용자가 직접 분리 지정")

    candidates = entity_resolution.find_merge_candidates(C)

    assert candidates == []


def test_run_actually_merges_in_graph(monkeypatch):
    monkeypatch.setattr(entity_resolution, "embed_texts", _fake_embed_texts)
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "애플", "Asset", "스마트폰 제조사")
    graph_manager.upsert_entity(C, "Apple", "Asset", "스마트폰 제조사 기업")

    entity_resolution.run()

    names = {e["name"] for e in graph_manager.get_all_entities()}
    assert len(names & {"애플", "Apple"}) == 1


def test_run_creates_backup_before_merging(monkeypatch):
    monkeypatch.setattr(entity_resolution, "embed_texts", _fake_embed_texts)
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "애플", "Asset", "스마트폰 제조사")
    graph_manager.upsert_entity(C, "Apple", "Asset", "스마트폰 제조사 기업")

    backup_calls = []
    monkeypatch.setattr(entity_resolution.backup_db, "create_backup", lambda: backup_calls.append(1))

    entity_resolution.run()

    assert len(backup_calls) == 1


def test_run_skips_backup_when_no_candidates(monkeypatch):
    monkeypatch.setattr(entity_resolution, "embed_texts", _fake_embed_texts)
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "애플", "Asset", "스마트폰 제조사")
    graph_manager.upsert_entity(C, "고양이", "Animal", "동물")

    backup_calls = []
    monkeypatch.setattr(entity_resolution.backup_db, "create_backup", lambda: backup_calls.append(1))

    entity_resolution.run()

    assert backup_calls == []


def test_merge_only_happens_within_a_collection(monkeypatch):
    # 다른 컬렉션의 비슷한 엔티티는 자동 병합되지 않아야 한다(사업 간 격벽).
    monkeypatch.setattr(entity_resolution, "embed_texts", _fake_embed_texts)
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity("사업A", "애플", "Asset", "스마트폰 제조사")
    graph_manager.upsert_entity("사업B", "Apple", "Asset", "스마트폰 제조사 기업")

    entity_resolution.run()

    # 컬렉션이 다르므로 둘 다 살아남아야 한다.
    assert graph_manager.count_entities() == 2


def test_merge_records_dropped_name_as_alias(monkeypatch):
    # 병합으로 사라진 이름이 살아남은 엔티티의 alias로 남아야, 다음에 같은 표현이
    # 또 나왔을 때 느린 임베딩 비교 없이 즉시 같은 엔티티로 인식할 수 있다.
    monkeypatch.setattr(entity_resolution, "embed_texts", _fake_embed_texts)
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "애플", "Asset", "스마트폰 제조사")
    graph_manager.upsert_entity(C, "Apple", "Asset", "스마트폰 제조사 기업")

    entity_resolution.run()

    survivor = (graph_manager.get_all_entities())[0]["name"]
    dropped = "Apple" if survivor == "애플" else "애플"
    assert graph_manager.find_canonical_name(C, dropped) == survivor
