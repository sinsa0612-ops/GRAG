# backup_db.py가 만든 zip 백업을 graphrag_dbs/에 복원하는 스크립트.
# 주의: 실행하면 현재 graphrag_dbs/ 내용을 전부 덮어쓴다.
import logging
import shutil
import sys
from pathlib import Path

from config import settings
from db import graph_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# 지정한 백업 zip을 graphrag_dbs/에 복원한다. 기존 내용은 교체된다.
# 핵심: '먼저 임시 폴더에 풀어보고 성공했을 때만' 기존 DB를 교체한다.
# 그래서 zip이 손상돼 풀기에 실패해도 살아있는 DB가 통째로 날아가지 않는다(원자적 스왑).
# Kuzu 커넥션이 열려 있으면 Windows에서 폴더 이동이 PermissionError로 막히므로, 교체 직전에 닫는다.
def restore_backup(archive_path: Path) -> None:
    if not archive_path.exists():
        raise FileNotFoundError(f"백업 파일을 찾을 수 없습니다: {archive_path}")

    staging = settings.db_dir.parent / f"{settings.db_dir.name}.restore_tmp"
    old_dir = settings.db_dir.parent / f"{settings.db_dir.name}.old"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    # 1) 먼저 스테이징 폴더에 풀어본다 — 손상된 zip이면 여기서 실패하고, 기존 DB는 그대로 남는다.
    try:
        shutil.unpack_archive(str(archive_path), str(staging), format="zip")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # 2) 풀기에 성공했을 때만 기존 DB를 옆으로 치우고 새 DB를 제자리에 놓는다(폴더 rename은 순간적).
    graph_manager.close_connection()
    if old_dir.exists():
        shutil.rmtree(old_dir)
    try:
        if settings.db_dir.exists():
            settings.db_dir.rename(old_dir)
        staging.rename(settings.db_dir)
    except Exception:
        # 교체 도중 실패하면 치워뒀던 원래 DB를 되돌린다.
        if not settings.db_dir.exists() and old_dir.exists():
            old_dir.rename(settings.db_dir)
        shutil.rmtree(staging, ignore_errors=True)
        raise

    shutil.rmtree(old_dir, ignore_errors=True)
    logger.info("복원 완료: %s -> %s", archive_path, settings.db_dir)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("사용법: python restore_db.py <백업.zip 경로>")
        sys.exit(1)
    restore_backup(Path(sys.argv[1]))
