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
