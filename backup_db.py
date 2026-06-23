# graphrag_dbs/ 전체를 zip으로 압축해 backups/ 아래 타임스탬프 이름으로 저장하는 백업 스크립트.
import logging
from datetime import datetime
from pathlib import Path
from shutil import make_archive

from config import settings
from db import graph_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# backups/에서 최신 keep개만 남기고 오래된 백업 zip을 삭제한다.
# 파일명이 graphrag_dbs_YYYYMMDD_HHMMSS.zip이라 이름 정렬이 곧 시간 정렬이다.
def _prune_old_backups(backup_dir: Path, keep: int) -> None:
    if keep < 1:
        return
    backups = sorted(backup_dir.glob("graphrag_dbs_*.zip"))
    for old in backups[:-keep]:
        old.unlink()
        logger.info("오래된 백업 삭제: %s", old.name)


# graphrag_dbs/ 전체를 zip으로 압축해 backups/ 아래 타임스탬프 이름으로 저장한다.
# Kuzu 커넥션이 열려 있으면 Windows에서 파일 복사가 PermissionError로 막히므로, 먼저 닫는다.
# 백업이 무한정 쌓이지 않도록, 저장 후 설정된 보관 개수(backup_keep)를 넘는 오래된 백업은 정리한다.
def create_backup() -> Path:
    graph_manager.close_connection()

    backup_dir = settings.project_root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_base = backup_dir / f"graphrag_dbs_{timestamp}"
    archive_path = make_archive(str(archive_base), "zip", root_dir=settings.db_dir)
    logger.info("백업 완료: %s", archive_path)
    _prune_old_backups(backup_dir, settings.backup_keep)
    return Path(archive_path)


if __name__ == "__main__":
    create_backup()
