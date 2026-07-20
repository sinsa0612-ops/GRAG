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


def test_cli_ingest_dry_run_shows_estimate_only(tmp_path, monkeypatch, capsys):
    # --dry-run은 예상 요청 수만 보여주고 실제 처리는 하지 않는다.
    monkeypatch.setattr(settings, "project_root", tmp_path)
    monkeypatch.setattr(settings, "chunk_size", 10)
    monkeypatch.setattr(settings, "chunk_overlap", 0)

    graphrag_cli.main(["init"])
    memo = tmp_path / "memo.md"
    memo.write_text("가" * 25, encoding="utf-8")  # 3청크

    graphrag_cli.main(["ingest", str(memo), "--dry-run"])

    out = capsys.readouterr().out
    assert "예상 3 요청" in out
    assert "dry-run" in out
    assert memo.exists()  # 처리 안 함 → 원본 그대로


def test_cli_ingest_blocks_when_over_daily_limit(tmp_path, monkeypatch, capsys):
    # 예상 + 오늘 사용량이 하루 한도를 넘으면 차단하고 분할을 안내한다.
    monkeypatch.setattr(settings, "project_root", tmp_path)
    monkeypatch.setattr(settings, "chunk_size", 10)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    monkeypatch.setattr(settings, "llm_daily_limit", 2)
    from db import graph_manager

    graphrag_cli.main(["init"])
    memo = tmp_path / "big.md"
    memo.write_text("가" * 25, encoding="utf-8")  # 3청크 > 한도 2

    graphrag_cli.main(["ingest", str(memo), "--collection", "사업A"])

    out = capsys.readouterr().out
    assert "한도 초과" in out
    assert "나눠" in out
    assert memo.exists()  # 차단 → 처리 안 함, 원본 그대로
    assert graph_manager.count_entities() == 0


def test_cli_ingest_force_overrides_limit(tmp_path, monkeypatch):
    # --force는 한도 초과 예상이어도 강행한다.
    monkeypatch.setattr(settings, "project_root", tmp_path)
    monkeypatch.setattr(settings, "chunk_size", 10)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    monkeypatch.setattr(settings, "llm_daily_limit", 1)
    monkeypatch.setattr(
        ingest, "generate", lambda prompt, **kwargs: json.dumps({"entities": [], "relations": []})
    )
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)

    graphrag_cli.main(["init"])
    memo = tmp_path / "m.md"
    memo.write_text("가" * 25, encoding="utf-8")

    graphrag_cli.main(["ingest", str(memo), "--collection", "사업A", "--force"])

    assert not memo.exists()  # force로 처리됨 → processed/로 이동


def test_cli_usage_reports_today(capsys):
    from db import sqlite_manager

    graphrag_cli.main(["init"])
    sqlite_manager.record_api_usage(5)
    graphrag_cli.main(["usage"])

    assert "5/" in capsys.readouterr().out


def test_cli_set_parent_expands_status_scope(tmp_path, monkeypatch, capsys):
    # 본부 아래 사업A를 묶고 status --collection 본부를 보면 자손(사업A) 엔티티까지 합산돼야 한다.
    monkeypatch.setattr(settings, "project_root", tmp_path)
    monkeypatch.setattr(
        ingest,
        "generate",
        lambda prompt, **kwargs: json.dumps(
            {"entities": [{"name": "김부장", "type": "Person", "description": ""}], "relations": []}
        ),
    )
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)

    graphrag_cli.main(["init"])
    memo = tmp_path / "memo.md"
    memo.write_text("김부장은 일한다.", encoding="utf-8")
    graphrag_cli.main(["ingest", str(memo), "--collection", "사업A"])
    graphrag_cli.main(["set-parent", "사업A", "본부"])
    capsys.readouterr()  # 이전 출력 비우기

    graphrag_cli.main(["status", "--collection", "본부"])

    assert "엔티티 수: 1" in capsys.readouterr().out


def test_cli_summarize_descriptions_dispatches_to_pipeline(tmp_path, monkeypatch, capsys):
    # CLI가 인자를 그대로 파이프라인 함수에 넘기고, 반환된 개수를 출력하는지 확인한다(요약 로직 자체는
    # tests/test_desc_summarizer.py가 검증 — 여기는 배선만 확인).
    monkeypatch.setattr(settings, "project_root", tmp_path)
    from pipeline import desc_summarizer

    captured = {}

    def fake_summarize(collection, min_candidates=None):
        captured["collection"] = collection
        captured["min_candidates"] = min_candidates
        return 3

    monkeypatch.setattr(desc_summarizer, "summarize_descriptions", fake_summarize)

    graphrag_cli.main(["init"])
    graphrag_cli.main(
        ["summarize-descriptions", "--collection", "사업A", "--min-candidates", "5"]
    )

    assert captured == {"collection": "사업A", "min_candidates": 5}
    assert "3개" in capsys.readouterr().out


def test_cli_bridge_add_and_list(tmp_path, monkeypatch, capsys):
    # bridge add로 사업 간 같은 대상을 연결하고 bridge list에 보이는지 확인한다.
    monkeypatch.setattr(settings, "project_root", tmp_path)
    from db import graph_manager

    graphrag_cli.main(["init"])
    graph_manager.upsert_entity("사업A", "김변호사", "Person", "")
    graph_manager.upsert_entity("사업B", "김변호사", "Person", "")

    graphrag_cli.main(["bridge", "add", "--from", "사업A:김변호사", "--to", "사업B:김변호사"])
    capsys.readouterr()
    graphrag_cli.main(["bridge", "list"])

    out = capsys.readouterr().out
    assert "김변호사" in out
    assert "사업A" in out and "사업B" in out
