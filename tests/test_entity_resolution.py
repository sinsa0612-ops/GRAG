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


def test_run_marks_communities_dirty_only_when_merge_happens(monkeypatch):
    # [M2] 수동 병합(정규화/임베딩 둘 다)과 blacklist 해제 후 재병합은 모두 이 run()을 거치므로,
    # 실제로 병합이 일어난 컬렉션만 dirty로 표시되고 아무것도 안 바뀐 컬렉션은 건드리지 않아야 한다.
    monkeypatch.setattr(entity_resolution, "embed_texts", _fake_embed_texts)
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity("사업A", "애플", "Asset", "스마트폰 제조사")
    graph_manager.upsert_entity("사업A", "Apple", "Asset", "스마트폰 제조사 기업")
    graph_manager.upsert_entity("사업B", "고양이", "Animal", "동물")  # 병합 후보 없음
    sqlite_manager.clear_communities_dirty("사업A", "이전-서명")
    sqlite_manager.clear_communities_dirty("사업B", "이전-서명")

    entity_resolution.run()

    assert sqlite_manager.is_communities_dirty("사업A") is True
    assert sqlite_manager.is_communities_dirty("사업B") is False


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


def test_normalize_name_collapses_spacing_and_symbols():
    # 공백·구두점·대소문자만 다른 표기는 같은 키, 구성 문자가 다르면 다른 키여야 한다.
    assert entity_resolution._normalize_name("연료전지 시스템") == entity_resolution._normalize_name(
        "연료전지시스템"
    )
    assert entity_resolution._normalize_name("GC/FID") != entity_resolution._normalize_name(
        "GC/FID/TCD"
    )


def test_strip_trailing_josa_collapses_particle_but_protects_short_names():
    # 3글자 이상 이름 끝 조사는 떼고(길동이->길동), 2글자 짧은 이름은 오삭제하지 않는다(순이->순이).
    assert entity_resolution.strip_trailing_josa("길동이") == "길동"
    assert entity_resolution.strip_trailing_josa("홍길동이") == "홍길동"
    assert entity_resolution.strip_trailing_josa("점순이") == "점순"
    assert entity_resolution.strip_trailing_josa("순이") == "순이"  # 2글자 보호
    assert entity_resolution.strip_trailing_josa("감자") == "감자"  # 조사 아님
    # 정규화 키도 조사 변형을 같은 키로 모은다(길동/길동이는 병합, 성 붙은 홍길동은 별개).
    assert entity_resolution._normalize_name("길동이") == entity_resolution._normalize_name("길동")
    assert entity_resolution._normalize_name("홍길동") != entity_resolution._normalize_name("길동")


def test_find_normalized_duplicates_groups_spacing_variants():
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "연료전지 시스템", "OBJECT", "개발 대상")
    graph_manager.upsert_entity(C, "연료전지시스템", "OBJECT", "평가 대상")
    graph_manager.upsert_entity(C, "고양이", "OTHER", "동물")

    pairs = entity_resolution.find_normalized_duplicates(C)

    assert len(pairs) == 1
    keep, drop = pairs[0]
    assert {keep, drop} == {"연료전지 시스템", "연료전지시스템"}
    assert all("고양이" not in pair for pair in pairs)


def test_find_normalized_duplicates_keeps_most_connected_node():
    # 연결이 많은(중심적인) 노드가 보존되고, 덜 연결된 표기가 그쪽으로 합쳐져야 한다.
    # (띄어쓰기만 다른 같은 키. 연결을 더 적게 가진 표기여도 degree가 높으면 보존된다.)
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "연료전지 시스템", "OBJECT", "개발 대상")
    graph_manager.upsert_entity(C, "연료전지시스템", "OBJECT", "평가 대상")
    graph_manager.upsert_entity(C, "연구원", "PERSON", "연구자")
    # 띄어쓰기 없는 '연료전지시스템'에만 관계를 달아 연결 수를 높인다.
    graph_manager.upsert_relation(C, "연구원", "연료전지시스템", "DEVELOPED", "", "doc1")

    pairs = entity_resolution.find_normalized_duplicates(C)

    assert pairs == [("연료전지시스템", "연료전지 시스템")]


def test_run_merges_normalized_variants_without_embedding(monkeypatch):
    # 표기만 다른 중복은 임베딩 호출 없이도 정규화 단계에서 병합돼야 한다.
    def boom(texts):
        raise AssertionError("정규화 병합은 임베딩을 호출하면 안 된다")

    monkeypatch.setattr(entity_resolution, "embed_texts", boom)
    monkeypatch.setattr(entity_resolution.backup_db, "create_backup", lambda: None)
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "한국가스안전공사", "ORGANIZATION", "공공기관")
    graph_manager.upsert_entity(C, "한국 가스안전공사", "ORGANIZATION", "안전 기관")

    entity_resolution.run([C])

    names = {e["name"] for e in graph_manager.get_all_entities([C])}
    assert names == {"한국가스안전공사"}


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
