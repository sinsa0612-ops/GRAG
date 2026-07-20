# 문서 저장 오케스트레이션(document_store)이 해시 비교/청킹/교체 흐름을 올바르게 수행하는지 검증한다.
from config import settings
from db import document_store, graph_manager, sqlite_manager, vector_manager

C = "c1"


def test_estimate_request_count_equals_chunk_count(monkeypatch):
    # 예상 요청 수 = 청크 수 (RPD 한도 예측의 근거).
    monkeypatch.setattr(settings, "chunk_size", 10)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    assert document_store.estimate_request_count("가" * 25) == 3


def test_compute_hash_is_deterministic():
    assert document_store.compute_hash("같은 내용") == document_store.compute_hash("같은 내용")
    assert document_store.compute_hash("a") != document_store.compute_hash("b")


def test_chunk_text_splits_by_size():
    chunks = document_store.chunk_text("가나다라마바사", chunk_size=3)
    assert chunks == ["가나다", "라마바", "사"]


def test_chunk_text_overlaps_boundaries():
    chunks = document_store.chunk_text("가나다라마바사아자차", chunk_size=4, overlap=2)
    assert chunks == ["가나다라", "다라마바", "마바사아", "사아자차"]


def test_chunk_text_merges_trailing_chunk_smaller_than_overlap():
    content = "가" * 850
    chunks = document_store.chunk_text(content, chunk_size=1000, overlap=200)

    assert len(chunks) == 1
    assert chunks[0] == content


def test_chunk_text_merge_guard_preserves_full_content():
    content = "가" * 2500
    chunks = document_store.chunk_text(content, chunk_size=1000, overlap=200)

    assert all(len(c) > 200 for c in chunks)
    assert "".join(chunks)[-1] == content[-1]
    assert chunks[0][0] == content[0]


def test_chunk_text_prefers_sentence_boundaries():
    # 경계가 있는 글은 문장 한가운데가 아니라 문장부호 뒤에서 잘려야 한다(추출 품질).
    content = "첫 문장이다. 두 번째 문장이다. 세 번째 문장이다."
    chunks = document_store.chunk_text(content, chunk_size=20, overlap=0)

    assert len(chunks) >= 2
    assert "".join(chunks) == content  # overlap=0이면 글자 손실 없이 원문이 복원된다
    assert chunks[0].rstrip().endswith(".")  # 첫 청크가 문장 끝에서 마무리됨


def test_clean_markdown_drops_empty_table_grid():
    # 빈 셀 격자와 구분선은 통째로 사라지고, 내용 있는 셀만 공백으로 이어져야 한다.
    raw = (
        "| 기관명 |     | 케이퓨얼셀 |     | 등록번호 | 111-88 |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "|     |     |     |     |     |     |\n"
        "본문 문장이다."
    )
    cleaned = document_store.clean_markdown(raw)

    assert "기관명 케이퓨얼셀 등록번호 111-88" in cleaned
    assert "---" not in cleaned
    assert "|     |" not in cleaned
    assert "본문 문장이다." in cleaned


def test_clean_markdown_leaves_plain_text_untouched():
    # 표가 없는 일반 문장은 글자 손실 없이 그대로 남아야 한다.
    raw = "강택리는 기획자다. 두 번째 문장이다."
    assert document_store.clean_markdown(raw) == raw


def test_clean_markdown_reduces_chunk_count(monkeypatch):
    # 표 노이즈를 걷어내면 같은 문서라도 예상 청크 수(=LLM 호출 수)가 줄어든다.
    monkeypatch.setattr(settings, "chunk_size", 20)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    noisy = ("|   |   |   |   |\n" * 50) + "핵심 내용 한 줄."
    assert document_store.estimate_request_count(noisy) == 1


def test_needs_processing_detects_change():
    sqlite_manager.init_schema()
    assert document_store.needs_processing(C, "memo.md", "hash1") is True

    sqlite_manager.upsert_document("doc_1", C, "memo.md", "내용", "hash1")
    assert document_store.needs_processing(C, "memo.md", "hash1") is False
    assert document_store.needs_processing(C, "memo.md", "hash2") is True


def test_commit_document_cleans_up_old_data(monkeypatch):
    sqlite_manager.init_schema()
    deleted_sources = []
    deleted_docs = []
    monkeypatch.setattr(
        "db.document_store.vector_manager.delete_chunks_by_source",
        lambda source_id: deleted_sources.append(source_id),
    )
    monkeypatch.setattr(
        "db.document_store.graph_manager.delete_relations_by_source_doc",
        lambda source_doc: deleted_docs.append(source_doc),
    )

    first_id = document_store.prepare_replacement("memo.md")
    document_store.commit_document(first_id, C, "memo.md", "첫 버전", "hash_a")
    assert deleted_sources == []

    second_id = document_store.prepare_replacement("memo.md")
    document_store.commit_document(second_id, C, "memo.md", "두번째 버전", "hash_b")
    assert deleted_sources == [first_id]
    assert deleted_docs == [first_id]
    assert second_id != first_id


def test_revert_to_previous_content_is_detected(monkeypatch):
    # A->B로 바꿨다가 다시 A로 되돌려도 '변경'으로 감지돼야 한다.
    sqlite_manager.init_schema()
    monkeypatch.setattr(
        "db.document_store.vector_manager.delete_chunks_by_source", lambda source_id: None
    )
    monkeypatch.setattr(
        "db.document_store.graph_manager.delete_relations_by_source_doc", lambda source_doc: None
    )

    id_a = document_store.prepare_replacement("memo.md")
    document_store.commit_document(id_a, C, "memo.md", "A 내용", "hash_a")
    id_b = document_store.prepare_replacement("memo.md")
    document_store.commit_document(id_b, C, "memo.md", "B 내용", "hash_b")

    assert document_store.needs_processing(C, "memo.md", "hash_a") is True


def test_commit_document_not_called_means_no_record(monkeypatch):
    sqlite_manager.init_schema()
    document_store.prepare_replacement("memo.md")

    assert document_store.needs_processing(C, "memo.md", "어떤 해시든") is True


def test_delete_document_removes_everything(monkeypatch):
    sqlite_manager.init_schema()
    deleted_sources = []
    deleted_docs = []
    monkeypatch.setattr(
        "db.document_store.vector_manager.delete_chunks_by_source",
        lambda source_id: deleted_sources.append(source_id),
    )
    monkeypatch.setattr(
        "db.document_store.graph_manager.delete_relations_by_source_doc",
        lambda source_doc: deleted_docs.append(source_doc),
    )

    source_id = document_store.prepare_replacement("memo.md")
    document_store.commit_document(source_id, C, "memo.md", "내용", "hash_a")

    assert document_store.delete_document(C, "memo.md") is True
    assert deleted_sources == [source_id]
    assert deleted_docs == [source_id]
    assert sqlite_manager.get_document_hash(C, "memo.md") is None


def test_delete_document_returns_false_when_not_found():
    sqlite_manager.init_schema()
    assert document_store.delete_document(C, "없는파일.md") is False


def test_delete_document_marks_communities_dirty(monkeypatch):
    # [M2] 문서 삭제로 관계가 사라지므로 그 컬렉션의 커뮤니티는 재빌드가 필요해진다.
    sqlite_manager.init_schema()
    monkeypatch.setattr("db.document_store.vector_manager.delete_chunks_by_source", lambda s: None)
    monkeypatch.setattr("db.document_store.graph_manager.delete_relations_by_source_doc", lambda s: None)

    source_id = document_store.prepare_replacement("memo.md")
    document_store.commit_document(source_id, C, "memo.md", "내용", "hash_a")
    sqlite_manager.clear_communities_dirty(C, "이전-서명")

    document_store.delete_document(C, "memo.md")

    assert sqlite_manager.is_communities_dirty(C) is True


def test_delete_collection_removes_everything(monkeypatch):
    # 컬렉션 통째 삭제: 문서 기록 + 벡터 청크 + 그래프 엔티티/관계가 그 컬렉션만 사라져야 한다.
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    deleted_vec_collections = []
    monkeypatch.setattr(
        "db.document_store.vector_manager.delete_chunks_by_collection",
        lambda c: deleted_vec_collections.append(c),
    )

    sqlite_manager.upsert_document("doc_a", "사업A", "memo.md", "내용", "h")
    graph_manager.upsert_entity("사업A", "김부장", "Person", "")
    graph_manager.upsert_entity("사업B", "이대리", "Person", "")

    count = document_store.delete_collection("사업A")

    assert count == 1
    assert deleted_vec_collections == ["사업A"]
    assert sqlite_manager.count_documents(["사업A"]) == 0
    assert graph_manager.count_entities(["사업A"]) == 0
    assert graph_manager.count_entities(["사업B"]) == 1


def test_delete_collection_cascades_communities_and_build_state():
    # [M2] 컬렉션을 통째로 지우면 그 컬렉션의 커뮤니티 오버레이(탐지 결과 + dirty/서명 상태)도 함께 사라져야
    # 한다 — 삭제된 컬렉션의 유령 커뮤니티 행이 남으면 안 된다(다른 컬렉션은 그대로).
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    community = {
        "collection": "사업A",
        "community_id": "c1",
        "level": 0,
        "parent_community_id": None,
        "entity_names": ["김부장"],
        "size": 1,
        "graph_signature": "sig",
    }
    sqlite_manager.replace_communities("사업A", [community])
    sqlite_manager.clear_communities_dirty("사업A", "sig")
    sqlite_manager.replace_communities("사업B", [{**community, "collection": "사업B"}])
    sqlite_manager.clear_communities_dirty("사업B", "sig")

    document_store.delete_collection("사업A")

    assert sqlite_manager.get_communities("사업A") == []
    assert sqlite_manager.is_communities_dirty("사업A") is True  # 상태 행도 삭제돼 기본값(dirty)으로
    assert len(sqlite_manager.get_communities("사업B")) == 1
    assert sqlite_manager.is_communities_dirty("사업B") is False


def test_delete_collection_cascades_community_reports():
    # [M3] 컬렉션을 통째로 지우면 그 컬렉션의 커뮤니티 리포트도 함께 사라져야 한다(다른 컬렉션은 그대로).
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    sqlite_manager.upsert_community_report("사업A", "c1", 0, "A제목", "A요약", None)
    sqlite_manager.upsert_community_report("사업B", "c1", 0, "B제목", "B요약", None)

    document_store.delete_collection("사업A")

    assert sqlite_manager.get_community_reports("사업A") == []
    assert len(sqlite_manager.get_community_reports("사업B")) == 1


def test_find_orphaned_source_ids_detects_untracked_data():
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "A", "Person", "")
    graph_manager.upsert_entity(C, "B", "Person", "")
    graph_manager.upsert_relation(C, "A", "B", "KNOWS", "", "doc_orphan")

    assert document_store.find_orphaned_source_ids() == {"doc_orphan"}


def test_find_orphaned_source_ids_excludes_tracked_data():
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    sqlite_manager.upsert_document("doc_1", C, "memo.md", "내용", "hash1")
    graph_manager.upsert_entity(C, "A", "Person", "")
    graph_manager.upsert_entity(C, "B", "Person", "")
    graph_manager.upsert_relation(C, "A", "B", "KNOWS", "", "doc_1")

    assert document_store.find_orphaned_source_ids() == set()


def test_find_orphaned_source_ids_detects_orphaned_vector_chunks(monkeypatch):
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    monkeypatch.setattr("db.vector_manager.embed_texts", lambda texts: [[0.0, 0.0] for _ in texts])

    vector_manager.add_chunks("doc_orphan_vec", ["청크 내용"], C)

    assert document_store.find_orphaned_source_ids() == {"doc_orphan_vec"}


def test_cleanup_orphaned_data_removes_untracked_relations():
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "A", "Person", "")
    graph_manager.upsert_entity(C, "B", "Person", "")
    graph_manager.upsert_relation(C, "A", "B", "KNOWS", "", "doc_orphan")

    removed = document_store.cleanup_orphaned_data()

    assert removed == 1
    assert graph_manager.get_outgoing_relations(C, "A") == []


# --- M1.5: 설명 후보(entity_desc_candidates) 캐스케이드 — 유령 후보 방지(spec-addendum §C-4) ---


def test_commit_document_cleans_up_old_desc_candidates(monkeypatch):
    # 문서 재처리 시 옛 source_id가 남긴 설명 후보는 정확히 제거되고, 새 문서 후보만 남아야 한다.
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    monkeypatch.setattr(
        "db.document_store.vector_manager.delete_chunks_by_source", lambda source_id: None
    )

    first_id = document_store.prepare_replacement("memo.md")
    sqlite_manager.upsert_desc_candidate(C, "강택리", first_id, "첫 버전 설명")
    document_store.commit_document(first_id, C, "memo.md", "첫 버전", "hash_a")
    assert sqlite_manager.get_desc_candidates(C, "강택리") == ["첫 버전 설명"]

    second_id = document_store.prepare_replacement("memo.md")
    sqlite_manager.upsert_desc_candidate(C, "강택리", second_id, "둘째 버전 설명")
    document_store.commit_document(second_id, C, "memo.md", "둘째 버전", "hash_b")

    # 옛 문서(first_id) 후보는 사라지고 새 문서(second_id) 후보만 남아야 한다(유령 후보 없음).
    assert sqlite_manager.get_desc_candidates(C, "강택리") == ["둘째 버전 설명"]


def test_delete_document_removes_desc_candidates(monkeypatch):
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    monkeypatch.setattr(
        "db.document_store.vector_manager.delete_chunks_by_source", lambda source_id: None
    )
    source_id = document_store.prepare_replacement("memo.md")
    sqlite_manager.upsert_desc_candidate(C, "강택리", source_id, "설명")
    document_store.commit_document(source_id, C, "memo.md", "내용", "hash_a")

    document_store.delete_document(C, "memo.md")

    assert sqlite_manager.get_desc_candidates(C, "강택리") == []


def test_delete_collection_removes_desc_candidates_scoped_to_that_collection(monkeypatch):
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    monkeypatch.setattr("db.document_store.vector_manager.delete_chunks_by_collection", lambda c: None)

    sqlite_manager.upsert_document("doc_a", "사업A", "memo.md", "내용", "h")
    sqlite_manager.upsert_desc_candidate("사업A", "김부장", "doc_a", "A쪽 설명")
    sqlite_manager.upsert_desc_candidate("사업B", "이대리", "doc_b", "B쪽 설명")

    document_store.delete_collection("사업A")

    assert sqlite_manager.get_desc_candidates("사업A", "김부장") == []
    assert sqlite_manager.get_desc_candidates("사업B", "이대리") == ["B쪽 설명"]


def test_cleanup_orphaned_data_removes_orphaned_desc_candidates():
    # 처리 중간에 끊겨 문서 기록이 없는 source_id의 설명 후보도 고아 정리 대상이어야 한다.
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "A", "Person", "")
    graph_manager.upsert_entity(C, "B", "Person", "")
    graph_manager.upsert_relation(C, "A", "B", "KNOWS", "", "doc_orphan")
    sqlite_manager.upsert_desc_candidate(C, "A", "doc_orphan", "고아 설명")

    removed = document_store.cleanup_orphaned_data()

    assert removed == 1
    assert sqlite_manager.get_desc_candidates(C, "A") == []
