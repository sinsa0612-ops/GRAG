# cleanup_db.main이 고아 관계만 정리하고, 그로 인해 고립된 엔티티 노드는 남겨두는지 확인한다.
# (관계가 없어진 엔티티도 나중에 다른 문서에서 관계가 생길 수 있어 자동 삭제하지 않는다.)
import cleanup_db
from db import graph_manager, sqlite_manager


def test_main_cleans_up_orphan_relations_but_keeps_entity_nodes():
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    # 고아 관계: SQLite에 기록되지 않은 source_doc -> 관계만 지워지고 A, B 노드는 남아야 함.
    graph_manager.upsert_entity("c1", "A", "Person", "")
    graph_manager.upsert_entity("c1", "B", "Person", "")
    graph_manager.upsert_relation("c1", "A", "B", "KNOWS", "", "doc_orphan")

    # 정상 추적되는 관계: SQLite에 문서가 기록돼 있으니 그대로 남아야 함.
    sqlite_manager.upsert_document("doc_1", "c1", "memo.md", "내용", "hash1")
    graph_manager.upsert_entity("c1", "C", "Person", "")
    graph_manager.upsert_entity("c1", "D", "Person", "")
    graph_manager.upsert_relation("c1", "C", "D", "KNOWS", "", "doc_1")

    cleanup_db.main()

    names = {e["name"] for e in graph_manager.get_all_entities()}
    assert names == {"A", "B", "C", "D"}
    assert graph_manager.get_outgoing_relations("c1", "A") == []
    assert {r["target"] for r in graph_manager.get_outgoing_relations("c1", "C")} == {"D"}
