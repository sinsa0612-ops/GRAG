# 문서 처리 파이프라인 — 읽기 → 변경감지 → 청킹 → LLM 추출 → 검증 → 저장.
import json
import logging
from pathlib import Path

from pydantic import ValidationError

from adapters.llm_adapter import generate
from config import settings
from db import document_store, graph_manager, vector_manager
from schemas import EntityType, ExtractionResult

logger = logging.getLogger(__name__)

# 엔티티 type을 고정 온톨로지로 가두기 위해 허용 목록을 프롬프트에 직접 명시한다(schemas.EntityType와 단일 출처).
_ALLOWED_TYPES = ", ".join(t.value for t in EntityType)

_EXTRACTION_PROMPT = """\
다음 텍스트에서 중요한 엔티티(명사)와 관계를 추출해.
동명이인 구분을 위해 description을 꼭 적어줘.
엔티티 type은 반드시 아래 목록 중 하나만 대문자 그대로 골라. 애매하면 OTHER로 둬:
{allowed_types}
관계 predicate는 항상 영어 대문자 스네이크케이스로만 적어(한글 금지. 예: WORKS_AT, LOCATED_IN).
valid_from처럼 날짜/시점 정보는 텍스트에 명시적으로 적혀 있을 때만 채우고,
적혀 있지 않으면 절대 추측하지 말고 빈 문자열("")로 남겨줘.

텍스트:
{chunk}
"""

_VOCAB_HINT = """\
이미 그래프에 쓰이고 있는 표현이야. 같은 대상/의미면 새로 만들지 말고 아래 표현을 정확히 그대로 재사용해.
정말 기존 표현으로 담을 수 없는 새로운 대상/개념일 때만 새로 만들어도 돼.
- 기존 엔티티 이름: {names}
- 기존 관계 이름: {predicates}

"""


# 이미 그래프에 쓰인 이름/관계 어휘를 프롬프트 앞에 붙인다 (어휘 파편화 방지).
# 이름은 전체를 넣지 않고 '이번 청크에 실제로 등장하는' 것만 max_name_hints개까지 추려, 입력 토큰이 무한정 늘지 않게 한다.
# (type은 고정 온톨로지라 동적으로 주입하지 않고 _EXTRACTION_PROMPT에 목록으로 박아둔다.)
def _build_prompt(
    chunk: str,
    known_names: list[str],
    known_predicates: list[str],
) -> str:
    relevant_names = [name for name in known_names if name and name in chunk][
        : settings.max_name_hints
    ]
    vocab_hint = ""
    if relevant_names or known_predicates:
        vocab_hint = _VOCAB_HINT.format(
            names=", ".join(relevant_names) or "(아직 없음)",
            predicates=", ".join(known_predicates) or "(아직 없음)",
        )
    return vocab_hint + _EXTRACTION_PROMPT.format(allowed_types=_ALLOWED_TYPES, chunk=chunk)


# LLM 원시 응답 문자열을 파싱하고 Pydantic으로 검증한다 (입구 검증).
def _parse_extraction(raw_text: str) -> ExtractionResult:
    cleaned = raw_text.replace("```json", "").replace("```", "").strip()
    data = json.loads(cleaned)
    return ExtractionResult.model_validate(data)


# 청크 하나를 LLM에 보내 엔티티/관계를 추출한다.
# JSON 모드로 호출해 스키마에 맞는 순수 JSON을 받는다(파싱 안정성↑).
# 호출 실패(네트워크/API 오류)나 검증 실패 모두 이 청크만 건너뛰고, 다른 청크 처리를 막지 않는다.
def extract_chunk(
    chunk: str,
    known_names: list[str] | None = None,
    known_predicates: list[str] | None = None,
) -> ExtractionResult | None:
    try:
        raw_text = generate(
            _build_prompt(chunk, known_names or [], known_predicates or []),
            response_schema=ExtractionResult,
        )
    except Exception as exc:
        logger.error("LLM 호출 실패, 이 청크는 건너뜀: %s", exc)
        return None

    try:
        return _parse_extraction(raw_text)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("LLM 추출 결과 검증 실패, 이 청크는 건너뜀: %s", exc)
        return None


# name이 그 컬렉션의 기존 엔티티 정식 이름/alias와 정확히 일치하면 그 정식 이름으로 치환한다.
# 일치하는 게 alias 매칭이었다면(이름 자체와는 다름) 그 사실을 alias로 기록해둔다.
def _resolve_canonical_name(collection: str, name: str) -> str:
    canonical = graph_manager.find_canonical_name(collection, name)
    if canonical is None:
        return name
    if canonical != name:
        graph_manager.add_alias(collection, canonical, name)
    return canonical


# 검증된 추출 결과를 해당 컬렉션의 그래프 DB에 반영한다. 엔티티/관계 이름은 저장 전 캐노니컬 이름으로 정규화한다.
def store_extraction(collection: str, result: ExtractionResult, source_doc: str) -> None:
    for entity in result.entities:
        resolved_name = _resolve_canonical_name(collection, entity.name)
        # entity.type은 EntityType(Enum)이라 DB(STRING)에는 순수 값 문자열("PERSON")로 풀어 저장한다.
        graph_manager.upsert_entity(collection, resolved_name, entity.type.value, entity.description)
    for relation in result.relations:
        graph_manager.upsert_relation(
            collection,
            _resolve_canonical_name(collection, relation.source),
            _resolve_canonical_name(collection, relation.target),
            relation.predicate,
            relation.valid_from,
            source_doc,
        )


# 파일 하나를 지정한 컬렉션(사업)으로 끝까지 처리한다 (변경 없으면 False, 처리했으면 True를 반환).
def process_file(file_path: Path, collection: str) -> bool:
    content = file_path.read_text(encoding="utf-8")
    file_name = file_path.name
    content_hash = document_store.compute_hash(content)

    if not document_store.needs_processing(collection, file_name, content_hash):
        logger.info("[%s] %s 은(는) 변경사항이 없어 건너뜁니다.", collection, file_name)
        return False

    source_id = document_store.prepare_replacement(file_name)
    chunks = document_store.chunk_text(content, settings.chunk_size, settings.chunk_overlap)
    vector_manager.add_chunks(source_id, chunks, collection)

    for chunk in chunks:
        # 청크마다 매번 새로 가져온다 — 같은 문서를 처리하는 중에도 앞 청크가 막 만든
        # 엔티티/관계 이름을 뒤 청크가 못 보면, 한 문서 안에서도 표현이 갈라진다(91청크 실제 검증으로 확인됨).
        # 어휘 힌트는 '이 컬렉션' 범위로만 모은다 — 다른 사업의 이름이 섞여 들어가지 않게.
        known_names = graph_manager.get_known_entity_names([collection])
        known_predicates = graph_manager.get_known_predicates([collection])

        result = extract_chunk(chunk, known_names, known_predicates)
        if result is None:
            continue
        try:
            store_extraction(collection, result, source_id)
        except Exception as exc:
            logger.error("그래프 저장 실패, 이 청크 결과는 건너뜀: %s", exc)

    document_store.commit_document(source_id, collection, file_name, content, content_hash)
    logger.info("[%s] %s 처리 완료 (source_id=%s, 청크 %d개)", collection, file_name, source_id, len(chunks))
    return True
