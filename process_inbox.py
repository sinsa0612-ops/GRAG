# inbox/ 폴더의 파일을 모두 처리하고, 성공하면 processed/로 실패하면 failed/로 옮기는 진입점 스크립트.
import logging
import time
from pathlib import Path

from config import settings
from pipeline import ingest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# 목적지에 같은 이름의 파일이 이미 있으면 타임스탬프를 붙여 덮어쓰지 않는다.
def _unique_destination(dest_dir: Path, file_name: str) -> Path:
    destination = dest_dir / file_name
    if not destination.exists():
        return destination
    return dest_dir / f"{int(time.time() * 1000)}_{file_name}"


# inbox/ 안의 모든 파일을 지정한 컬렉션(사업)으로 처리한다.
# 처리된 파일은 컬렉션별 분류 폴더(processed/<컬렉션>/, failed/<컬렉션>/)로 옮겨 사업끼리 섞이지 않게 한다.
# model/glean_rounds는 process_file로 그대로 전달한다(없으면 설정 기본값).
def process_inbox(
    collection: str | None = None, model: str | None = None, glean_rounds: int | None = None
) -> None:
    collection = collection or settings.default_collection
    processed_dir = settings.processed_dir / collection
    failed_dir = settings.failed_dir / collection
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in settings.inbox_dir.iterdir() if p.is_file())
    if not files:
        logger.info("inbox/가 비어 있습니다.")
        return

    for file_path in files:
        try:
            ingest.process_file(file_path, collection, model=model, glean_rounds=glean_rounds)
            file_path.rename(_unique_destination(processed_dir, file_path.name))
            logger.info("[%s] 처리 완료: %s", collection, file_path.name)
        except Exception as exc:
            file_path.rename(_unique_destination(failed_dir, file_path.name))
            logger.error("[%s] 처리 실패, failed/로 이동: %s (%s)", collection, file_path.name, exc)


if __name__ == "__main__":
    process_inbox()
