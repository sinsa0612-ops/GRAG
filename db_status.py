# 현재 DB 상태(문서/엔티티/관계 수, 디스크 용량, 사용 중인 타입/관계 어휘)를 보여주는 점검 스크립트.
import logging
from pathlib import Path

from config import settings
from db import document_store, graph_manager, sqlite_manager, vector_manager

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


# 폴더(또는 파일) 전체의 디스크 사용량(바이트)을 계산한다.
def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


# DB 현황을 모아서 한 번에 출력한다.
def print_status() -> None:
    doc_count = sqlite_manager.count_documents()
    chunk_count = vector_manager.count_chunks()
    entity_count = graph_manager.count_entities()
    relation_count = graph_manager.count_relations()
    known_types = graph_manager.get_known_types()
    known_predicates = graph_manager.get_known_predicates()
    orphaned_count = len(document_store.find_orphaned_source_ids())
    isolated_count = len(graph_manager.find_isolated_entities())
    total_size_mb = _dir_size_bytes(settings.db_dir) / (1024 * 1024)

    logger.info("=== GraphRAG DB 현황 ===")
    logger.info("문서 수: %d", doc_count)
    logger.info("벡터 청크 수: %d", chunk_count)
    logger.info("엔티티 수: %d", entity_count)
    logger.info("관계 수: %d", relation_count)
    logger.info("디스크 사용량: %.2f MB", total_size_mb)
    logger.info("사용 중인 엔티티 타입(%d개): %s", len(known_types), ", ".join(known_types) or "(없음)")
    logger.info(
        "사용 중인 관계 이름(%d개): %s", len(known_predicates), ", ".join(known_predicates) or "(없음)"
    )
    logger.info("고아 데이터(source_id): %d개 %s", orphaned_count, "- cleanup_db.py 실행 권장" if orphaned_count else "")
    logger.info("고립된 엔티티(관계 없음, 자동삭제 안 함): %d개", isolated_count)


if __name__ == "__main__":
    print_status()
