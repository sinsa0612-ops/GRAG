# graphrag 통합 CLI의 핵심 서브커맨드(init/ingest/status/collections)가 에러 없이 동작하는지 확인한다.
import json

import graphrag_cli
import pipeline.ingest as ingest
from config import settings


def test_cli_init_status_collections_smoke(capsys):
    graphrag_cli.main(["init"])
    graphrag_cli.main(["status"])
    graphrag_cli.main(["collections"])

    out = capsys.readouterr().out
    assert "DB 초기화 완료" in out
    assert "엔티티 수: 0" in out
    assert "아직 컬렉션이 없습니다" in out


def test_cli_ingest_into_collection_then_listed(tmp_path, monkeypatch, capsys):
    # 추출 결과(엔티티 1개)를 가짜로 돌려주고, ingest 후 collections에 그 컬렉션이 보이는지 확인한다.
    # 또한 파일을 직접 지정해도 처리 후 원본이 processed/<컬렉션>/로 이동하는지 확인한다.
    monkeypatch.setattr(settings, "project_root", tmp_path)
    monkeypatch.setattr(
        ingest,
        "generate",
        lambda prompt, **kwargs: json.dumps(
            {"entities": [{"name": "강택리", "type": "Person", "description": "기획자"}], "relations": []}
        ),
    )
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)

    graphrag_cli.main(["init"])
    memo = tmp_path / "memo.md"
    memo.write_text("강택리는 기획자다.", encoding="utf-8")

    graphrag_cli.main(["ingest", str(memo), "--collection", "사업A"])
    graphrag_cli.main(["status", "--collection", "사업A"])
    graphrag_cli.main(["collections"])

    out = capsys.readouterr().out
    assert "사업A" in out
    assert "엔티티 수: 1" in out
    # 원본은 사라지고 processed/사업A/ 로 이동했다.
    assert not memo.exists()
    assert (settings.processed_dir / "사업A" / "memo.md").exists()


def test_cli_ingest_moves_failed_file_to_failed_dir(tmp_path, monkeypatch, capsys):
    # 처리 중 에러가 나면 직접 지정한 파일도 failed/<컬렉션>/로 이동해야 한다.
    monkeypatch.setattr(settings, "project_root", tmp_path)

    def boom(path, collection):
        raise RuntimeError("처리 중 에러")

    monkeypatch.setattr(ingest, "process_file", boom)

    graphrag_cli.main(["init"])
    memo = tmp_path / "memo.md"
    memo.write_text("아무 메모", encoding="utf-8")

    graphrag_cli.main(["ingest", str(memo), "--collection", "사업A"])

    assert not memo.exists()
    assert (settings.failed_dir / "사업A" / "memo.md").exists()


def test_cli_delete_collection_clears_it(tmp_path, monkeypatch, capsys):
    # 잘못 넣은 컬렉션을 delete-collection으로 통째 비우면 엔티티가 사라져야 한다(롤백 용도).
    monkeypatch.setattr(settings, "project_root", tmp_path)
    monkeypatch.setattr(
        ingest,
        "generate",
        lambda prompt, **kwargs: json.dumps(
            {"entities": [{"name": "김부장", "type": "Person", "description": ""}], "relations": []}
        ),
    )
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    monkeypatch.setattr("db.vector_manager.delete_chunks_by_collection", lambda c: None)
    from db import graph_manager

    graphrag_cli.main(["init"])
    memo = tmp_path / "memo.md"
    memo.write_text("김부장은 일한다.", encoding="utf-8")
    graphrag_cli.main(["ingest", str(memo), "--collection", "사업B"])
    assert graph_manager.count_entities(["사업B"]) >= 1

    graphrag_cli.main(["delete-collection", "사업B"])

    assert graph_manager.count_entities(["사업B"]) == 0
    assert "삭제 완료" in capsys.readouterr().out
