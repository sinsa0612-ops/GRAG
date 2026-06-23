# inbox/ 처리 진입점이 파일을 올바르게 처리하고 이동시키는지 확인한다.
import json

import process_inbox
import pipeline.ingest as ingest
from config import settings
from db import graph_manager, sqlite_manager

VALID_RESPONSE = json.dumps({"entities": [], "relations": []})


def test_process_inbox_empty_does_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "project_root", tmp_path)
    process_inbox.process_inbox()  # 예외 없이 끝나야 한다


def test_process_inbox_moves_succeeded_file_to_processed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "project_root", tmp_path)
    monkeypatch.setattr(ingest, "generate", lambda prompt, **kwargs: VALID_RESPONSE)
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    file_path = settings.inbox_dir / "memo.md"
    file_path.write_text("아무 메모", encoding="utf-8")

    process_inbox.process_inbox("사업A")

    assert not file_path.exists()
    # 처리된 파일은 컬렉션별 분류 폴더(processed/<컬렉션>/)로 이동한다.
    assert (settings.processed_dir / "사업A" / "memo.md").exists()


def test_process_inbox_moves_failed_file_to_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "project_root", tmp_path)

    def boom(path, collection):
        raise RuntimeError("처리 중 에러")

    monkeypatch.setattr(ingest, "process_file", boom)

    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    file_path = settings.inbox_dir / "memo.md"
    file_path.write_text("아무 메모", encoding="utf-8")

    process_inbox.process_inbox()

    assert not file_path.exists()
    assert (settings.failed_dir / settings.default_collection / "memo.md").exists()


def test_process_inbox_does_not_overwrite_existing_processed_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "project_root", tmp_path)
    monkeypatch.setattr(ingest, "generate", lambda prompt, **kwargs: VALID_RESPONSE)
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    processed_collection_dir = settings.processed_dir / settings.default_collection
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    processed_collection_dir.mkdir(parents=True, exist_ok=True)
    (processed_collection_dir / "memo.md").write_text("이미 있던 파일", encoding="utf-8")

    file_path = settings.inbox_dir / "memo.md"
    file_path.write_text("새 메모", encoding="utf-8")

    process_inbox.process_inbox()

    assert not file_path.exists()
    moved_files = list(processed_collection_dir.iterdir())
    assert len(moved_files) == 2
    assert (processed_collection_dir / "memo.md").read_text(encoding="utf-8") == "이미 있던 파일"
