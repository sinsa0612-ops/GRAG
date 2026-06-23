# backup_db -> 삭제 -> restore_db 전체 사이클이 실제로 데이터를 되살리는지 확인한다.
import pytest

import backup_db
import restore_db
from config import settings


def test_backup_then_restore_recovers_data(monkeypatch):
    monkeypatch.setattr(settings, "project_root", settings.db_dir.parent)

    (settings.db_dir / "dummy_sub").mkdir(parents=True, exist_ok=True)
    (settings.db_dir / "dummy_sub" / "file.txt").write_text("원본 내용", encoding="utf-8")

    archive_path = backup_db.create_backup()

    # 데이터를 실제로 지워서 '사고'를 흉내낸다.
    (settings.db_dir / "dummy_sub" / "file.txt").unlink()
    assert not (settings.db_dir / "dummy_sub" / "file.txt").exists()

    restore_db.restore_backup(archive_path)

    restored = settings.db_dir / "dummy_sub" / "file.txt"
    assert restored.exists()
    assert restored.read_text(encoding="utf-8") == "원본 내용"


def test_restore_corrupt_archive_keeps_existing_db(tmp_path, monkeypatch):
    # 손상된 zip으로 복원을 시도해도, 살아있는 DB가 통째로 날아가지 않아야 한다(원자적 스왑).
    monkeypatch.setattr(settings, "project_root", settings.db_dir.parent)

    live = settings.db_dir / "dummy_sub" / "file.txt"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("소중한 원본", encoding="utf-8")

    broken = tmp_path / "broken.zip"
    broken.write_text("이건 진짜 zip이 아님", encoding="utf-8")

    with pytest.raises(Exception):
        restore_db.restore_backup(broken)

    # 복원이 실패해도 기존 DB는 그대로여야 한다.
    assert live.exists()
    assert live.read_text(encoding="utf-8") == "소중한 원본"
