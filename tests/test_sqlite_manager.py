# SQLite Master DB 매니저(컬렉션별 문서 해시 추적, 병합 블랙리스트)를 검증한다.
from db import sqlite_manager

C = "c1"


def test_document_hash_roundtrip():
    sqlite_manager.init_schema()
    assert sqlite_manager.get_document_hash(C, "memo.md") is None

    sqlite_manager.upsert_document("doc_1", C, "memo.md", "내용", "hash123")
    assert sqlite_manager.get_document_hash(C, "memo.md") == "hash123"
    assert sqlite_manager.get_document_source_id(C, "memo.md") == "doc_1"


def test_merge_blacklist_is_order_independent():
    sqlite_manager.init_schema()
    sqlite_manager.add_merge_blacklist(C, "애플", "Apple", "다른 의미")

    assert sqlite_manager.is_merge_blacklisted(C, "애플", "Apple") is True
    assert sqlite_manager.is_merge_blacklisted(C, "Apple", "애플") is True
    assert sqlite_manager.is_merge_blacklisted(C, "애플", "고양이") is False


def test_merge_blacklist_is_collection_scoped():
    # 한 사업의 병합 금지 규칙이 다른 사업으로 새지 않아야 한다(격벽).
    sqlite_manager.init_schema()
    sqlite_manager.add_merge_blacklist("사업A", "애플", "Apple", "다른 의미")

    assert sqlite_manager.is_merge_blacklisted("사업A", "애플", "Apple") is True
    assert sqlite_manager.is_merge_blacklisted("사업B", "애플", "Apple") is False


def test_delete_document_and_count():
    sqlite_manager.init_schema()
    assert sqlite_manager.count_documents() == 0

    sqlite_manager.upsert_document("doc_1", C, "memo.md", "내용", "hash123")
    assert sqlite_manager.count_documents() == 1

    sqlite_manager.delete_document(C, "memo.md")
    assert sqlite_manager.count_documents() == 0
    assert sqlite_manager.get_document_hash(C, "memo.md") is None


def test_upsert_same_file_name_keeps_single_current_row():
    # 같은 컬렉션에서 같은 파일을 새 source_id로 다시 upsert하면 옛 행이 사라지고 현재 버전 하나만 남아야 한다.
    sqlite_manager.init_schema()
    sqlite_manager.upsert_document("doc_1", C, "memo.md", "첫 내용", "hash1")
    sqlite_manager.upsert_document("doc_2", C, "memo.md", "둘째 내용", "hash2")

    assert sqlite_manager.count_documents() == 1
    assert sqlite_manager.get_document_source_id(C, "memo.md") == "doc_2"
    assert sqlite_manager.get_document_hash(C, "memo.md") == "hash2"
    assert sqlite_manager.get_all_source_ids() == {"doc_2"}


def test_same_file_name_in_different_collections_coexist():
    # 같은 파일명이라도 컬렉션이 다르면 별개 문서로 공존해야 한다(사업 간 격벽).
    sqlite_manager.init_schema()
    sqlite_manager.upsert_document("doc_a", "사업A", "memo.md", "A 내용", "hashA")
    sqlite_manager.upsert_document("doc_b", "사업B", "memo.md", "B 내용", "hashB")

    assert sqlite_manager.count_documents() == 2
    assert sqlite_manager.get_document_source_id("사업A", "memo.md") == "doc_a"
    assert sqlite_manager.get_document_source_id("사업B", "memo.md") == "doc_b"
    assert sqlite_manager.get_collection_doc_counts() == {"사업A": 1, "사업B": 1}


def test_documents_table_has_file_name_index():
    sqlite_manager.init_schema()
    with sqlite_manager.get_connection() as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_documents_file_name'"
        ).fetchone()
    assert row is not None
    assert "file_name" in row[0]


def test_get_all_source_ids():
    sqlite_manager.init_schema()
    assert sqlite_manager.get_all_source_ids() == set()

    sqlite_manager.upsert_document("doc_1", C, "memo.md", "내용", "hash1")
    sqlite_manager.upsert_document("doc_2", C, "memo2.md", "내용2", "hash2")
    assert sqlite_manager.get_all_source_ids() == {"doc_1", "doc_2"}


def test_api_usage_accumulates_per_day():
    sqlite_manager.init_schema()
    assert sqlite_manager.get_api_usage_today() == 0

    sqlite_manager.record_api_usage(91)
    sqlite_manager.record_api_usage(9)
    assert sqlite_manager.get_api_usage_today() == 100

    sqlite_manager.record_api_usage(0)  # 0 이하는 무시
    assert sqlite_manager.get_api_usage_today() == 100


def test_collection_hierarchy_descendants():
    # 부모 이름을 주면 자기 자신 + 모든 하위(자손)를 펼쳐야 한다(본부 단위 범위 조회).
    sqlite_manager.init_schema()
    sqlite_manager.set_collection_parent("사업A", "본부")
    sqlite_manager.set_collection_parent("사업B", "본부")
    sqlite_manager.set_collection_parent("팀A1", "사업A")

    assert set(sqlite_manager.get_collection_descendants("본부")) == {"본부", "사업A", "사업B", "팀A1"}
    assert set(sqlite_manager.get_collection_descendants("사업A")) == {"사업A", "팀A1"}
    # 계층에 없는 컬렉션은 자기 자신만 반환된다.
    assert sqlite_manager.get_collection_descendants("독립사업") == ["독립사업"]


def test_collection_parent_set_and_unset():
    sqlite_manager.init_schema()
    sqlite_manager.set_collection_parent("사업A", "본부")
    assert sqlite_manager.get_collection_parent("사업A") == "본부"
    assert sqlite_manager.get_collection_children("본부") == ["사업A"]

    # 부모를 바꾸면 덮어써진다.
    sqlite_manager.set_collection_parent("사업A", "신본부")
    assert sqlite_manager.get_collection_parent("사업A") == "신본부"

    sqlite_manager.unset_collection_parent("사업A")
    assert sqlite_manager.get_collection_parent("사업A") is None
    assert sqlite_manager.get_collection_children("본부") == []


def test_list_and_remove_merge_blacklist():
    sqlite_manager.init_schema()
    sqlite_manager.add_merge_blacklist(C, "애플", "Apple", "다른 의미")

    listed = sqlite_manager.list_merge_blacklist()
    assert listed == [{"collection": C, "node_a": "애플", "node_b": "Apple", "reason": "다른 의미"}]

    sqlite_manager.remove_merge_blacklist(C, "Apple", "애플")
    assert sqlite_manager.list_merge_blacklist() == []
    assert sqlite_manager.is_merge_blacklisted(C, "애플", "Apple") is False


# --- M1.5: 엔티티 설명 후보(entity_desc_candidates) ---


def test_upsert_and_get_desc_candidates():
    sqlite_manager.init_schema()
    assert sqlite_manager.get_desc_candidates(C, "강택리") == []

    sqlite_manager.upsert_desc_candidate(C, "강택리", "doc_a", "기획자")
    sqlite_manager.upsert_desc_candidate(C, "강택리", "doc_b", "ISA계좌 운영자")

    assert sqlite_manager.get_desc_candidates(C, "강택리") == ["기획자", "ISA계좌 운영자"]


def test_upsert_desc_candidate_same_doc_replaces_not_accumulates():
    # 같은 문서가 같은 엔티티를 다시 언급하면(같은 문서 안 다른 청크) 그 문서 몫 1행만 최신으로 갱신돼야 한다.
    sqlite_manager.init_schema()
    sqlite_manager.upsert_desc_candidate(C, "강택리", "doc_a", "1차 설명")
    sqlite_manager.upsert_desc_candidate(C, "강택리", "doc_a", "2차(갱신) 설명")

    assert sqlite_manager.get_desc_candidates(C, "강택리") == ["2차(갱신) 설명"]


def test_desc_candidates_are_collection_scoped():
    sqlite_manager.init_schema()
    sqlite_manager.upsert_desc_candidate("사업A", "김변호사", "doc_a", "A쪽 설명")
    sqlite_manager.upsert_desc_candidate("사업B", "김변호사", "doc_b", "B쪽 설명")

    assert sqlite_manager.get_desc_candidates("사업A", "김변호사") == ["A쪽 설명"]
    assert sqlite_manager.get_desc_candidates("사업B", "김변호사") == ["B쪽 설명"]


def test_get_entities_with_min_candidates():
    sqlite_manager.init_schema()
    sqlite_manager.upsert_desc_candidate(C, "다중후보", "doc_a", "설명1")
    sqlite_manager.upsert_desc_candidate(C, "다중후보", "doc_b", "설명2")
    sqlite_manager.upsert_desc_candidate(C, "단일후보", "doc_c", "설명3")

    assert sqlite_manager.get_entities_with_min_candidates(C, 2) == ["다중후보"]
    assert set(sqlite_manager.get_entities_with_min_candidates(C, 1)) == {"다중후보", "단일후보"}


def test_delete_desc_candidates_by_source_doc_removes_only_that_doc():
    sqlite_manager.init_schema()
    sqlite_manager.upsert_desc_candidate(C, "강택리", "doc_a", "옛 문서 설명")
    sqlite_manager.upsert_desc_candidate(C, "강택리", "doc_b", "새 문서 설명")

    sqlite_manager.delete_desc_candidates_by_source_doc("doc_a")

    assert sqlite_manager.get_desc_candidates(C, "강택리") == ["새 문서 설명"]


def test_delete_desc_candidates_by_collection_scoped():
    sqlite_manager.init_schema()
    sqlite_manager.upsert_desc_candidate("사업A", "김변호사", "doc_a", "A쪽 설명")
    sqlite_manager.upsert_desc_candidate("사업B", "김변호사", "doc_b", "B쪽 설명")

    sqlite_manager.delete_desc_candidates_by_collection("사업A")

    assert sqlite_manager.get_desc_candidates("사업A", "김변호사") == []
    assert sqlite_manager.get_desc_candidates("사업B", "김변호사") == ["B쪽 설명"]


# --- M2: communities(커뮤니티 탐지 결과) ---


def _sample_communities(collection: str) -> list[dict]:
    return [
        {
            "collection": collection,
            "community_id": "root1",
            "level": 0,
            "parent_community_id": None,
            "entity_names": ["A", "B", "C"],
            "size": 3,
            "graph_signature": "sig1",
        },
        {
            "collection": collection,
            "community_id": "child1",
            "level": 1,
            "parent_community_id": "root1",
            "entity_names": ["A", "B"],
            "size": 2,
            "graph_signature": "sig1",
        },
    ]


def test_replace_communities_stores_and_retrieves():
    sqlite_manager.init_schema()
    assert sqlite_manager.get_communities(C) == []

    sqlite_manager.replace_communities(C, _sample_communities(C))

    stored = sqlite_manager.get_communities(C)
    assert len(stored) == 2
    root = next(c for c in stored if c["community_id"] == "root1")
    assert root == {
        "collection": C,
        "community_id": "root1",
        "level": 0,
        "parent_community_id": None,
        "entity_names": ["A", "B", "C"],
        "size": 3,
        "graph_signature": "sig1",
    }


def test_get_communities_filters_by_level():
    sqlite_manager.init_schema()
    sqlite_manager.replace_communities(C, _sample_communities(C))

    level0 = sqlite_manager.get_communities(C, level=0)
    level1 = sqlite_manager.get_communities(C, level=1)

    assert [c["community_id"] for c in level0] == ["root1"]
    assert [c["community_id"] for c in level1] == ["child1"]


def test_replace_communities_overwrites_stale_rows():
    # 재탐지 결과로 통째로 교체돼야 한다 — 옛 커뮤니티(멤버가 바뀌어 사라진 것)가 유령으로 남으면 안 된다.
    sqlite_manager.init_schema()
    sqlite_manager.replace_communities(C, _sample_communities(C))

    new_communities = [
        {
            "collection": C,
            "community_id": "fresh1",
            "level": 0,
            "parent_community_id": None,
            "entity_names": ["X"],
            "size": 1,
            "graph_signature": "sig2",
        }
    ]
    sqlite_manager.replace_communities(C, new_communities)

    stored = sqlite_manager.get_communities(C)
    assert [c["community_id"] for c in stored] == ["fresh1"]


def test_communities_are_collection_scoped():
    sqlite_manager.init_schema()
    sqlite_manager.replace_communities("사업A", _sample_communities("사업A"))
    sqlite_manager.replace_communities("사업B", _sample_communities("사업B"))

    sqlite_manager.delete_communities_by_collection("사업A")

    assert sqlite_manager.get_communities("사업A") == []
    assert len(sqlite_manager.get_communities("사업B")) == 2


def test_communities_dirty_flag_defaults_true_when_never_built():
    sqlite_manager.init_schema()
    # 한 번도 빌드된 적 없는 컬렉션은 상태 행 자체가 없어도 '빌드 필요'로 간주해야 한다.
    assert sqlite_manager.is_communities_dirty(C) is True


def test_mark_and_clear_communities_dirty_roundtrip():
    sqlite_manager.init_schema()
    sqlite_manager.mark_communities_dirty(C)
    assert sqlite_manager.is_communities_dirty(C) is True

    sqlite_manager.clear_communities_dirty(C, "sig-abc")
    assert sqlite_manager.is_communities_dirty(C) is False

    # 다시 그래프가 바뀌면 dirty로 되돌아간다.
    sqlite_manager.mark_communities_dirty(C)
    assert sqlite_manager.is_communities_dirty(C) is True


def test_delete_community_build_state_resets_to_dirty_by_default():
    sqlite_manager.init_schema()
    sqlite_manager.clear_communities_dirty(C, "sig-abc")
    assert sqlite_manager.is_communities_dirty(C) is False

    sqlite_manager.delete_community_build_state(C)

    assert sqlite_manager.is_communities_dirty(C) is True
