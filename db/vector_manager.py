# ChromaDB(Vector DB) 전담 — 텍스트 청크 임베딩 저장/검색만 책임진다.
import chromadb

from adapters.embedding_adapter import embed_texts
from config import settings

_client: chromadb.ClientAPI | None = None


# Chroma persistent client를 1회만 생성해 컬렉션을 반환한다.
def _get_collection():
    global _client
    if _client is None:
        settings.db_dir.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(settings.chroma_path))
    return _client.get_or_create_collection(
        name="knowledge_chunks",
        metadata={"hnsw:space": "cosine"},
    )


# knowledge_chunks 컬렉션을 최초 1회 생성한다.
def init_schema() -> None:
    _get_collection()


# 문서 청크들을 임베딩하여 컬렉션에 저장한다. 각 청크에 소속 컬렉션(사업) 태그를 함께 단다.
def add_chunks(source_id: str, chunks: list[str], collection_name: str) -> None:
    if not chunks:
        return
    collection = _get_collection()
    vectors = embed_texts(chunks)
    ids = [f"{source_id}_chunk_{i}" for i in range(len(chunks))]
    metadatas = [{"source_id": source_id, "collection": collection_name} for _ in chunks]
    collection.add(ids=ids, embeddings=vectors, documents=chunks, metadatas=metadatas)


# 특정 문서의 청크를 모두 삭제한다 (증분 업데이트 시 재처리 전 호출).
def delete_chunks_by_source(source_id: str) -> None:
    collection = _get_collection()
    collection.delete(where={"source_id": source_id})


# 특정 컬렉션(사업)의 청크를 모두 삭제한다 (컬렉션 통째 삭제 시 호출).
def delete_chunks_by_collection(collection_name: str) -> None:
    collection = _get_collection()
    collection.delete(where={"collection": collection_name})


# 입력 텍스트와 의미적으로 가장 가까운 청크들을 검색한다.
# collections를 주면 그 컬렉션(사업) 범위 안에서만 찾고, None이면 전체에서 찾는다(행정 종합).
def query_similar(text: str, top_k: int = 5, collections: list[str] | None = None) -> list[str]:
    collection = _get_collection()
    vector = embed_texts([text])[0]
    where = {"collection": {"$in": collections}} if collections else None
    result = collection.query(query_embeddings=[vector], n_results=top_k, where=where)
    documents = result.get("documents") or [[]]
    return documents[0]


# 전체 청크 개수를 센다 (상태 확인용).
def count_chunks() -> int:
    return _get_collection().count()


# 현재 컬렉션에 들어있는 모든 source_id를 가져온다 (고아 데이터 탐지용).
def get_all_source_ids() -> set[str]:
    collection = _get_collection()
    result = collection.get(include=["metadatas"])
    metadatas = result.get("metadatas") or []
    return {m["source_id"] for m in metadatas if m and "source_id" in m}
