# backup_db가 graphrag_dbs/ 내용을 zip 파일로 정확히 백업하는지 확인한다.
import zipfile

import backup_db
from config import settings


def test_create_backup_produces_zip_with_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "project_root", tmp_path)
    (settings.db_dir / "dummy_sub").mkdir(parents=True, exist_ok=True)
    (settings.db_dir / "dummy_sub" / "file.txt").write_text("hello", encoding="utf-8")

    archive_path = backup_db.create_backup()

    assert archive_path.exists()
    assert archive_path.parent == tmp_path / "backups"
    with zipfile.ZipFile(archive_path) as zf:
        names = zf.namelist()
    assert any("file.txt" in name for name in names)


def test_create_backup_prunes_old_backups(tmp_path, monkeypatch):
    # 새 백업을 만들 때 보관 개수(backup_keep)를 넘는 오래된 백업은 자동 삭제돼야 한다.
    monkeypatch.setattr(settings, "project_root", tmp_path)
    monkeypatch.setattr(settings, "backup_keep", 3)
    (settings.db_dir / "dummy_sub").mkdir(parents=True, exist_ok=True)
    (settings.db_dir / "dummy_sub" / "file.txt").write_text("x", encoding="utf-8")

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    # 시간순 정렬되도록 이름을 만든 오래된 백업 5개(모두 새 백업보다 과거 날짜).
    for i in range(1, 6):
        (backup_dir / f"graphrag_dbs_2025010{i}_000000.zip").write_text("old", encoding="utf-8")

    backup_db.create_backup()

    remaining = sorted(p.name for p in backup_dir.glob("graphrag_dbs_*.zip"))
    assert len(remaining) == 3  # 최신 3개(= 새 백업 1개 + 가장 최근 과거 2개)만 남는다
