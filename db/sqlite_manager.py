# SQLite(Master DB) 전담 — 문서 원본/해시, 병합 블랙리스트, 일일 API 사용량을 책임진다.
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from config import settings

# Gemini 무료 한도는 태평양시간 자정에 리셋된다. 로컬 날짜로 세면 최대 17시간까지 어긋나므로 태평양 날짜로 집계한다.
# (Windows엔 IANA tz DB가 없어 tzdata 패키지가 필요하다. 없으면 로컬 시간으로 안전하게 폴백한다.)
try:
    from zoneinfo import ZoneInfo

    _PACIFIC: "ZoneInfo | None" = ZoneInfo("America/Los_Angeles")
except Exception:
    _PACIFIC = None


# 사용량 집계에 쓸 '오늘' 날짜 문자열(태평양시간 기준, 폴백 시 로컬)을 만든다.
def _usage_date() -> str:
    now = datetime.now(_PACIFIC) if _PACIFIC else datetime.now()
    return now.date().isoformat()


# SQLite 커넥션을 열고 자동 commit/close까지 책임지는 컨텍스트 매니저.
@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    settings.db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.sqlite_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# documents/merge_blacklist 테이블을 최초 1회 생성한다.
def init_schema() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                source_id TEXT PRIMARY KEY,
                collection TEXT NOT NULL DEFAULT 'default',
                file_name TEXT NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                last_modified DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # 기존(컬렉션 도입 전) DB에는 collection 컬럼이 없으므로 있으면 무시하고 없으면 추가한다.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
        if "collection" not in existing_cols:
            conn.execute(
                "ALTER TABLE documents ADD COLUMN collection TEXT NOT NULL DEFAULT 'default'"
            )
        # 병합 금지 목록 — 컬렉션(사업)별로 격리한다. 같은 이름 쌍이라도 사업이 다르면 별개로 관리(격벽 유지).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS merge_blacklist (
                collection TEXT NOT NULL DEFAULT 'default',
                node_a TEXT NOT NULL,
                node_b TEXT NOT NULL,
                reason TEXT,
                PRIMARY KEY (collection, node_a, node_b)
            )
            """
        )
        # 기존(컬렉션 도입 전) 블랙리스트엔 collection 컬럼이 없다. SQLite는 PK 변경이 불가하므로,
        # 새 스키마 테이블에 데이터를 'default' 컬렉션으로 옮겨 담아 교체한다(자동 마이그레이션).
        bl_cols = {row[1] for row in conn.execute("PRAGMA table_info(merge_blacklist)").fetchall()}
        if "collection" not in bl_cols:
            conn.execute("ALTER TABLE merge_blacklist RENAME TO merge_blacklist_old")
            conn.execute(
                """
                CREATE TABLE merge_blacklist (
                    collection TEXT NOT NULL DEFAULT 'default',
                    node_a TEXT NOT NULL,
                    node_b TEXT NOT NULL,
                    reason TEXT,
                    PRIMARY KEY (collection, node_a, node_b)
                )
                """
            )
            conn.execute(
                "INSERT INTO merge_blacklist (collection, node_a, node_b, reason) "
                "SELECT 'default', node_a, node_b, reason FROM merge_blacklist_old"
            )
            conn.execute("DROP TABLE merge_blacklist_old")
        # 날짜별 LLM 요청 수를 누적 기록한다 (RPD 한도 예측·차단용).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_usage (
                usage_date TEXT PRIMARY KEY,
                request_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # 컬렉션(사업) 계층 — 상위 '본부' 아래 사업들을 묶는다. parent가 없는 컬렉션은 행이 없을 수도 있다.
        # 범위 질의에서 부모 이름을 주면 자손까지 펼쳐, 본부 단위/전체 단위를 골라 볼 수 있게 한다.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS collection_meta (
                collection TEXT PRIMARY KEY,
                parent TEXT
            )
            """
        )
        # 엔티티 설명 후보(M1.5) — 같은 엔티티가 여러 문서에서 언급될 때마다 description 후보를 쌓는다.
        # source_doc으로 키잉해, 문서 재처리/삭제 시 그 문서가 남긴 후보만 정확히 지울 수 있다(유령 후보 방지,
        # spec-addendum §C-4). 같은 문서 안에서 같은 엔티티가 여러 청크에 걸쳐 또 나오면 그 문서의 후보 1행만
        # 최신 내용으로 갱신한다(REPLACE) — 문서 간 다중 후보만 통합 요약의 재료가 된다.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entity_desc_candidates (
                collection TEXT NOT NULL,
                entity_name TEXT NOT NULL,
                source_doc TEXT NOT NULL,
                description TEXT NOT NULL,
                PRIMARY KEY (collection, entity_name, source_doc)
            )
            """
        )
        # (collection, file_name)을 문서의 자연 식별자로 강제한다. 같은 파일명을 다른 사업(컬렉션)에서 써도
        # 충돌하지 않게 하고, 같은 컬렉션 안에서 같은 파일 재처리 시 옛 행이 자동 교체(유령 행 방지)되게 한다.
        # 기존 DB에 중복 행이 있으면 가장 최근(rowid 최대) 행만 남기고 정리한 뒤 UNIQUE로 승격한다.
        conn.execute(
            "DELETE FROM documents WHERE rowid NOT IN "
            "(SELECT MAX(rowid) FROM documents GROUP BY collection, file_name)"
        )
        conn.execute("DROP INDEX IF EXISTS idx_documents_file_name")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_file_name "
            "ON documents(collection, file_name)"
        )
        # 커뮤니티 탐지 결과(M2) — 컬렉션별로 격리된 계층(레벨 0..n) 커뮤니티. 코어 그래프(Entity/RELATION)를
        # 건드리지 않는 오버레이라 통째로 지우고 재빌드할 수 있다. entity_names는 조인 회피용 비정규화 JSON 배열.
        # community_id는 (collection, level, 정렬된 멤버 이름 집합)의 결정적 해시라 재탐지해도 멤버셋이 같으면 그대로다.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS communities (
                collection TEXT NOT NULL,
                community_id TEXT NOT NULL,
                level INTEGER NOT NULL,
                parent_community_id TEXT,
                entity_names TEXT NOT NULL,
                size INTEGER NOT NULL,
                graph_signature TEXT NOT NULL,
                PRIMARY KEY (collection, community_id)
            )
            """
        )
        # 컬렉션별 커뮤니티 빌드 상태 — dirty(재빌드 필요 여부)와 마지막 빌드 시점의 graph_signature를 보관한다.
        # 그래프 변이(인제스트/삭제/병합 등)가 일어나면 dirty=1로 표시되고, build가 완주했을 때만 0으로 풀린다
        # (크래시 안전 — 빌드 중간에 죽어도 dirty가 남아 있어야 다음에 다시 시도됨을 알 수 있다).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS community_build_state (
                collection TEXT PRIMARY KEY,
                dirty INTEGER NOT NULL DEFAULT 1,
                graph_signature TEXT
            )
            """
        )
        # 커뮤니티 리포트(M3) — 각 커뮤니티(레벨 0..n)에 LLM이 생성한 title/summary/rating을 저장한다.
        # communities와 동일하게 코어 그래프를 건드리지 않는 텍스트 오버레이라, 컬렉션 삭제 시 함께
        # 캐스케이드되고 재빌드 시 통째로 다시 만들 수 있다(M3는 증분 최적화 없이 매번 전체 재생성, M5 범위).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS community_reports (
                collection TEXT NOT NULL,
                community_id TEXT NOT NULL,
                level INTEGER NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                rating REAL,
                content_signature TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (collection, community_id)
            )
            """
        )
        # [M5] 증분 재계산용 content_signature 컬럼 마이그레이션(기존 DB엔 없을 수 있음).
        report_cols = {row[1] for row in conn.execute("PRAGMA table_info(community_reports)").fetchall()}
        if "content_signature" not in report_cols:
            conn.execute("ALTER TABLE community_reports ADD COLUMN content_signature TEXT")


# 컬렉션+파일명으로 저장된 마지막 content_hash를 조회한다 (없으면 None).
def get_document_hash(collection: str, file_name: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT content_hash FROM documents WHERE collection = ? AND file_name = ?",
            (collection, file_name),
        ).fetchone()
    return row[0] if row else None


# 컬렉션+파일명으로 저장된 source_id를 조회한다 (없으면 None).
def get_document_source_id(collection: str, file_name: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT source_id FROM documents WHERE collection = ? AND file_name = ?",
            (collection, file_name),
        ).fetchone()
    return row[0] if row else None


# 문서 레코드를 새로 쓰거나 갱신한다.
# (collection, file_name)이 UNIQUE라, 같은 파일을 새 source_id로 다시 넣으면 옛 행이 자동 교체된다(유령 행 없음).
def upsert_document(
    source_id: str, collection: str, file_name: str, content: str, content_hash: str
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            REPLACE INTO documents (source_id, collection, file_name, content, content_hash)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source_id, collection, file_name, content, content_hash),
        )


# 컬렉션+파일명으로 문서 레코드를 완전히 삭제한다.
def delete_document(collection: str, file_name: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM documents WHERE collection = ? AND file_name = ?", (collection, file_name)
        )


# 한 컬렉션의 모든 문서 레코드를 삭제한다 (컬렉션 통째 삭제 시 호출).
def delete_collection_documents(collection: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM documents WHERE collection = ?", (collection,))


# 지정한 컬렉션 범위의 문서 개수를 센다 (상태 확인용, collections=None이면 전체).
def count_documents(collections: list[str] | None = None) -> int:
    with get_connection() as conn:
        if collections is None:
            row = conn.execute("SELECT COUNT(*) FROM documents").fetchone()
        else:
            placeholders = ",".join("?" for _ in collections)
            row = conn.execute(
                f"SELECT COUNT(*) FROM documents WHERE collection IN ({placeholders})",
                tuple(collections),
            ).fetchone()
    return row[0] if row else 0


# 현재 SQLite에 기록된 모든 source_id를 가져온다 (고아 데이터 탐지용 — 전역, 이 집합에 없으면 고아).
def get_all_source_ids() -> set[str]:
    with get_connection() as conn:
        rows = conn.execute("SELECT source_id FROM documents").fetchall()
    return {row[0] for row in rows}


# 오늘 보낸 LLM 요청 수를 n만큼 누적 기록한다 (날짜는 태평양시간 기준 — Gemini 한도 리셋과 맞춤).
def record_api_usage(n: int) -> None:
    if n <= 0:
        return
    today = _usage_date()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO api_usage (usage_date, request_count) VALUES (?, ?)
            ON CONFLICT(usage_date) DO UPDATE SET request_count = request_count + ?
            """,
            (today, n, n),
        )


# 오늘 누적된 LLM 요청 수를 조회한다 (없으면 0, 날짜는 태평양시간 기준).
def get_api_usage_today() -> int:
    today = _usage_date()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT request_count FROM api_usage WHERE usage_date = ?", (today,)
        ).fetchone()
    return row[0] if row else 0


# 현재 SQLite에 기록된 모든 컬렉션 이름과 각 문서 수를 가져온다 (컬렉션 목록 조회용).
def get_collection_doc_counts() -> dict[str, int]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT collection, COUNT(*) FROM documents GROUP BY collection"
        ).fetchall()
    return {row[0]: row[1] for row in rows}


# 컬렉션의 부모(본부)를 지정한다. parent가 같은 이름이거나 순환을 만들면 호출 측에서 막아야 한다.
def set_collection_parent(collection: str, parent: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO collection_meta (collection, parent) VALUES (?, ?) "
            "ON CONFLICT(collection) DO UPDATE SET parent = ?",
            (collection, parent, parent),
        )


# 컬렉션의 부모 지정을 해제한다.
def unset_collection_parent(collection: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM collection_meta WHERE collection = ?", (collection,))


# 컬렉션의 직속 부모를 조회한다 (없으면 None).
def get_collection_parent(collection: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT parent FROM collection_meta WHERE collection = ?", (collection,)
        ).fetchone()
    return row[0] if row and row[0] else None


# 어떤 컬렉션의 직속 자식들을 조회한다.
def get_collection_children(parent: str) -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT collection FROM collection_meta WHERE parent = ? ORDER BY collection",
            (parent,),
        ).fetchall()
    return [r[0] for r in rows]


# 컬렉션 자신 + 모든 하위(자식의 자식까지)를 반환한다. 범위 질의에서 부모를 자손까지 펼칠 때 쓴다.
# 순환(A가 B의 부모이면서 B가 A의 부모)이 있어도 방문 집합으로 보호해 무한 루프에 빠지지 않는다.
def get_collection_descendants(collection: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    stack = [collection]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        result.append(current)
        stack.extend(get_collection_children(current))
    return result


# 부모가 지정된 모든 (컬렉션, 부모) 쌍을 가져온다 (계층 트리 표시용).
def list_collection_hierarchy() -> dict[str, str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT collection, parent FROM collection_meta WHERE parent IS NOT NULL AND parent != ''"
        ).fetchall()
    return {r[0]: r[1] for r in rows}


# 두 노드가 해당 컬렉션의 병합 금지 목록에 있는지 확인한다 (순서 무관).
def is_merge_blacklisted(collection: str, node_a: str, node_b: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM merge_blacklist
            WHERE collection = ?
              AND ((node_a = ? AND node_b = ?) OR (node_a = ? AND node_b = ?))
            """,
            (collection, node_a, node_b, node_b, node_a),
        ).fetchone()
    return row is not None


# 두 노드를 해당 컬렉션의 병합 금지 목록에 추가한다.
def add_merge_blacklist(collection: str, node_a: str, node_b: str, reason: str = "") -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO merge_blacklist (collection, node_a, node_b, reason) "
            "VALUES (?, ?, ?, ?)",
            (collection, node_a, node_b, reason),
        )


# 병합 금지 목록을 조회한다 (collection을 주면 그 사업 범위만, None이면 전체).
def list_merge_blacklist(collection: str | None = None) -> list[dict]:
    with get_connection() as conn:
        if collection is None:
            rows = conn.execute(
                "SELECT collection, node_a, node_b, reason FROM merge_blacklist"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT collection, node_a, node_b, reason FROM merge_blacklist WHERE collection = ?",
                (collection,),
            ).fetchall()
    return [{"collection": r[0], "node_a": r[1], "node_b": r[2], "reason": r[3]} for r in rows]


# 해당 컬렉션의 병합 금지 목록에서 두 노드 쌍을 제거한다 (순서 무관).
def remove_merge_blacklist(collection: str, node_a: str, node_b: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM merge_blacklist
            WHERE collection = ?
              AND ((node_a = ? AND node_b = ?) OR (node_a = ? AND node_b = ?))
            """,
            (collection, node_a, node_b, node_b, node_a),
        )


# 엔티티 설명 후보 하나를 (collection, entity_name, source_doc) 키로 기록/갱신한다(M1.5).
# 같은 문서가 같은 엔티티를 다시 언급하면(같은 문서 안 다른 청크) 그 문서 몫 후보 1행만 최신 내용으로 갱신된다.
def upsert_desc_candidate(collection: str, entity_name: str, source_doc: str, description: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            REPLACE INTO entity_desc_candidates (collection, entity_name, source_doc, description)
            VALUES (?, ?, ?, ?)
            """,
            (collection, entity_name, source_doc, description),
        )


# 한 엔티티(해당 컬렉션)에 쌓인 설명 후보 전체를 가져온다. source_doc 사전순으로 반환해 결정적이게 한다.
def get_desc_candidates(collection: str, entity_name: str) -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT description FROM entity_desc_candidates "
            "WHERE collection = ? AND entity_name = ? ORDER BY source_doc",
            (collection, entity_name),
        ).fetchall()
    return [r[0] for r in rows]


# 해당 컬렉션에서 설명 후보가 min_count개 이상 쌓인 엔티티 이름만 가져온다(요약 배치의 대상 선정용).
def get_entities_with_min_candidates(collection: str, min_count: int) -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT entity_name FROM entity_desc_candidates
            WHERE collection = ?
            GROUP BY entity_name
            HAVING COUNT(*) >= ?
            """,
            (collection, min_count),
        ).fetchall()
    return [r[0] for r in rows]


# 특정 문서(source_doc)가 남긴 설명 후보를 전부 삭제한다(문서 재처리/삭제 시 호출 — 유령 후보 방지).
def delete_desc_candidates_by_source_doc(source_doc: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM entity_desc_candidates WHERE source_doc = ?", (source_doc,))


# 한 컬렉션의 설명 후보를 전부 삭제한다(컬렉션 통째 삭제 시 호출 — 다른 데이터와 동일한 캐스케이드 패턴).
def delete_desc_candidates_by_collection(collection: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM entity_desc_candidates WHERE collection = ?", (collection,))


# --- M2: 커뮤니티 탐지 결과(communities) ---


# 한 컬렉션의 커뮤니티 탐지 결과를 통째로 교체한다. 탐지는 매번 전체 재계산(부분 갱신 없음)이라
# 낡은 행을 지우고 새로 넣는 것이 항상 정확하다(멤버가 바뀐 커뮤니티가 옛 id로 남는 유령 행 방지).
def replace_communities(collection: str, communities: list[dict]) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM communities WHERE collection = ?", (collection,))
        conn.executemany(
            """
            INSERT INTO communities
                (collection, community_id, level, parent_community_id, entity_names, size, graph_signature)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    collection,
                    c["community_id"],
                    c["level"],
                    c.get("parent_community_id"),
                    json.dumps(c["entity_names"], ensure_ascii=False),
                    c["size"],
                    c["graph_signature"],
                )
                for c in communities
            ],
        )


# 한 컬렉션의 커뮤니티를 조회한다(level을 주면 그 레벨만, 없으면 전체 레벨). entity_names는 리스트로 역직렬화해 돌려준다.
def get_communities(collection: str, level: int | None = None) -> list[dict]:
    with get_connection() as conn:
        if level is None:
            rows = conn.execute(
                "SELECT collection, community_id, level, parent_community_id, entity_names, size, graph_signature "
                "FROM communities WHERE collection = ? ORDER BY level, community_id",
                (collection,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT collection, community_id, level, parent_community_id, entity_names, size, graph_signature "
                "FROM communities WHERE collection = ? AND level = ? ORDER BY community_id",
                (collection, level),
            ).fetchall()
    return [
        {
            "collection": r[0],
            "community_id": r[1],
            "level": r[2],
            "parent_community_id": r[3],
            "entity_names": json.loads(r[4]),
            "size": r[5],
            "graph_signature": r[6],
        }
        for r in rows
    ]


# 한 컬렉션의 커뮤니티를 전부 삭제한다(컬렉션 통째 삭제 시 호출 — 다른 오버레이 데이터와 동일한 캐스케이드 패턴).
def delete_communities_by_collection(collection: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM communities WHERE collection = ?", (collection,))


# --- M2: 커뮤니티 빌드 상태(dirty 플래그 + graph_signature) ---


# 해당 컬렉션의 그래프가 바뀌었음을 표시한다(다음 communities build 필요).
# 인제스트/문서삭제/수동 병합(blacklist 재병합 포함)/엔티티 삭제 등 그래프를 바꾸는 모든 지점에서 호출한다.
def mark_communities_dirty(collection: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO community_build_state (collection, dirty) VALUES (?, 1) "
            "ON CONFLICT(collection) DO UPDATE SET dirty = 1",
            (collection,),
        )


# 커뮤니티 재빌드가 필요한지 확인한다. 상태 행이 아예 없으면(한 번도 안 빌드됨) dirty로 간주한다.
def is_communities_dirty(collection: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT dirty FROM community_build_state WHERE collection = ?", (collection,)
        ).fetchone()
    return True if row is None else bool(row[0])


# 커뮤니티 빌드가 끝까지 완주했을 때만 호출한다 — dirty를 해제하고 이번 빌드의 graph_signature를 기록한다.
# (크래시 안전: 빌드 중간에 프로세스가 죽으면 이 함수가 호출되지 않아 dirty가 그대로 남는다.)
def clear_communities_dirty(collection: str, graph_signature: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO community_build_state (collection, dirty, graph_signature) VALUES (?, 0, ?) "
            "ON CONFLICT(collection) DO UPDATE SET dirty = 0, graph_signature = ?",
            (collection, graph_signature, graph_signature),
        )


# 컬렉션의 커뮤니티 빌드 상태 행을 삭제한다(컬렉션 통째 삭제 시 호출).
def delete_community_build_state(collection: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM community_build_state WHERE collection = ?", (collection,))


# --- M3: 커뮤니티 리포트(community_reports) ---


# 한 커뮤니티의 리포트를 새로 쓰거나 갱신한다(REPLACE — updated_at은 호출 시점으로 항상 갱신).
# content_signature는 [M5] 증분 재계산용 — 다음 빌드 때 이 값이 같으면 재요약을 건너뛴다.
def upsert_community_report(
    collection: str, community_id: str, level: int, title: str, summary: str,
    rating: float | None, content_signature: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            REPLACE INTO community_reports
                (collection, community_id, level, title, summary, rating, content_signature, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (collection, community_id, level, title, summary, rating, content_signature),
        )


# 한 커뮤니티의 리포트를 조회한다(없으면 None).
def get_community_report(collection: str, community_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT collection, community_id, level, title, summary, rating, content_signature, updated_at "
            "FROM community_reports WHERE collection = ? AND community_id = ?",
            (collection, community_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "collection": row[0],
        "community_id": row[1],
        "level": row[2],
        "title": row[3],
        "summary": row[4],
        "rating": row[5],
        "content_signature": row[6],
        "updated_at": row[7],
    }


# 한 컬렉션의 커뮤니티 리포트를 조회한다(level을 주면 그 레벨만, 없으면 전체 레벨).
def get_community_reports(collection: str, level: int | None = None) -> list[dict]:
    with get_connection() as conn:
        if level is None:
            rows = conn.execute(
                "SELECT collection, community_id, level, title, summary, rating, content_signature, updated_at "
                "FROM community_reports WHERE collection = ? ORDER BY level, community_id",
                (collection,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT collection, community_id, level, title, summary, rating, content_signature, updated_at "
                "FROM community_reports WHERE collection = ? AND level = ? ORDER BY community_id",
                (collection, level),
            ).fetchall()
    return [
        {
            "collection": r[0],
            "community_id": r[1],
            "level": r[2],
            "title": r[3],
            "summary": r[4],
            "rating": r[5],
            "content_signature": r[6],
            "updated_at": r[7],
        }
        for r in rows
    ]


# 한 컬렉션의 커뮤니티 리포트를 전부 삭제한다(컬렉션 통째 삭제 시 호출 — 다른 오버레이 데이터와 동일한 캐스케이드 패턴).
def delete_community_reports_by_collection(collection: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM community_reports WHERE collection = ?", (collection,))
