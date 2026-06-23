# 고아 데이터(추적 끊긴 벡터/관계)를 정리하는 스크립트.
# 고립된 엔티티(관계가 전혀 없는 노드)는 나중에 다른 문서에서 관계가 생길 수 있으므로
# 자동으로 지우지 않는다 — db_status.py에서 개수만 참고용으로 보여준다.
import logging

from db import document_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# 고아 데이터를 정리한다.
def main() -> None:
    orphaned_count = document_store.cleanup_orphaned_data()
    logger.info("고아 데이터 정리 완료: %d개 source_id", orphaned_count)


if __name__ == "__main__":
    main()
