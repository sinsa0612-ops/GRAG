# SQLite(Master DB) 전담 — 문서 원본/해시 및 병합 블랙리스트만 책임진다.
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from config import settings


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS merge_blacklist (
                node_a TEXT NOT NULL,
                node_b TEXT NOT NULL,
                reason TEXT,
                PRIMARY KEY (node_a, node_b)
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


# 현재 SQLite에 기록된 모든 컬렉션 이름과 각 문서 수를 가져온다 (컬렉션 목록 조회용).
def get_collection_doc_counts() -> dict[str, int]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT collection, COUNT(*) FROM documents GROUP BY collection"
        ).fetchall()
    return {row[0]: row[1] for row in rows}


# 두 노드가 병합 금지 목록에 있는지 확인한다 (순서 무관).
def is_merge_blacklisted(node_a: str, node_b: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM merge_blacklist
            WHERE (node_a = ? AND node_b = ?) OR (node_a = ? AND node_b = ?)
            """,
            (node_a, node_b, node_b, node_a),
        ).fetchone()
    return row is not None


# 두 노드를 병합 금지 목록에 추가한다.
def add_merge_blacklist(node_a: str, node_b: str, reason: str = "") -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO merge_blacklist (node_a, node_b, reason) VALUES (?, ?, ?)",
            (node_a, node_b, reason),
        )


# 병합 금지 목록 전체를 조회한다.
def list_merge_blacklist() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT node_a, node_b, reason FROM merge_blacklist").fetchall()
    return [{"node_a": r[0], "node_b": r[1], "reason": r[2]} for r in rows]


# 병합 금지 목록에서 두 노드 쌍을 제거한다 (순서 무관).
def remove_merge_blacklist(node_a: str, node_b: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            DELETE FROM merge_blacklist
            WHERE (node_a = ? AND node_b = ?) OR (node_a = ? AND node_b = ?)
            """,
            (node_a, node_b, node_b, node_a),
        )
