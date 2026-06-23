# DB 3종(SQLite/ChromaDB/KuzuDB) 최초 초기화 진입점 스크립트.
import logging

from db import graph_manager, sqlite_manager, vector_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# 3개 DB의 스키마를 순서대로 초기화한다.
def main() -> None:
    logger.info("DB 초기화를 시작합니다...")
    sqlite_manager.init_schema()
    logger.info("SQLite 준비 완료")
    vector_manager.init_schema()
    logger.info("ChromaDB 준비 완료")
    graph_manager.init_schema()
    logger.info("KuzuDB 준비 완료")
    logger.info("모든 DB 초기화가 완료되었습니다.")


if __name__ == "__main__":
    main()
