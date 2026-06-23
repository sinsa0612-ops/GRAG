# db_status.print_status가 에러 없이 현재 DB 현황을 정확히 집계하는지 확인한다.
import logging

import db_status
from db import graph_manager, sqlite_manager


def test_print_status_reports_correct_counts(caplog):
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    sqlite_manager.upsert_document("doc_1", "c1", "memo.md", "내용", "hash123")
    graph_manager.upsert_entity("c1", "강택리", "Person", "")
    graph_manager.upsert_entity("c1", "ISA계좌", "Asset", "")
    graph_manager.upsert_relation("c1", "강택리", "ISA계좌", "MANAGES", "2026-01", "doc_1")

    with caplog.at_level(logging.INFO):
        db_status.print_status()

    output = "\n".join(record.message for record in caplog.records)
    assert "문서 수: 1" in output
    assert "벡터 청크 수: 0" in output
    assert "엔티티 수: 2" in output
    assert "관계 수: 1" in output
    assert "Asset" in output
    assert "MANAGES" in output
    assert "고아 데이터(source_id): 0개" in output
    assert "고립된 엔티티(관계 없음, 자동삭제 안 함): 0개" in output
