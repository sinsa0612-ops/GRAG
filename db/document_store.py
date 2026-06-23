# 문서 단위 저장 오케스트레이션 — sqlite/vector/graph 호출 순서만 정하는 얇은 계층.
import hashlib
import logging
import time

from db import graph_manager, sqlite_manager, vector_manager

logger = logging.getLogger(__name__)


# 문서 내용으로부터 변경 감지용 해시값을 계산한다.
def compute_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# 문서를 일정 길이로 분할한다. overlap만큼 겹쳐 잘라 경계에서 끊긴 문맥(관계)을 보존한다.
# 마지막 청크가 overlap 이하로 작으면(= 내용 전부가 이전 청크와 중복) 따로 떼지 않고
# 이전 청크에 흡수시켜, 의미 없는 초소형 청크가 LLM 호출을 낭비하지 않게 한다.
def chunk_text(content: str, chunk_size: int, overlap: int = 0) -> list[str]:
    step = chunk_size - overlap
    starts = list(range(0, len(content), step))
    chunks = [content[i : i + chunk_size] for i in starts]

    if len(chunks) > 1 and len(chunks[-1]) <= overlap:
        chunks = chunks[:-2] + [content[starts[-2] :]]

    return chunks


# 저장된 해시와 비교해 재처리가 필요한지 판단한다 (해당 컬렉션 범위에서).
def needs_processing(collection: str, file_name: str, content_hash: str) -> bool:
    existing_hash = sqlite_manager.get_document_hash(collection, file_name)
    return existing_hash != content_hash


# 새 source_id만 발급한다. 이 단계에서는 SQLite를 건드리지 않는다 — 처리가 끝까지
# 성공해야 commit_document가 호출되므로, 중간에 실패하면 다음 시도에서 다시 처리 대상이 된다.
def prepare_replacement(file_name: str) -> str:
    return f"doc_{int(time.time() * 1000)}"


# 모든 청킹/임베딩/추출이 성공적으로 끝난 뒤에만 호출한다.
# 새 문서를 SQLite에 기록하고, 그제서야 옛 문서의 벡터/그래프 데이터를 정리한다.
def commit_document(
    source_id: str, collection: str, file_name: str, content: str, content_hash: str
) -> None:
    old_source_id = sqlite_manager.get_document_source_id(collection, file_name)
    sqlite_manager.upsert_document(source_id, collection, file_name, content, content_hash)

    if old_source_id and old_source_id != source_id:
        logger.info("기존 문서 데이터 삭제: [%s] %s (source_id=%s)", collection, file_name, old_source_id)
        vector_manager.delete_chunks_by_source(old_source_id)
        graph_manager.delete_relations_by_source_doc(old_source_id)


# 문서를 완전히 삭제한다(SQLite 기록 + 벡터 청크 + 관계). 엔티티 노드 자체는 남는다
# (다른 문서가 같은 엔티티를 참조하고 있을 수 있어서, 엔티티 삭제는 graph_manager.delete_entity로 별도 처리).
# 문서가 존재하지 않으면 False를 반환한다.
def delete_document(collection: str, file_name: str) -> bool:
    source_id = sqlite_manager.get_document_source_id(collection, file_name)
    if not source_id:
        return False

    vector_manager.delete_chunks_by_source(source_id)
    graph_manager.delete_relations_by_source_doc(source_id)
    sqlite_manager.delete_document(collection, file_name)
    logger.info("문서 완전 삭제: [%s] %s (source_id=%s)", collection, file_name, source_id)
    return True


# 한 컬렉션(사업)을 통째로 삭제한다(문서 기록 + 벡터 청크 + 그래프 엔티티/관계 전부).
# 잘못된 컬렉션으로 추출한 것을 깔끔히 되돌릴 때 쓴다. 삭제한 문서 수를 반환한다.
def delete_collection(collection: str) -> int:
    doc_count = sqlite_manager.count_documents([collection])
    vector_manager.delete_chunks_by_collection(collection)
    graph_manager.delete_collection(collection)
    sqlite_manager.delete_collection_documents(collection)
    logger.info("컬렉션 통째 삭제: %s (문서 %d개)", collection, doc_count)
    return doc_count


# SQLite에 더 이상 기록되지 않은 source_id(= 처리 중간에 실패해서 추적이 끊긴 데이터)를 찾는다.
def find_orphaned_source_ids() -> set[str]:
    valid_ids = sqlite_manager.get_all_source_ids()
    referenced_ids = vector_manager.get_all_source_ids() | graph_manager.get_all_source_docs()
    return referenced_ids - valid_ids


# 고아 source_id의 벡터 청크/관계를 모두 정리하고, 정리한 source_id 개수를 반환한다.
def cleanup_orphaned_data() -> int:
    orphaned = find_orphaned_source_ids()
    for source_id in orphaned:
        vector_manager.delete_chunks_by_source(source_id)
        graph_manager.delete_relations_by_source_doc(source_id)
        logger.info("고아 데이터 정리: source_id=%s", source_id)
    return len(orphaned)
