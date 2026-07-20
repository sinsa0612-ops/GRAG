# SQLite(Master DB) 전담 — 문서 원본/해시, 병합 블랙리스트, 일일 API 사용량을 책임진다.
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
