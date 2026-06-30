# 문서 단위 저장 오케스트레이션 — sqlite/vector/graph 호출 순서만 정하는 얇은 계층.
import hashlib
import logging
import re
import time

from config import settings
from db import graph_manager, sqlite_manager, vector_manager

logger = logging.getLogger(__name__)


# 문서 내용으로부터 변경 감지용 해시값을 계산한다.
def compute_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# PDF/HWP에서 변환된 마크다운의 표 노이즈(빈 셀 그리드 `|   |   |`, 구분선 `|---|`)를 걷어낸다.
# 빈 칸 격자가 청크 예산을 절반 가까이 잡아먹어 추출 집중도를 떨어뜨리기 때문에, 청킹 직전에만 적용한다.
# (해시·원본 저장은 raw 그대로 두어 변경 감지 정확성을 유지하고, 청킹/임베딩/추출 입력만 정리본을 쓴다.)
# 표 행은 비어있지 않은 셀만 공백으로 이어 붙여 실제 정보(기관명·번호 등)는 보존한다.
def clean_markdown(content: str) -> str:
    cleaned_lines: list[str] = []
    for line in content.split("\n"):
        # 표 구분선/빈 표줄(파이프와 공백·콜론·하이픈만으로 된 줄)은 통째로 버린다.
        if "|" in line and re.fullmatch(r"[\s|:\-]*", line):
            continue
        # 표 행(파이프 3개 이상)은 빈 셀을 빼고 내용 있는 셀만 공백으로 잇는다.
        if line.count("|") >= 3:
            cells = [cell.strip() for cell in line.split("|")]
            cells = [cell for cell in cells if cell]
            if not cells:
                continue
            line = " ".join(cells)
        cleaned_lines.append(line)
    # 표를 걷어내며 생긴 과도한 빈 줄(3줄 이상)을 두 줄로 압축한다.
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned_lines))


# 문장/문단 경계를 우선해 자르기 위한 구분자 우선순위.
# 위에서부터 차례로 시도하고, 어떤 것도 안 통하면 최후에 글자 단위(None)로 쪼갠다.
# 문장 구분자는 문장부호 뒤 공백을 노려, 문장부호 자체는 앞 문장에 남겨 보존한다.
_CHUNK_SEPARATORS = [
    r"\n\s*\n",  # 문단 경계(빈 줄)
    r"\n",  # 줄 경계
    r"[.!?。?！][\s\"'”’)\]]*\s+",  # 문장 경계(문장부호 + 따라오는 닫는 따옴표/공백)
    r"\s+",  # 공백 경계
    None,  # 최후: 글자 단위
]


# 구분자를 '앞 조각'에 그대로 붙여 자른다(어떤 글자도 잃지 않게).
def _split_keep(text: str, pattern: str) -> list[str]:
    pieces: list[str] = []
    last = 0
    for match in re.finditer(pattern, text):
        pieces.append(text[last : match.end()])
        last = match.end()
    if last < len(text):
        pieces.append(text[last:])
    return pieces


# 텍스트를 chunk_size 이하의 의미 단위(원자)들로 쪼갠다.
# 구분자 우선순위를 따라 내려가며, 한 조각이 여전히 너무 길면 더 잘게(다음 구분자로) 재귀 분할한다.
def _atomize(text: str, chunk_size: int, separators: list) -> list[str]:
    if len(text) <= chunk_size:
        return [text] if text else []
    if not separators or separators[0] is None:
        return list(text)  # 더 쪼갤 경계가 없으면 글자 단위로(겹침 계산이 정확해진다)
    pieces = _split_keep(text, separators[0])
    if len(pieces) <= 1:
        return _atomize(text, chunk_size, separators[1:])
    atoms: list[str] = []
    for piece in pieces:
        atoms.extend(_atomize(piece, chunk_size, separators[1:]))
    return atoms


# 의미 단위(원자)들을 chunk_size를 넘지 않게 이어붙이고, 경계마다 overlap만큼 겹쳐 다음 청크로 넘긴다.
def _merge_atoms(atoms: list[str], chunk_size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    total = 0
    for atom in atoms:
        if total + len(atom) > chunk_size and current:
            chunks.append("".join(current))
            while total > overlap and current:  # 앞쪽을 덜어 겹침 분량만 남긴다
                total -= len(current[0])
                current.pop(0)
        current.append(atom)
        total += len(atom)
    if current:
        chunks.append("".join(current))
    return chunks


# 문서를 분할한다. 문단/문장/공백 경계를 우선해 잘라 문맥(관계)이 문장 한가운데서 끊기지 않게 하고,
# overlap만큼 겹쳐 경계에 걸친 관계도 보존한다. 자를 경계가 전혀 없으면 글자 단위로 폴백한다.
def chunk_text(content: str, chunk_size: int, overlap: int = 0) -> list[str]:
    if not content:
        return []
    atoms = _atomize(content, chunk_size, _CHUNK_SEPARATORS)
    return _merge_atoms(atoms, chunk_size, overlap)


# 이 문서를 처리할 때 나갈 LLM 요청 수(= 청크 수)를 미리 계산한다 (RPD 한도 예측용).
# 실제 처리와 동일하게 표 노이즈를 걷어낸 뒤 청킹해야 예상치와 실제 호출 수가 일치한다.
def estimate_request_count(content: str) -> int:
    return len(chunk_text(clean_markdown(content), settings.chunk_size, settings.chunk_overlap))


# 저장된 해시와 비교해 재처리가 필요한지 판단한다 (해당 컬렉션 범위에서).
def needs_processing(collection: str, file_name: str, content_hash: str) -> bool:
    existing_hash = sqlite_manager.get_document_hash(collection, file_name)
    return existing_hash != content_hash


# 새 source_id만 발급한다. 이 단계에서는 SQLite를 건드리지 않는다 — 처리가 끝까지
# 성공해야 commit_document가 호출되므로, 중간에 실패하면 다음 시도에서 다시 처리 대상이 된다.
def prepare_replacement(file_name: str) -> str:
    return f"doc_{int(time.time() * 1000)}"


# 모든 청킹/임베딩/추출이 성공적으로 끝난 뒤에만 호출한다.
# 새 문서를 SQLite에 기록하고, 그제서야 옛 문서의 벡터/그래프 데이터를 정리한다.
def commit_document(
    source_id: str, collection: str, file_name: str, content: str, content_hash: str
) -> None:
    old_source_id = sqlite_manager.get_document_source_id(collection, file_name)
    sqlite_manager.upsert_document(source_id, collection, file_name, content, content_hash)

    if old_source_id and old_source_id != source_id:
        logger.info("기존 문서 데이터 삭제: [%s] %s (source_id=%s)", collection, file_name, old_source_id)
        vector_manager.delete_chunks_by_source(old_source_id)
        graph_manager.delete_relations_by_source_doc(old_source_id)


# 문서를 완전히 삭제한다(SQLite 기록 + 벡터 청크 + 관계). 엔티티 노드 자체는 남는다
# (다른 문서가 같은 엔티티를 참조하고 있을 수 있어서, 엔티티 삭제는 graph_manager.delete_entity로 별도 처리).
# 문서가 존재하지 않으면 False를 반환한다.
def delete_document(collection: str, file_name: str) -> bool:
    source_id = sqlite_manager.get_document_source_id(collection, file_name)
    if not source_id:
        return False

    vector_manager.delete_chunks_by_source(source_id)
    graph_manager.delete_relations_by_source_doc(source_id)
    sqlite_manager.delete_document(collection, file_name)
    logger.info("문서 완전 삭제: [%s] %s (source_id=%s)", collection, file_name, source_id)
    return True


# 한 컬렉션(사업)을 통째로 삭제한다(문서 기록 + 벡터 청크 + 그래프 엔티티/관계 전부).
# 잘못된 컬렉션으로 추출한 것을 깔끔히 되돌릴 때 쓴다. 삭제한 문서 수를 반환한다.
def delete_collection(collection: str) -> int:
    doc_count = sqlite_manager.count_documents([collection])
    vector_manager.delete_chunks_by_collection(collection)
    graph_manager.delete_collection(collection)
    sqlite_manager.delete_collection_documents(collection)
    logger.info("컬렉션 통째 삭제: %s (문서 %d개)", collection, doc_count)
    return doc_count


# SQLite에 더 이상 기록되지 않은 source_id(= 처리 중간에 실패해서 추적이 끊긴 데이터)를 찾는다.
def find_orphaned_source_ids() -> set[str]:
    valid_ids = sqlite_manager.get_all_source_ids()
    referenced_ids = vector_manager.get_all_source_ids() | graph_manager.get_all_source_docs()
    return referenced_ids - valid_ids


# 고아 source_id의 벡터 청크/관계를 모두 정리하고, 정리한 source_id 개수를 반환한다.
def cleanup_orphaned_data() -> int:
    orphaned = find_orphaned_source_ids()
    for source_id in orphaned:
        vector_manager.delete_chunks_by_source(source_id)
        graph_manager.delete_relations_by_source_doc(source_id)
        logger.info("고아 데이터 정리: source_id=%s", source_id)
    return len(orphaned)
