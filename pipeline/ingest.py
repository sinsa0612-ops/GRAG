# 문서 처리 파이프라인 — 읽기 → 변경감지 → 청킹 → LLM 추출 → 검증 → 저장.
import json
import logging
from pathlib import Path

from pydantic import ValidationError

from adapters.llm_adapter import generate
from config import settings
from db import document_store, graph_manager, sqlite_manager, vector_manager
from schemas import EntityType, ExtractionResult

logger = logging.getLogger(__name__)

# 엔티티 type을 고정 온톨로지로 가두기 위해 허용 목록을 프롬프트에 직접 명시한다(schemas.EntityType와 단일 출처).
_ALLOWED_TYPES = ", ".join(t.value for t in EntityType)

# 자주 쓰는 관계 이름 시드 목록. type처럼 강제하지는 않고(열린 어휘), '가능하면 이것부터 재사용'하도록 권장만 한다.
# 초기 문서들이 같은 관계를 제각각(WORKS_AT/EMPLOYED_BY/...)으로 만들어 파편화되는 것을 줄인다.
_SEED_PREDICATES = ", ".join(
    [
        "WORKS_AT", "MEMBER_OF", "PART_OF", "LOCATED_IN", "OWNS", "MANAGES",
        "CREATED", "PRODUCES", "USES", "PARTICIPATED_IN", "OCCURRED_ON",
        "HAS_ROLE", "AFFILIATED_WITH", "CAUSES", "RELATED_TO",
    ]
)

_EXTRACTION_PROMPT = """\
다음 텍스트에서 중요한 엔티티(명사)와 관계를 추출해.
동명이인 구분을 위해 description을 꼭 적어줘.
엔티티 type은 반드시 아래 목록 중 하나만 대문자 그대로 골라. 애매하면 OTHER로 둬:
{allowed_types}
관계 predicate는 항상 영어 대문자 스네이크케이스로만 적어(한글 금지. 예: WORKS_AT, LOCATED_IN).
가능하면 아래 자주 쓰는 관계를 먼저 재사용하고, 정말 맞는 게 없을 때만 같은 형식으로 새로 만들어:
{seed_predicates}
valid_from처럼 날짜/시점 정보는 텍스트에 명시적으로 적혀 있을 때만 채우고,
적혀 있지 않으면 절대 추측하지 말고 빈 문자열("")로 남겨줘.

추출 품질 규칙(꼭 지켜):
- 순수 식별자·코드·수치만으로 된 것은 엔티티로 만들지 마. 예: 특허번호(10-2125457), 과제·공고번호(RS-2026-..., 제2026-71호), 사업자/법인등록번호, 전화번호, 주소 전체. 이런 건 관련 엔티티의 description에 녹여 적어.
- 엔티티 이름(name)은 가장 짧은 표준 명칭으로 적어. 과제명·논문제목·문장 전체를 통째로 이름으로 쓰지 마(예: "20kW급 잠수함용 연료전지 시스템" 같은 핵심어로 줄여).
- 같은 대상은 표기를 통일해. 띄어쓰기·괄호만 다르게 새로 만들지 마(예: "연료전지 시스템"과 "연료전지시스템"을 따로 만들지 말 것).
- RELATED_TO는 마땅한 구체 관계가 없을 때만 최후의 수단으로 써. 가능하면 구체적인 predicate를 골라.
- 단지 같은 목록/표에 함께 등장한다는 이유만으로 장비·도구를 주제(예: 연료전지)에 CREATED/PRODUCES로 잇지 마. 텍스트가 실제로 그 행위를 말할 때만 관계를 만들어.

예시)
입력: "홍길동은 2020년부터 OO전자에 다녔다."
출력: {{"entities": [{{"name": "홍길동", "type": "PERSON", "description": "OO전자 직원"}}, \
{{"name": "OO전자", "type": "ORGANIZATION", "description": "홍길동이 다닌 회사"}}], \
"relations": [{{"source": "홍길동", "target": "OO전자", "predicate": "WORKS_AT", "valid_from": "2020"}}]}}

출력은 위 예시처럼 순수 JSON 객체 하나만 적어. 코드펜스(```)·설명·앞뒤 문장을 붙이지 마.

텍스트:
{chunk}
"""

_VOCAB_HINT = """\
이미 그래프에 쓰이고 있는 표현이야. 같은 대상/의미면 새로 만들지 말고 아래 표현을 정확히 그대로 재사용해.
정말 기존 표현으로 담을 수 없는 새로운 대상/개념일 때만 새로 만들어도 돼.
- 기존 엔티티 이름: {names}
- 기존 관계 이름: {predicates}

"""

# gleaning 라운드용 프롬프트 — 1차 추출에서 놓친 엔티티/관계만 추가로 캐낸다.
# 필드명(name/source/target …)을 예시로 못 박는다 — 구조화 출력 미지원 모델(Gemma)이 스키마 강제 없이
# id/text/subject 같은 딴 키로 응답해 검증에서 통째로 버려지는 것을 막기 위함.
_GLEAN_PROMPT = """\
아래 텍스트에서 [이미 찾은 엔티티]에 없는, 놓친 중요한 엔티티와 관계만 추가로 추출해.
이미 목록에 있는 것은 다시 넣지 마. 놓친 게 없으면 entities/relations를 빈 배열([])로 둬.
엔티티 type은 아래 목록 중 하나만 대문자 그대로. 애매하면 OTHER:
{allowed_types}
predicate는 영어 대문자 스네이크케이스. 가능하면 다음을 먼저 재사용: {seed_predicates}
반드시 아래 필드명을 그대로 써 — 엔티티는 name/type/description, 관계는 source/target/predicate/valid_from.
출력은 순수 JSON 객체 하나만(코드펜스·설명·앞뒤 문장 금지). 예:
{{"entities": [{{"name": "홍길동", "type": "PERSON", "description": "설명"}}], \
"relations": [{{"source": "홍길동", "target": "OO전자", "predicate": "WORKS_AT", "valid_from": ""}}]}}

[이미 찾은 엔티티]
{found_names}

텍스트:
{chunk}
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
    return vocab_hint + _EXTRACTION_PROMPT.format(
        allowed_types=_ALLOWED_TYPES, seed_predicates=_SEED_PREDICATES, chunk=chunk
    )


# 구조화 출력 미지원 모델(Gemma 등)이 표준 키 대신 흔히 쓰는 변형 키를 표준 키로 되돌린다.
# 표준 키가 없을 때만 변형 키 값을 채워 넣으므로 스키마(response_schema)는 건드리지 않고 파싱만 견고해진다.
# (예: 엔티티 id/text/entity/label -> name, 관계 subject/from -> source, object/to -> target)
_ENTITY_NAME_ALIASES = ("id", "text", "entity", "label")
_REL_SOURCE_ALIASES = ("subject", "from")
_REL_TARGET_ALIASES = ("object", "to")


# entities/relations 딕셔너리들의 키를 표준 키로 정규화한다(입구 검증 직전 방어).
def _normalize_field_names(data: dict) -> dict:
    for entity in data.get("entities") or []:
        if isinstance(entity, dict) and "name" not in entity:
            for alias in _ENTITY_NAME_ALIASES:
                if alias in entity:
                    entity["name"] = entity[alias]
                    break
    for relation in data.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        if "source" not in relation:
            for alias in _REL_SOURCE_ALIASES:
                if alias in relation:
                    relation["source"] = relation[alias]
                    break
        if "target" not in relation:
            for alias in _REL_TARGET_ALIASES:
                if alias in relation:
                    relation["target"] = relation[alias]
                    break
    return data


# LLM 원시 응답 문자열을 파싱하고 Pydantic으로 검증한다 (입구 검증).
def _parse_extraction(raw_text: str) -> ExtractionResult:
    cleaned = raw_text.replace("```json", "").replace("```", "").strip()
    data = json.loads(cleaned)
    if isinstance(data, dict):
        data = _normalize_field_names(data)
    return ExtractionResult.model_validate(data)


# 청크 하나를 LLM에 보내 엔티티/관계를 추출한다.
# JSON 모드로 호출해 스키마에 맞는 순수 JSON을 받는다(파싱 안정성↑). 구조화 출력 미지원 모델(Gemma)은
# 어댑터가 스키마를 빼고 프롬프트 기반 JSON으로 받는다 — 그 경우에도 아래 파서가 동일하게 처리한다.
# model로 호출 모델을 바꿀 수 있다(없으면 설정 기본 모델).
# 호출 실패(네트워크/API 오류)나 검증 실패 모두 이 청크만 건너뛰고, 다른 청크 처리를 막지 않는다.
def extract_chunk(
    chunk: str,
    known_names: list[str] | None = None,
    known_predicates: list[str] | None = None,
    model: str | None = None,
    backend: str | None = None,
) -> ExtractionResult | None:
    try:
        raw_text = generate(
            _build_prompt(chunk, known_names or [], known_predicates or []),
            response_schema=ExtractionResult,
            model=model,
            backend=backend,
        )
    except Exception as exc:
        logger.error("LLM 호출 실패, 이 청크는 건너뜀: %s", exc)
        return None

    try:
        return _parse_extraction(raw_text)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("LLM 추출 결과 검증 실패, 이 청크는 건너뜀: %s", exc)
        return None


# 한 청크의 1차 추출 결과(base)에 이어, 놓친 엔티티/관계를 rounds회 더 캐내 누적한다(MS GraphRAG의 gleaning).
# 라운드마다 '이미 찾은 엔티티'를 알려주고 놓친 것만 요청하며, 새로 나온 게 없으면 조기 종료해 호출을 아낀다.
# 반환: (누적 결과, 실제로 추가 소비한 LLM 호출 수).
def glean_chunk(
    chunk: str,
    base: ExtractionResult,
    rounds: int,
    model: str | None = None,
    backend: str | None = None,
) -> tuple[ExtractionResult, int]:
    entities = {e.name: e for e in base.entities}  # 이름 기준 dedupe
    relations = {(r.source, r.predicate, r.target): r for r in base.relations}
    extra_calls = 0
    for _ in range(max(0, rounds)):
        found_names = ", ".join(entities.keys()) or "(없음)"
        prompt = _GLEAN_PROMPT.format(
            allowed_types=_ALLOWED_TYPES,
            seed_predicates=_SEED_PREDICATES,
            found_names=found_names,
            chunk=chunk,
        )
        extra_calls += 1
        try:
            extra = _parse_extraction(
                generate(prompt, response_schema=ExtractionResult, model=model, backend=backend)
            )
        except Exception as exc:
            logger.warning("gleaning 라운드 실패, 이 라운드는 건너뜀: %s", exc)
            break
        added = 0
        for entity in extra.entities:
            if entity.name not in entities:
                entities[entity.name] = entity
                added += 1
        for relation in extra.relations:
            key = (relation.source, relation.predicate, relation.target)
            if key not in relations:
                relations[key] = relation
                added += 1
        if added == 0:
            break  # 더 나올 게 없으면 조기 종료(불필요한 호출 방지)
    return (
        ExtractionResult(entities=list(entities.values()), relations=list(relations.values())),
        extra_calls,
    )


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
        # [M1.5] hot-path의 description은 위에서 이미 그대로 채웠다(로컬 질의가 그걸 쓰므로 절대 비우지 않음).
        # 이 후보 적재는 그와 '병행 추가'일 뿐 — source_doc 키로 쌓아뒀다가 옵트인 배치
        # (summarize-descriptions)가 나중에 여러 문서의 설명을 하나로 통합할 때 재료로 쓴다.
        if entity.description:
            sqlite_manager.upsert_desc_candidate(collection, resolved_name, source_doc, entity.description)
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
# model로 추출 LLM 모델을, glean_rounds로 청크당 gleaning 라운드 수를 바꿀 수 있다(둘 다 없으면 설정 기본값).
def process_file(
    file_path: Path,
    collection: str,
    model: str | None = None,
    glean_rounds: int | None = None,
    backend: str | None = None,
) -> bool:
    # backend=None/"gemini"이면 기존 Gemini 경로(RPD 한도 기록 포함). "ollama"/"claude_cli"는 로컬/구독이라
    # Gemini 일일 한도(RPD)에 안 잡히므로 record_api_usage를 건너뛴다(아래 _is_gemini 참조).
    _is_gemini = backend in (None, "gemini")
    content = file_path.read_text(encoding="utf-8")
    file_name = file_path.name
    content_hash = document_store.compute_hash(content)

    if not document_store.needs_processing(collection, file_name, content_hash):
        logger.info("[%s] %s 은(는) 변경사항이 없어 건너뜁니다.", collection, file_name)
        return False

    source_id = document_store.prepare_replacement(file_name)
    # 변경 감지/원본 보존은 raw(content)로 끝냈으니, 청킹·임베딩·추출 입력만 표 노이즈를 걷어낸 정리본으로 쓴다.
    cleaned = document_store.clean_markdown(content)
    chunks = document_store.chunk_text(cleaned, settings.chunk_size, settings.chunk_overlap)
    # gleaning 라운드 수 결정(인자 우선, 없으면 설정값).
    rounds = settings.glean_rounds if glean_rounds is None else glean_rounds
    # 청크마다 1차 LLM 호출 1번이 나가므로, 청크 수만큼 오늘 사용량에 기록한다(RPD 추적).
    # gleaning 추가 호출은 실제 소비한 만큼 아래에서 따로 더한다.
    if _is_gemini:
        sqlite_manager.record_api_usage(len(chunks))
    vector_manager.add_chunks(source_id, chunks, collection)

    extra_calls = 0
    for chunk in chunks:
        # 청크마다 매번 새로 가져온다 — 같은 문서를 처리하는 중에도 앞 청크가 막 만든
        # 엔티티/관계 이름을 뒤 청크가 못 보면, 한 문서 안에서도 표현이 갈라진다(91청크 실제 검증으로 확인됨).
        # 어휘 힌트는 '이 컬렉션' 범위로만 모은다 — 다른 사업의 이름이 섞여 들어가지 않게.
        known_names = graph_manager.get_known_entity_names([collection])
        known_predicates = graph_manager.get_known_predicates([collection])

        result = extract_chunk(chunk, known_names, known_predicates, model=model, backend=backend)
        if result is None:
            continue
        # gleaning: 놓친 엔티티/관계를 몇 번 더 캐내 누적한다(옵트인, rounds>0일 때만).
        if rounds > 0:
            result, calls = glean_chunk(chunk, result, rounds, model=model, backend=backend)
            extra_calls += calls
        try:
            store_extraction(collection, result, source_id)
        except Exception as exc:
            logger.error("그래프 저장 실패, 이 청크 결과는 건너뜀: %s", exc)

    if extra_calls and _is_gemini:
        sqlite_manager.record_api_usage(extra_calls)  # gleaning으로 추가 소비한 호출 기록(Gemini만)
    document_store.commit_document(source_id, collection, file_name, content, content_hash)
    # [M2] 그래프가 바뀌었으니 이 컬렉션의 커뮤니티는 재빌드가 필요하다(LLM 없이 플래그만 세팅 — 값싼 인제스트 경로 불변).
    sqlite_manager.mark_communities_dirty(collection)
    logger.info("[%s] %s 처리 완료 (source_id=%s, 청크 %d개)", collection, file_name, source_id, len(chunks))
    return True
