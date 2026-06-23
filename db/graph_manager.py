# KùzuDB(Graph DB) 전담 — 컬렉션(사업)별로 격리된 Entity/RELATION 스키마의 CRUD만 책임진다.
# 엔티티 정체성은 (collection, name)이다. Kuzu는 단일 PK만 지원하므로 합성 id(collection + 구분자 + name)를 PK로 쓴다.
# 그래서 같은 이름이라도 컬렉션이 다르면 별개 노드로 격리된다(무관한 사업 간 교차 오염 방지).
import logging

import kuzu

from config import settings

logger = logging.getLogger(__name__)

_db: kuzu.Database | None = None
_conn: kuzu.Connection | None = None

# 엔티티 합성 id를 만들 때 컬렉션과 이름을 가르는 구분자(평범한 텍스트엔 등장하지 않는 Unit Separator).
_SEP = "\x1f"


# 컬렉션과 이름을 합쳐 엔티티의 합성 PK(id)를 만든다.
def _entity_id(collection: str, name: str) -> str:
    return f"{collection}{_SEP}{name}"


# 조회/집계 쿼리에 붙일 컬렉션 필터 절을 만든다. collections가 None이면 필터 없음(= 전체 컬렉션 = 행정 종합).
def _where_collection(var: str, collections: list[str] | None) -> tuple[str, dict]:
    if collections is None:
        return "", {}
    return f" WHERE {var}.collection IN $collections", {"collections": collections}


# Kuzu 커넥션을 1회만 생성해 재사용한다.
def _get_connection() -> kuzu.Connection:
    global _db, _conn
    if _conn is None:
        settings.db_dir.mkdir(parents=True, exist_ok=True)
        _db = kuzu.Database(str(settings.kuzu_path))
        _conn = kuzu.Connection(_db)
    return _conn


# 열려 있는 Kuzu 커넥션을 명시적으로 해제한다. Windows에서는 파일이 열린 채로
# 백업/복구처럼 통째로 복사하면 PermissionError가 나기 때문에, 그 전에 호출해 락을 풀어준다.
# 다음 DB 접근 시 _get_connection()이 자동으로 다시 연다.
def close_connection() -> None:
    global _db, _conn
    _conn = None
    _db = None


# 이미 존재하는 테이블/스키마면 무시하고 넘어간다.
def _execute_if_absent(conn: kuzu.Connection, query: str) -> None:
    try:
        conn.execute(query)
    except RuntimeError as exc:
        if "already exists" not in str(exc).lower():
            raise


# 컬렉션 인식 Entity 노드 테이블과 RELATION 관계 테이블을 최초 1회 생성한다.
# Entity PK는 합성 id(collection+name), 별도로 collection/name 속성을 둬서 컬렉션 단위 필터를 건다.
def init_schema() -> None:
    conn = _get_connection()
    _execute_if_absent(
        conn,
        "CREATE NODE TABLE Entity "
        "(id STRING, collection STRING, name STRING, type STRING, description STRING, "
        "aliases STRING[], PRIMARY KEY(id))",
    )
    _execute_if_absent(
        conn,
        "CREATE REL TABLE RELATION "
        "(FROM Entity TO Entity, predicate STRING, valid_from STRING, source_doc STRING, collection STRING)",
    )


# 엔티티 노드를 새로 만들거나 기존 노드의 타입/설명을 갱신한다(해당 컬렉션 범위 안에서).
# 신규 생성일 때만 collection/name/aliases를 초기화하고, 기존 노드의 aliases는 건드리지 않는다.
def upsert_entity(collection: str, name: str, entity_type: str, description: str = "") -> None:
    conn = _get_connection()
    conn.execute(
        """
        MERGE (e:Entity {id: $id})
        ON CREATE SET e.collection = $collection, e.name = $name, e.type = $type,
                      e.description = $description, e.aliases = []
        ON MATCH SET e.type = $type, e.description = $description
        """,
        {
            "id": _entity_id(collection, name),
            "collection": collection,
            "name": name,
            "type": entity_type,
            "description": description,
        },
    )


# 해당 컬렉션 안에 이름으로 엔티티가 존재하는지 확인한다 (관계 저장 전 무결성 검사용).
def entity_exists(collection: str, name: str) -> bool:
    conn = _get_connection()
    result = conn.execute(
        "MATCH (e:Entity {id: $id}) RETURN e.id", {"id": _entity_id(collection, name)}
    )
    return result.has_next()


# candidate가 해당 컬렉션 안에서 어떤 엔티티의 정식 이름이거나 alias와 정확히 일치하면 그 정식 이름을 반환한다.
# 일치하는 게 전혀 없으면 None (= 새로운 엔티티로 취급해야 함).
def find_canonical_name(collection: str, candidate: str) -> str | None:
    if entity_exists(collection, candidate):
        return candidate

    conn = _get_connection()
    result = conn.execute(
        "MATCH (e:Entity {collection: $collection}) "
        "WHERE list_contains(e.aliases, $candidate) RETURN e.name",
        {"collection": collection, "candidate": candidate},
    )
    return result.get_next()[0] if result.has_next() else None


# canonical_name 엔티티의 alias 목록에 alias를 추가한다. 이미 있으면 중복 추가하지 않는다.
def add_alias(collection: str, canonical_name: str, alias: str) -> None:
    conn = _get_connection()
    conn.execute(
        """
        MATCH (e:Entity {id: $id})
        WHERE NOT list_contains(e.aliases, $alias)
        SET e.aliases = list_concat(e.aliases, [$alias])
        """,
        {"id": _entity_id(collection, canonical_name), "alias": alias},
    )


# 지정한 컬렉션 범위의 모든 엔티티 정식 이름 목록을 가져온다(collections=None이면 전체).
# 추출 프롬프트에 보여줘서 LLM이 같은 대상을 가리킬 때 기존 이름을 그대로 쓰도록 유도하는 데 쓴다.
def get_known_entity_names(collections: list[str] | None = None) -> list[str]:
    conn = _get_connection()
    where, params = _where_collection("e", collections)
    result = conn.execute(f"MATCH (e:Entity){where} RETURN e.name", params)
    names = []
    while result.has_next():
        names.append(result.get_next()[0])
    return names


# 두 엔티티(같은 컬렉션) 사이의 관계를 새로 만들거나 갱신한다.
# 관계 식별 키는 (predicate, valid_from)이다 — 시점이 다르면 별개 엣지로 공존시켜 시계열 이력을 보존한다.
# 양쪽 엔티티가 그 컬렉션에 실제로 존재하지 않으면 경고를 남기고 건너뛴다.
def upsert_relation(
    collection: str,
    source: str,
    target: str,
    predicate: str,
    valid_from: str = "",
    source_doc: str = "",
) -> None:
    if not entity_exists(collection, source) or not entity_exists(collection, target):
        logger.warning(
            "관계 저장 건너뜀 — 엔티티가 존재하지 않음: [%s] %s -[%s]-> %s",
            collection,
            source,
            predicate,
            target,
        )
        return

    conn = _get_connection()
    conn.execute(
        """
        MATCH (a:Entity {id: $source_id}), (b:Entity {id: $target_id})
        MERGE (a)-[r:RELATION {predicate: $predicate, valid_from: $valid_from}]->(b)
        SET r.source_doc = $source_doc, r.collection = $collection
        """,
        {
            "source_id": _entity_id(collection, source),
            "target_id": _entity_id(collection, target),
            "predicate": predicate,
            "valid_from": valid_from,
            "source_doc": source_doc,
            "collection": collection,
        },
    )


# 특정 엔티티에서 나가는 관계와 대상을 모두 조회한다 (2-hop 추적, 질의응답 등에 사용).
def get_outgoing_relations(collection: str, name: str) -> list[dict]:
    conn = _get_connection()
    result = conn.execute(
        "MATCH (a:Entity {id: $id})-[r:RELATION]->(b:Entity) "
        "RETURN r.predicate, b.name, r.valid_from, r.source_doc",
        {"id": _entity_id(collection, name)},
    )
    relations = []
    while result.has_next():
        predicate, target, valid_from, source_doc = result.get_next()
        relations.append(
            {
                "predicate": predicate,
                "target": target,
                "valid_from": valid_from,
                "source_doc": source_doc,
            }
        )
    return relations


# 특정 엔티티로 들어오는 관계와 그 주체를 모두 조회한다 (양방향 관계 파악, 질의응답 등에 사용).
def get_incoming_relations(collection: str, name: str) -> list[dict]:
    conn = _get_connection()
    result = conn.execute(
        "MATCH (a:Entity)-[r:RELATION]->(b:Entity {id: $id}) "
        "RETURN a.name, r.predicate, r.valid_from, r.source_doc",
        {"id": _entity_id(collection, name)},
    )
    relations = []
    while result.has_next():
        source, predicate, valid_from, source_doc = result.get_next()
        relations.append(
            {
                "source": source,
                "predicate": predicate,
                "valid_from": valid_from,
                "source_doc": source_doc,
            }
        )
    return relations


# 지정한 컬렉션 범위의 모든 엔티티(컬렉션/이름/타입/설명)를 조회한다(collections=None이면 전체).
def get_all_entities(collections: list[str] | None = None) -> list[dict]:
    conn = _get_connection()
    where, params = _where_collection("e", collections)
    result = conn.execute(
        f"MATCH (e:Entity){where} RETURN e.collection, e.name, e.type, e.description", params
    )
    entities = []
    while result.has_next():
        collection, name, entity_type, description = result.get_next()
        entities.append(
            {"collection": collection, "name": name, "type": entity_type, "description": description}
        )
    return entities


# 지정한 컬렉션 범위의 모든 관계를 가져온다(collections=None이면 전체). 시각화/종합 조회에 사용.
def get_all_relations(collections: list[str] | None = None) -> list[dict]:
    conn = _get_connection()
    where, params = _where_collection("r", collections)
    result = conn.execute(
        "MATCH (a:Entity)-[r:RELATION]->(b:Entity)"
        f"{where} "
        "RETURN a.name, r.predicate, b.name, r.valid_from, r.source_doc, r.collection",
        params,
    )
    relations = []
    while result.has_next():
        source, predicate, target, valid_from, source_doc, collection = result.get_next()
        relations.append(
            {
                "source": source,
                "predicate": predicate,
                "target": target,
                "valid_from": valid_from,
                "source_doc": source_doc,
                "collection": collection,
            }
        )
    return relations


# 이름으로 엔티티 노드를 직접 삭제한다 (연결된 관계도 함께 사라진다). 수동 정리/오류 수정용.
def delete_entity(collection: str, name: str) -> None:
    conn = _get_connection()
    conn.execute(
        "MATCH (e:Entity {id: $id}) DETACH DELETE e", {"id": _entity_id(collection, name)}
    )


# 한 컬렉션(사업)의 모든 엔티티를 연결된 관계까지 통째로 삭제한다 (컬렉션 전체 삭제 시 호출).
def delete_collection(collection: str) -> None:
    conn = _get_connection()
    conn.execute(
        "MATCH (e:Entity {collection: $collection}) DETACH DELETE e", {"collection": collection}
    )


# 이름으로 엔티티 하나를 조회한다 (별칭 포함). 없으면 None.
def get_entity(collection: str, name: str) -> dict | None:
    conn = _get_connection()
    result = conn.execute(
        "MATCH (e:Entity {id: $id}) RETURN e.collection, e.name, e.type, e.description, e.aliases",
        {"id": _entity_id(collection, name)},
    )
    if not result.has_next():
        return None
    collection_, name_, entity_type, description, aliases = result.get_next()
    return {
        "collection": collection_,
        "name": name_,
        "type": entity_type,
        "description": description,
        "aliases": aliases,
    }


# 어떤 관계에도 연결되지 않은(들어오는/나가는 관계가 전혀 없는) 엔티티를 찾는다.
# 같은 이름이 컬렉션마다 따로 있을 수 있어, 이름만이 아니라 {collection, name}으로 돌려준다.
def find_isolated_entities(collections: list[str] | None = None) -> list[dict]:
    all_keys = {(e["collection"], e["name"]) for e in get_all_entities(collections)}

    conn = _get_connection()
    where, params = _where_collection("r", collections)
    result = conn.execute(
        "MATCH (a:Entity)-[r:RELATION]->(b:Entity)"
        f"{where} "
        "RETURN DISTINCT a.collection, a.name, b.collection, b.name",
        params,
    )
    connected: set[tuple[str, str]] = set()
    while result.has_next():
        coll_a, name_a, coll_b, name_b = result.get_next()
        connected.add((coll_a, name_a))
        connected.add((coll_b, name_b))

    return [{"collection": c, "name": n} for c, n in sorted(all_keys - connected)]


# 고립된 엔티티(관계가 전혀 없는 노드)를 모두 삭제하고, 삭제한 개수를 반환한다.
def cleanup_isolated_entities(collections: list[str] | None = None) -> int:
    isolated = find_isolated_entities(collections)
    for entity in isolated:
        delete_entity(entity["collection"], entity["name"])
    return len(isolated)


# 그래프의 관계들이 참조하고 있는 모든 source_doc 값을 가져온다 (고아 데이터 탐지용 — 전역).
def get_all_source_docs() -> set[str]:
    conn = _get_connection()
    result = conn.execute("MATCH ()-[r:RELATION]->() RETURN DISTINCT r.source_doc")
    source_docs = set()
    while result.has_next():
        source_docs.add(result.get_next()[0])
    return source_docs


# 지정한 컬렉션 범위의 엔티티 개수를 센다 (상태 확인용, collections=None이면 전체).
def count_entities(collections: list[str] | None = None) -> int:
    conn = _get_connection()
    where, params = _where_collection("e", collections)
    result = conn.execute(f"MATCH (e:Entity){where} RETURN COUNT(*)", params)
    return result.get_next()[0] if result.has_next() else 0


# 지정한 컬렉션 범위의 관계 개수를 센다 (상태 확인용, collections=None이면 전체).
def count_relations(collections: list[str] | None = None) -> int:
    conn = _get_connection()
    where, params = _where_collection("r", collections)
    result = conn.execute(f"MATCH ()-[r:RELATION]->(){where} RETURN COUNT(*)", params)
    return result.get_next()[0] if result.has_next() else 0


# 지정한 컬렉션 범위에서 실제로 쓰이고 있는 엔티티 타입 목록을 중복 없이 가져온다.
def get_known_types(collections: list[str] | None = None) -> list[str]:
    conn = _get_connection()
    where, params = _where_collection("e", collections)
    result = conn.execute(f"MATCH (e:Entity){where} RETURN DISTINCT e.type", params)
    types = []
    while result.has_next():
        types.append(result.get_next()[0])
    return types


# 지정한 컬렉션 범위에서 실제로 쓰이고 있는 관계 이름(predicate) 목록을 중복 없이 가져온다.
def get_known_predicates(collections: list[str] | None = None) -> list[str]:
    conn = _get_connection()
    where, params = _where_collection("r", collections)
    result = conn.execute(f"MATCH ()-[r:RELATION]->(){where} RETURN DISTINCT r.predicate", params)
    predicates = []
    while result.has_next():
        predicates.append(result.get_next()[0])
    return predicates


# 현재 그래프에 존재하는 모든 컬렉션 이름을 가져온다 (컬렉션 목록 조회용).
def get_all_collections() -> list[str]:
    conn = _get_connection()
    result = conn.execute("MATCH (e:Entity) RETURN DISTINCT e.collection")
    collections = []
    while result.has_next():
        collections.append(result.get_next()[0])
    return sorted(collections)


# 특정 문서에서 생성된 관계를 모두 삭제한다 (증분 업데이트 시 재처리 전 호출).
# source_doc(source_id)은 문서마다 고유해서 컬렉션을 따로 받지 않아도 정확히 짚인다.
def delete_relations_by_source_doc(source_doc: str) -> None:
    conn = _get_connection()
    conn.execute(
        "MATCH ()-[r:RELATION {source_doc: $source_doc}]->() DELETE r",
        {"source_doc": source_doc},
    )


# drop_name 노드의 모든 관계를 keep_name으로 옮기고 drop_name 노드를 삭제한다(같은 컬렉션 안에서).
# 3개 쿼리를 트랜잭션으로 묶어, 중간에 실패해도 "관계만 옮겨지고 노드는 안 지워진" 반쪼가리 상태가 남지 않게 한다.
def merge_entity_into(collection: str, keep_name: str, drop_name: str) -> None:
    conn = _get_connection()
    keep_id = _entity_id(collection, keep_name)
    drop_id = _entity_id(collection, drop_name)
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(
            """
            MATCH (drop:Entity {id: $drop_id})-[r:RELATION]->(target)
            MATCH (keep:Entity {id: $keep_id})
            MERGE (keep)-[new_r:RELATION {predicate: r.predicate, valid_from: r.valid_from}]->(target)
            SET new_r.source_doc = r.source_doc, new_r.collection = $collection
            """,
            {"drop_id": drop_id, "keep_id": keep_id, "collection": collection},
        )
        conn.execute(
            """
            MATCH (source)-[r:RELATION]->(drop:Entity {id: $drop_id})
            MATCH (keep:Entity {id: $keep_id})
            MERGE (source)-[new_r:RELATION {predicate: r.predicate, valid_from: r.valid_from}]->(keep)
            SET new_r.source_doc = r.source_doc, new_r.collection = $collection
            """,
            {"drop_id": drop_id, "keep_id": keep_id, "collection": collection},
        )
        conn.execute(
            "MATCH (drop:Entity {id: $drop_id}) DETACH DELETE drop",
            {"drop_id": drop_id},
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
