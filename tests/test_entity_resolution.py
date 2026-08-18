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


# --- 검토 회색대(반자동 병합) ---
# 실측 재현: 이름 임베딩만으로는 '합쳐야 하는 쌍'과 '합치면 안 되는 쌍'이 같은 점수대에 섞인다.
# 그래서 회색대는 자동 판정하지 않고 승인 목록으로 나와야 한다.
def _gray_band_embed_texts(texts):
    # 국민연금공단(1,0) 기준: 기금 0.87(합쳐야 함), 건강보험공단 0.88(합치면 안 됨) — 역전 상황을 그대로 만든다.
    # 공단을 기준축에 두고, 기금(0.87)·건강보험공단(0.88)을 반대편으로 벌려 서로는 0.53으로 떨어뜨린다
    # — 회색대에 걸리는 쌍이 정확히 '공단↔기금'과 '공단↔건강보험공단' 둘뿐이게 만들기 위함.
    table = {
        "국민연금공단": [1.0, 0.0, 0.0, 0.0],
        "국민연금기금": [0.87, 0.493, 0.0, 0.0],
        "국민건강보험공단": [0.88, -0.475, 0.0, 0.0],
        "고양이": [0.0, 0.0, 1.0, 0.0],
        "카카오": [0.0, 0.0, 0.0, 1.0],
    }
    return [table[text] for text in texts]


def _seed_gray_band():
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "국민연금공단", "ORGANIZATION", "카카오 주요 주주")
    graph_manager.upsert_entity(C, "국민연금기금", "ORGANIZATION", "네이버 주요 주주")
    graph_manager.upsert_entity(C, "고양이", "OTHER", "동물")
    graph_manager.upsert_entity(C, "카카오", "ORGANIZATION", "IT 기업")
    graph_manager.upsert_relation(C, "국민연금공단", "카카오", "HOLDS_STAKE", source_doc="kakao.md")


def test_gray_band_pairs_are_not_auto_merged(monkeypatch):
    monkeypatch.setattr(entity_resolution, "embed_texts", _gray_band_embed_texts)
    _seed_gray_band()

    # 0.87은 자동 병합 임계값(0.92) 아래라 자동으로는 합쳐지지 않아야 한다.
    assert entity_resolution.find_merge_candidates(C) == []


def test_find_review_candidates_surfaces_gray_band_with_context(monkeypatch):
    monkeypatch.setattr(entity_resolution, "embed_texts", _gray_band_embed_texts)
    _seed_gray_band()

    reviews = entity_resolution.find_review_candidates(C)

    pairs = {frozenset((r["keep"], r["drop"])) for r in reviews}
    assert frozenset(("국민연금공단", "국민연금기금")) in pairs
    assert all("고양이" not in pair for pair in pairs)

    # 연결이 많은 쪽이 보존되고, 판단 근거(설명·관계)가 함께 실려야 사용자가 가를 수 있다.
    review = next(r for r in reviews if r["drop"] == "국민연금기금")
    assert review["keep"] == "국민연금공단"
    assert review["keep_context"]["relations"] == ["국민연금공단 -[HOLDS_STAKE]-> 카카오"]
    assert review["drop_context"]["description"] == "네이버 주요 주주"


def test_review_approval_merges_and_rejection_is_remembered(monkeypatch):
    monkeypatch.setattr(entity_resolution, "embed_texts", _gray_band_embed_texts)
    monkeypatch.setattr(entity_resolution.backup_db, "create_backup", lambda: "(백업 생략)")
    _seed_gray_band()
    graph_manager.upsert_entity(C, "국민건강보험공단", "ORGANIZATION", "건강보험 운영")

    entity_resolution.apply_review_decisions(
        C,
        approved=[("국민연금공단", "국민연금기금")],
        rejected=[("국민연금공단", "국민건강보험공단")],
    )

    # 승인: 노드가 합쳐지고 사라진 표기는 alias로 남아 다음 문서에서 바로 같은 대상으로 인식된다.
    assert graph_manager.get_entity(C, "국민연금기금") is None
    assert "국민연금기금" in graph_manager.get_entity(C, "국민연금공단")["aliases"]
    # 거부: 블랙리스트에 남아 다시 후보로 올라오지 않는다.
    assert sqlite_manager.is_merge_blacklisted(C, "국민연금공단", "국민건강보험공단")
    assert entity_resolution.find_review_candidates(C) == []


# 실제 임베딩 모델(bge-m3)로 회색대 설계의 전제를 고정한다 — 가짜 벡터로는 증명되지 않는 부분.
# 전제: '합쳐야 하는 쌍'(국민연금공단↔국민연금기금)과 '합치면 안 되는 쌍'(↔국민건강보험공단)의
# 점수가 역전돼 있어, 임계값 하나로는 절대 갈리지 않는다. 그래서 둘 다 자동 병합이 아니라 검토로 가야 한다.
# conftest.py가 이름의 real_embedding을 보고 GRAG_RUN_LLM_SMOKE=1 일 때만 실행한다.
def test_gray_band_holds_for_real_korean_org_names_real_embedding():
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    for name, desc in (
        ("국민연금공단", "카카오 주요 주주"),
        ("국민연금기금", "네이버 주요 주주"),
        ("국민건강보험공단", "건강보험 운영 기관"),
    ):
        graph_manager.upsert_entity(C, name, "ORGANIZATION", desc)

    # 어느 쌍도 자동 병합되지 않는다 — 자동으로 합쳤다면 건강보험공단까지 삼켰을 것이다.
    assert entity_resolution.find_merge_candidates(C) == []

    reviews = entity_resolution.find_review_candidates(C)
    pairs = {frozenset((r["keep"], r["drop"])) for r in reviews}
    assert frozenset(("국민연금공단", "국민연금기금")) in pairs
    assert frozenset(("국민연금공단", "국민건강보험공단")) in pairs

    # 역전 확인: 합치면 안 되는 쌍이 합쳐야 하는 쌍보다 점수가 높다(= 기계가 가를 수 없다는 증거).
    score = {frozenset((r["keep"], r["drop"])): r["score"] for r in reviews}
    assert score[frozenset(("국민연금공단", "국민건강보험공단"))] > score[frozenset(("국민연금공단", "국민연금기금"))]
