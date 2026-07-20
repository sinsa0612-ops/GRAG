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


def test_cli_ingest_backend_flag_threads_to_process_file(tmp_path, monkeypatch):
    # --backend ollama 가 process_file까지 그대로 전달되고(라우터가 어댑터를 고를 수 있게),
    # 플래그가 없으면 기본 None(=Gemini)으로 전달되는지 확인한다.
    monkeypatch.setattr(settings, "project_root", tmp_path)
    seen = {}

    def spy_process_file(path, collection, *, glean_rounds=None, backend=None, **kwargs):
        seen["backend"] = backend
        return True

    monkeypatch.setattr(ingest, "process_file", spy_process_file)
    graphrag_cli.main(["init"])
    memo = tmp_path / "memo.md"
    memo.write_text("아무 메모", encoding="utf-8")

    graphrag_cli.main(["ingest", str(memo), "--collection", "사업A", "--backend", "ollama"])
    assert seen["backend"] == "ollama"

    memo.write_text("아무 메모2", encoding="utf-8")  # 해시가 달라야 재처리됨
    graphrag_cli.main(["ingest", str(memo), "--collection", "사업A"])
    assert seen["backend"] is None  # 플래그 없으면 기본 Gemini 경로


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


def test_cli_communities_build_populates_sqlite(tmp_path, monkeypatch, capsys):
    # LLM 없이(순수 CPU) 그래프를 직접 심고, communities build --no-reports가 SQLite를 채우고 dirty를
    # 해제하는지 확인한다(탐지 단계만 — 리포트 생성은 아래 별도 테스트에서 mock으로 검증).
    monkeypatch.setattr(settings, "project_root", tmp_path)
    from db import graph_manager, sqlite_manager

    graphrag_cli.main(["init"])
    for name in ["A", "B", "C"]:
        graph_manager.upsert_entity("사업A", name, "OTHER", "")
    graph_manager.upsert_relation("사업A", "A", "B", "RELATED_TO", "", "doc1")
    graph_manager.upsert_relation("사업A", "B", "C", "RELATED_TO", "", "doc1")
    assert sqlite_manager.is_communities_dirty("사업A") is True

    graphrag_cli.main(["communities", "build", "--collection", "사업A", "--no-reports"])

    stored = sqlite_manager.get_communities("사업A")
    assert stored
    assert all(c["collection"] == "사업A" for c in stored)
    assert sqlite_manager.is_communities_dirty("사업A") is False
    assert "저장 완료" in capsys.readouterr().out


def test_cli_communities_build_on_empty_collection_stores_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "project_root", tmp_path)
    from db import sqlite_manager

    graphrag_cli.main(["init"])

    graphrag_cli.main(["communities", "build", "--collection", "빈사업", "--no-reports"])

    assert sqlite_manager.get_communities("빈사업") == []
    assert "엔티티가 없어" in capsys.readouterr().out


def test_cli_communities_build_requires_collection(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "project_root", tmp_path)

    graphrag_cli.main(["init"])
    graphrag_cli.main(["communities", "build"])

    assert "--collection이 필요합니다" in capsys.readouterr().out


def test_cli_communities_build_generates_reports_by_default(tmp_path, monkeypatch, capsys):
    # [M3] --no-reports를 안 주면 탐지 후 community_reporter.generate_reports가 자동 호출돼야 한다.
    monkeypatch.setattr(settings, "project_root", tmp_path)
    from db import graph_manager
    from pipeline import community_reporter

    graphrag_cli.main(["init"])
    graph_manager.upsert_entity("사업A", "A", "OTHER", "")
    graph_manager.upsert_entity("사업A", "B", "OTHER", "")
    graph_manager.upsert_relation("사업A", "A", "B", "RELATED_TO", "", "doc1")

    captured = {}

    def fake_generate_reports(collection, **kwargs):
        captured["collection"] = collection
        return 2

    monkeypatch.setattr(community_reporter, "generate_reports", fake_generate_reports)

    graphrag_cli.main(["communities", "build", "--collection", "사업A"])

    assert captured["collection"] == "사업A"
    assert "리포트 2개 생성 완료" in capsys.readouterr().out


def test_cli_communities_build_no_reports_skips_report_generation(tmp_path, monkeypatch, capsys):
    # [M3] --no-reports를 주면 community_reporter가 아예 호출되지 않아야 한다.
    monkeypatch.setattr(settings, "project_root", tmp_path)
    from db import graph_manager
    from pipeline import community_reporter

    graphrag_cli.main(["init"])
    graph_manager.upsert_entity("사업A", "A", "OTHER", "")

    calls = []
    monkeypatch.setattr(
        community_reporter, "generate_reports", lambda *a, **k: calls.append(1) or 0
    )

    graphrag_cli.main(["communities", "build", "--collection", "사업A", "--no-reports"])

    assert calls == []
    assert "리포트" not in capsys.readouterr().out


def test_cli_communities_status_prints_counts_and_stale(tmp_path, monkeypatch, capsys):
    # [M3] communities status가 컬렉션별 커뮤니티/리포트 개수와 stale 여부를 출력하는지 확인한다.
    monkeypatch.setattr(settings, "project_root", tmp_path)
    from db import sqlite_manager

    graphrag_cli.main(["init"])
    sqlite_manager.replace_communities(
        "사업A",
        [
            {
                "collection": "사업A", "community_id": "c1", "level": 0,
                "parent_community_id": None, "entity_names": ["A"], "size": 1,
                "graph_signature": "sig",
            }
        ],
    )
    sqlite_manager.upsert_community_report("사업A", "c1", 0, "제목", "요약", 5.0)
    sqlite_manager.clear_communities_dirty("사업A", "sig")
    sqlite_manager.mark_communities_dirty("사업A")

    graphrag_cli.main(["communities", "status", "--collection", "사업A"])

    out = capsys.readouterr().out
    assert "사업A" in out
    assert "커뮤니티 1개" in out
    assert "리포트 1개" in out
    assert "stale" in out


# [M2 회귀] cmd_delete는 문서 삭제가 no-op이어도, cleanup이 기존 고립 엔티티를 지우면(=그래프 변이)
# 해당 컬렉션을 dirty로 표시해야 한다. 안 그러면 삭제된 엔티티가 낡은 커뮤니티 멤버로 남는다.
def test_cli_delete_marks_dirty_when_cleanup_removes_isolated(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "project_root", tmp_path)
    from db import graph_manager, sqlite_manager
    from pipeline import community_detector

    graphrag_cli.main(["init"])
    graph_manager.upsert_entity("사업A", "외톨이", "OTHER", "")  # 관계 없는 고립 엔티티
    comms, sig = community_detector.detect_communities("사업A")
    sqlite_manager.replace_communities("사업A", comms)
    sqlite_manager.clear_communities_dirty("사업A", sig)
    assert sqlite_manager.is_communities_dirty("사업A") is False

    # 존재하지 않는 파일 삭제 → delete_document는 dirty를 세우지 않지만 cleanup이 외톨이를 지운다
    graphrag_cli.main(["delete", "없는파일.md", "--collection", "사업A"])

    assert sqlite_manager.is_communities_dirty("사업A") is True


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
