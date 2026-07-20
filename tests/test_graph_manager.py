# KuzuDB 그래프 매니저 — 컬렉션 격리 Entity/RELATION 스키마의 CRUD와 병합 로직을 검증한다.
import logging

from db import graph_manager

# 테스트 기본 컬렉션.
C = "c1"


def test_entity_exists():
    graph_manager.init_schema()
    assert graph_manager.entity_exists(C, "강택리") is False

    graph_manager.upsert_entity(C, "강택리", "Person", "")
    assert graph_manager.entity_exists(C, "강택리") is True


def test_upsert_entity_initializes_aliases_only_on_creation():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "기획자")
    graph_manager.add_alias(C, "강택리", "택리")

    # 같은 이름으로 다시 upsert해도(설명만 바뀌어도) 기존 alias가 지워지면 안 된다.
    graph_manager.upsert_entity(C, "강택리", "Person", "수정된 설명")

    assert graph_manager.find_canonical_name(C, "택리") == "강택리"


def test_find_canonical_name_matches_name_alias_or_nothing():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "절세용 계좌")
    graph_manager.add_alias(C, "ISA계좌", "ISA 계좌")

    assert graph_manager.find_canonical_name(C, "ISA계좌") == "ISA계좌"
    assert graph_manager.find_canonical_name(C, "ISA 계좌") == "ISA계좌"
    assert graph_manager.find_canonical_name(C, "전혀 다른 이름") is None


def test_add_alias_is_idempotent():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "")
    graph_manager.add_alias(C, "ISA계좌", "ISA 계좌")
    graph_manager.add_alias(C, "ISA계좌", "ISA 계좌")

    entity = graph_manager.get_entity(C, "ISA계좌")
    assert entity["aliases"] == ["ISA 계좌"]


def test_get_known_entity_names():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "")
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "")

    assert set(graph_manager.get_known_entity_names([C])) == {"강택리", "ISA계좌"}


def test_upsert_relation_skips_and_warns_when_entity_missing(caplog):
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "")

    with caplog.at_level(logging.WARNING):
        graph_manager.upsert_relation(C, "강택리", "ISA계좌", "MANAGES", "2026-01", "doc1")

    assert graph_manager.get_outgoing_relations(C, "강택리") == []
    assert any("존재하지 않음" in record.message for record in caplog.records)


def test_update_entity_description_leaves_type_and_aliases_untouched():
    # M1.5 설명요약 배치가 쓰는 함수 — description만 바뀌고 type/aliases는 그대로여야 한다.
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "옛 설명")
    graph_manager.add_alias(C, "강택리", "택리")

    graph_manager.update_entity_description(C, "강택리", "통합된 새 설명")

    entity = graph_manager.get_entity(C, "강택리")
    assert entity["description"] == "통합된 새 설명"
    assert entity["type"] == "Person"
    assert entity["aliases"] == ["택리"]


def test_get_entity_includes_aliases():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "절세용 계좌")
    graph_manager.add_alias(C, "ISA계좌", "ISA 계좌")

    entity = graph_manager.get_entity(C, "ISA계좌")
    assert entity == {
        "collection": C,
        "name": "ISA계좌",
        "type": "Asset",
        "description": "절세용 계좌",
        "aliases": ["ISA 계좌"],
    }
    assert graph_manager.get_entity(C, "없는엔티티") is None


def test_get_incoming_relations():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "")
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "")
    graph_manager.upsert_relation(C, "강택리", "ISA계좌", "MANAGES", "2026-01", "doc1")

    assert graph_manager.get_incoming_relations(C, "ISA계좌") == [
        {"source": "강택리", "predicate": "MANAGES", "valid_from": "2026-01", "source_doc": "doc1"}
    ]
    assert graph_manager.get_incoming_relations(C, "강택리") == []


def test_find_and_cleanup_isolated_entities():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "")
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "")
    graph_manager.upsert_entity(C, "외톨이", "Person", "")
    graph_manager.upsert_relation(C, "강택리", "ISA계좌", "MANAGES", "", "doc1")

    assert graph_manager.find_isolated_entities() == [{"collection": C, "name": "외톨이"}]

    removed = graph_manager.cleanup_isolated_entities()

    assert removed == 1
    names = {e["name"] for e in graph_manager.get_all_entities()}
    assert names == {"강택리", "ISA계좌"}


def test_get_all_source_docs():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "A", "Person", "")
    graph_manager.upsert_entity(C, "B", "Person", "")
    graph_manager.upsert_relation(C, "A", "B", "KNOWS", "", "doc1")

    assert graph_manager.get_all_source_docs() == {"doc1"}


def test_get_all_relations():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "")
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "")
    graph_manager.upsert_relation(C, "강택리", "ISA계좌", "MANAGES", "2026-01", "doc1")

    relations = graph_manager.get_all_relations()
    assert relations == [
        {
            "source": "강택리",
            "predicate": "MANAGES",
            "target": "ISA계좌",
            "valid_from": "2026-01",
            "source_doc": "doc1",
            "collection": C,
        }
    ]


def test_entity_upsert_and_list():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "기획자")
    graph_manager.upsert_entity(C, "강택리", "Person", "수정된 설명")

    entities = graph_manager.get_all_entities()
    assert len(entities) == 1
    assert entities[0]["description"] == "수정된 설명"


def test_relation_upsert_and_query():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "")
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "")
    graph_manager.upsert_relation(C, "강택리", "ISA계좌", "MANAGES", "2026-01", "doc1")

    relations = graph_manager.get_outgoing_relations(C, "강택리")
    assert relations == [
        {"predicate": "MANAGES", "target": "ISA계좌", "valid_from": "2026-01", "source_doc": "doc1"}
    ]


def test_delete_relations_by_source_doc():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "")
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "")
    graph_manager.upsert_relation(C, "강택리", "ISA계좌", "MANAGES", "2026-01", "doc1")

    graph_manager.delete_relations_by_source_doc("doc1")

    assert graph_manager.get_outgoing_relations(C, "강택리") == []


def test_delete_entity_removes_node_and_relations():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "")
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "")
    graph_manager.upsert_relation(C, "강택리", "ISA계좌", "MANAGES", "2026-01", "doc1")

    graph_manager.delete_entity(C, "강택리")

    names = {e["name"] for e in graph_manager.get_all_entities()}
    assert names == {"ISA계좌"}


def test_count_entities_and_relations():
    graph_manager.init_schema()
    assert graph_manager.count_entities() == 0
    assert graph_manager.count_relations() == 0

    graph_manager.upsert_entity(C, "강택리", "Person", "")
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "")
    graph_manager.upsert_relation(C, "강택리", "ISA계좌", "MANAGES", "2026-01", "doc1")

    assert graph_manager.count_entities() == 2
    assert graph_manager.count_relations() == 1


def test_get_known_types_and_predicates():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "")
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "")
    graph_manager.upsert_relation(C, "강택리", "ISA계좌", "MANAGES", "2026-01", "doc1")

    assert set(graph_manager.get_known_types()) == {"Person", "Asset"}
    assert set(graph_manager.get_known_predicates()) == {"MANAGES"}


def test_upsert_relation_preserves_distinct_valid_from_as_separate_edges():
    # 같은 (주체, 대상, predicate)라도 valid_from(시점)이 다르면 별개의 관계로 보존돼야 한다(이력 손실 방지).
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "")
    graph_manager.upsert_entity(C, "A사", "Organization", "")
    graph_manager.upsert_relation(C, "강택리", "A사", "WORKS_AT", "2020-01", "doc1")
    graph_manager.upsert_relation(C, "강택리", "A사", "WORKS_AT", "2024-01", "doc2")

    rels = graph_manager.get_outgoing_relations(C, "강택리")
    assert sorted(r["valid_from"] for r in rels) == ["2020-01", "2024-01"]
    assert graph_manager.count_relations() == 2


def test_upsert_relation_same_valid_from_is_idempotent():
    # 같은 시점의 같은 관계를 다시 넣으면(재처리 등) 중복 생성되지 않고 source_doc만 갱신된다.
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "")
    graph_manager.upsert_entity(C, "A사", "Organization", "")
    graph_manager.upsert_relation(C, "강택리", "A사", "WORKS_AT", "2020-01", "doc1")
    graph_manager.upsert_relation(C, "강택리", "A사", "WORKS_AT", "2020-01", "doc2")

    rels = graph_manager.get_outgoing_relations(C, "강택리")
    assert len(rels) == 1
    assert rels[0]["source_doc"] == "doc2"


def test_merge_entity_into_transfers_edges_and_removes_node():
    graph_manager.init_schema()
    for name, entity_type in [("A", "Person"), ("B", "Person"), ("C", "Event"), ("D", "Person")]:
        graph_manager.upsert_entity(C, name, entity_type, "")
    graph_manager.upsert_relation(C, "B", "C", "ATTENDS", "2026-01", "doc1")
    graph_manager.upsert_relation(C, "D", "B", "KNOWS", "2026-01", "doc1")

    graph_manager.merge_entity_into(C, keep_name="A", drop_name="B")

    names = {e["name"] for e in graph_manager.get_all_entities()}
    assert names == {"A", "C", "D"}

    a_targets = {r["target"] for r in graph_manager.get_outgoing_relations(C, "A")}
    assert a_targets == {"C"}

    d_targets = {r["target"] for r in graph_manager.get_outgoing_relations(C, "D")}
    assert d_targets == {"A"}


def test_merge_entity_into_preserves_distinct_valid_from_edges():
    # 엔티티 병합 후에도 시점이 다른 관계들이 합쳐지지 않고 그대로 보존돼야 한다.
    graph_manager.init_schema()
    for name in ["강택리_정식", "강택리_중복", "A사"]:
        graph_manager.upsert_entity(C, name, "Person", "")
    graph_manager.upsert_relation(C, "강택리_중복", "A사", "WORKS_AT", "2020-01", "doc1")
    graph_manager.upsert_relation(C, "강택리_중복", "A사", "WORKS_AT", "2024-01", "doc2")

    graph_manager.merge_entity_into(C, keep_name="강택리_정식", drop_name="강택리_중복")

    rels = graph_manager.get_outgoing_relations(C, "강택리_정식")
    assert sorted(r["valid_from"] for r in rels) == ["2020-01", "2024-01"]


def test_collections_are_isolated():
    # 같은 이름이라도 컬렉션이 다르면 별개 노드로 격리되고, 컬렉션별 조회가 서로 섞이지 않아야 한다.
    graph_manager.init_schema()
    graph_manager.upsert_entity("사업A", "김부장", "Person", "A사업의 김부장")
    graph_manager.upsert_entity("사업B", "김부장", "Person", "B사업의 김부장")

    # 컬렉션별로 1명씩, 전체로는 2개의 별개 노드.
    assert graph_manager.count_entities(["사업A"]) == 1
    assert graph_manager.count_entities(["사업B"]) == 1
    assert graph_manager.count_entities() == 2

    # 설명이 컬렉션마다 다르게 유지된다(한 노드로 병합되지 않음).
    assert graph_manager.get_entity("사업A", "김부장")["description"] == "A사업의 김부장"
    assert graph_manager.get_entity("사업B", "김부장")["description"] == "B사업의 김부장"

    # 어휘 힌트도 컬렉션 범위로만.
    assert graph_manager.get_known_entity_names(["사업A"]) == ["김부장"]
    assert set(graph_manager.get_all_collections()) == {"사업A", "사업B"}


def test_relations_do_not_cross_collections():
    # 관계 조회도 컬렉션 범위로 격리된다.
    graph_manager.init_schema()
    graph_manager.upsert_entity("사업A", "김부장", "Person", "")
    graph_manager.upsert_entity("사업A", "A프로젝트", "CONCEPT", "")
    graph_manager.upsert_entity("사업B", "김부장", "Person", "")
    graph_manager.upsert_relation("사업A", "김부장", "A프로젝트", "LEADS", "", "docA")

    # 사업A의 김부장만 관계가 있고, 사업B의 김부장은 없다.
    assert len(graph_manager.get_outgoing_relations("사업A", "김부장")) == 1
    assert graph_manager.get_outgoing_relations("사업B", "김부장") == []
    assert graph_manager.count_relations(["사업B"]) == 0


def test_delete_collection_removes_only_that_collection():
    # 한 컬렉션을 통째로 삭제하면 그 컬렉션의 엔티티/관계만 사라지고 다른 컬렉션은 그대로여야 한다.
    graph_manager.init_schema()
    graph_manager.upsert_entity("사업A", "김부장", "Person", "")
    graph_manager.upsert_entity("사업A", "A프로젝트", "CONCEPT", "")
    graph_manager.upsert_relation("사업A", "김부장", "A프로젝트", "LEADS", "", "docA")
    graph_manager.upsert_entity("사업B", "이대리", "Person", "")

    graph_manager.delete_collection("사업A")

    assert graph_manager.count_entities(["사업A"]) == 0
    assert graph_manager.count_relations(["사업A"]) == 0
    assert graph_manager.count_entities(["사업B"]) == 1


def test_bridge_connects_entities_across_collections():
    # 서로 다른 사업의 같은 대상을 병합 없이 SAME_AS로 잇고, 무방향으로 조회된다.
    graph_manager.init_schema()
    graph_manager.upsert_entity("사업A", "김변호사", "Person", "법률 자문")
    graph_manager.upsert_entity("사업B", "김변호사", "Person", "투자 자문")

    assert graph_manager.add_bridge("사업A", "김변호사", "사업B", "김변호사") is True
    assert graph_manager.is_bridged("사업A", "김변호사", "사업B", "김변호사") is True
    # 순서를 바꿔도 같은 브릿지로 인식된다(무방향).
    assert graph_manager.is_bridged("사업B", "김변호사", "사업A", "김변호사") is True

    twins_a = graph_manager.get_bridges("사업A", "김변호사")
    assert {"collection": "사업B", "name": "김변호사"} in twins_a
    twins_b = graph_manager.get_bridges("사업B", "김변호사")
    assert {"collection": "사업A", "name": "김변호사"} in twins_b

    # 두 노드는 합쳐지지 않고 각자 그대로 존재한다(격벽 유지).
    assert graph_manager.count_entities(["사업A"]) == 1
    assert graph_manager.count_entities(["사업B"]) == 1


def test_bridge_skips_when_entity_missing_or_self():
    graph_manager.init_schema()
    graph_manager.upsert_entity("사업A", "김변호사", "Person", "")

    # 상대 엔티티가 없으면 연결되지 않는다.
    assert graph_manager.add_bridge("사업A", "김변호사", "사업B", "없는사람") is False
    # 같은 엔티티끼리는 연결하지 않는다.
    assert graph_manager.add_bridge("사업A", "김변호사", "사업A", "김변호사") is False


def test_get_bridges_respects_collection_scope():
    # 스코프 밖 사업의 브릿지는 끌어오지 않는다(격벽 존중).
    graph_manager.init_schema()
    graph_manager.upsert_entity("사업A", "공통거래처", "ORGANIZATION", "")
    graph_manager.upsert_entity("사업B", "공통거래처", "ORGANIZATION", "")
    graph_manager.upsert_entity("사업C", "공통거래처", "ORGANIZATION", "")
    graph_manager.add_bridge("사업A", "공통거래처", "사업B", "공통거래처")
    graph_manager.add_bridge("사업A", "공통거래처", "사업C", "공통거래처")

    scoped = graph_manager.get_bridges("사업A", "공통거래처", ["사업A", "사업B"])
    names = {(t["collection"], t["name"]) for t in scoped}
    assert ("사업B", "공통거래처") in names
    assert ("사업C", "공통거래처") not in names


def test_remove_bridge():
    graph_manager.init_schema()
    graph_manager.upsert_entity("사업A", "김변호사", "Person", "")
    graph_manager.upsert_entity("사업B", "김변호사", "Person", "")
    graph_manager.add_bridge("사업A", "김변호사", "사업B", "김변호사")

    graph_manager.remove_bridge("사업B", "김변호사", "사업A", "김변호사")  # 순서 무관 해제

    assert graph_manager.is_bridged("사업A", "김변호사", "사업B", "김변호사") is False
    assert graph_manager.list_all_bridges() == []
