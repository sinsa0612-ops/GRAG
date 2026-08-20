# 문서 처리 파이프라인 — 읽기 → 변경감지 → 청킹 → LLM 추출 → 검증 → 저장.
import json
import logging
import re
from pathlib import Path

from pydantic import ValidationError

from adapters.llm_adapter import generate
from config import settings
from db import document_store, graph_manager, sqlite_manager, vector_manager
from pipeline import entity_resolution
from schemas import EntityType, ExtractedEntity, ExtractedRelation, ExtractionResult

logger = logging.getLogger(__name__)

# 엔티티 type을 고정 온톨로지로 가두기 위해 허용 목록을 프롬프트에 직접 명시한다(schemas.EntityType와 단일 출처).
_ALLOWED_TYPES = ", ".join(t.value for t in EntityType)

# 자주 쓰는 관계 이름 시드 목록. type처럼 강제하지는 않고(열린 어휘), '가능하면 이것부터 재사용'하도록 권장만 한다.
# 초기 문서들이 같은 관계를 제각각(WORKS_AT/EMPLOYED_BY/...)으로 만들어 파편화되는 것을 줄인다.
# FOUNDED~OPERATES는 분해 추출 PoC(설립/인수/지분/분사/운영 방향 실측 검증)로 추가됐다.
_SEED_PREDICATES = ", ".join(
    [
        "WORKS_AT", "MEMBER_OF", "PART_OF", "LOCATED_IN", "OWNS", "MANAGES",
        "CREATED", "PRODUCES", "USES", "PARTICIPATED_IN", "OCCURRED_ON",
        "HAS_ROLE", "AFFILIATED_WITH", "CAUSES", "RELATED_TO",
        "FOUNDED", "ACQUIRED", "MERGED_WITH", "SPUN_OFF", "HOLDS_STAKE", "OPERATES",
        # 인물·생활 관계. 기업 어휘만 있으면 서사 텍스트에서 담을 그릇이 없어 관계가 통째로 비어버린다
        # (실측: 봄봄 8청크에서 관계 0개 — 데릴사위·혼약·머슴살이를 넣을 predicate가 없었다).
        "PARENT_OF", "SPOUSE_OF", "BETROTHED_TO", "SIBLING_OF", "WORKS_FOR",
        "LIVES_IN", "FRIEND_OF", "LIKES", "CONFLICTS_WITH", "PROMISED_TO",
    ]
)

_EXTRACTION_PROMPT = """\
다음 텍스트에서 중요한 엔티티(명사)와 그 사이 관계를 빠짐없이 추출해. 어떤 주제의 글이든 똑같이 적용해.
사람·조직뿐 아니라 중요한 사물·개념·작품·장소·사건·기술·제품도 모두 엔티티로 뽑아(특정 분야에 치우치지 마).
동명이인·동명 사물 구분을 위해 description을 꼭 적어줘.
엔티티 type은 반드시 아래 목록 중 하나만 대문자 그대로 골라. 애매하면 OTHER로 둬:
{allowed_types}
관계 predicate는 항상 영어 대문자 스네이크케이스로만 적어(한글 금지. 예: WORKS_AT, CREATED, USES).
가능하면 아래 자주 쓰는 관계를 먼저 재사용하고, 정말 맞는 게 없을 때만 같은 형식으로 새로 만들어:
{seed_predicates}
valid_from처럼 날짜/시점 정보는 텍스트에 명시적으로 적혀 있을 때만 채우고,
적혀 있지 않으면 절대 추측하지 말고 빈 문자열("")로 남겨줘.

추출 품질 규칙(꼭 지켜):
- 순수 식별자·번호·코드만으로 된 것(전화번호, 일련·등록·계좌번호, 각종 코드 등)은 엔티티로 만들지 말고, 관련 엔티티의 description에 녹여 적어.
- 엔티티 이름(name)은 가장 짧은 표준 명칭으로 적어. 제목·문장 전체를 통째로 이름으로 쓰지 말고 핵심어로 줄여.
- 같은 대상은 표기를 통일해. 띄어쓰기·괄호·직함만 다르게 새로 만들지 마(예: "홍길동"과 "홍길동 박사"를 따로 만들지 말 것).
- RELATED_TO는 마땅한 구체 관계가 없을 때만 최후의 수단으로 써. 가능하면 구체적인 predicate를 골라.
- 관계는 텍스트가 실제로 말하는 것만 만들어. 단지 같은 문장·목록에 함께 나온다는 이유만으로 잇지 마.

예시 1)
입력: "홍길동은 2020년부터 OO전자에 다녔다."
출력: {{"entities": [{{"name": "홍길동", "type": "PERSON", "description": "OO전자 직원"}}, \
{{"name": "OO전자", "type": "ORGANIZATION", "description": "홍길동이 다닌 회사"}}], \
"relations": [{{"source": "홍길동", "target": "OO전자", "predicate": "WORKS_AT", "valid_from": "2020"}}]}}

예시 2)
입력: "에디슨은 전구를 발명했다. 전구는 필라멘트로 빛을 낸다."
출력: {{"entities": [{{"name": "에디슨", "type": "PERSON", "description": "전구를 발명한 사람"}}, \
{{"name": "전구", "type": "OBJECT", "description": "빛을 내는 장치"}}, \
{{"name": "필라멘트", "type": "OBJECT", "description": "전구에서 빛을 내는 부품"}}], \
"relations": [{{"source": "에디슨", "target": "전구", "predicate": "CREATED", "valid_from": ""}}, \
{{"source": "전구", "target": "필라멘트", "predicate": "USES", "valid_from": ""}}]}}

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

# --- 분해 추출(decomposed extraction) — 로컬 모델 과부하 회피 ---
# 근거: 단일패스(엔티티+관계 동시 요구)는 qwen3:14b에서 구조가 무너진다(정신아 누락·소유방향 역전·
# 날짜 노이즈화 실측, implementation_plan.md ①). 좁게 나눠 여러 번 물으면 회복된다(같은 문서로 4/4 검증,
# scratchpad/matchup/poc_v2.py). 아래 프롬프트 문구는 그 PoC를 그대로 이식한 것 — 임의 변경 금지(회귀 위험).

# 패스 A(엔티티) — schemas.EntityType을 DATE 제외 4개 좁은 타입군으로 나눠 그룹마다 1콜씩 묻는다.
# 날짜는 어떤 그룹에도 없다(valid_from 전용 — 엔티티로 승격시키지 않음).
_ENTITY_TYPE_GROUPS: dict[str, str] = {
    "사람": (
        '다음 텍스트에 등장하는 모든 "사람(인물)"을 그 직책·역할과 함께 빠짐없이 뽑아라. '
        "사람이 아닌 것은 넣지 마라.\n"
        '이야기를 "나/내/저"로 이끄는 1인칭 화자(주인공)도 반드시 한 사람으로 뽑아라. '
        '그 화자의 이름이 텍스트에 따로 없으면 name을 "나"로 쓰고 description에 "1인칭 서술자"라고 적어라.\n'
        "같은 대상을 여러 호칭(예: 장인/빙장/봉필씨)으로 부르면 반드시 하나로 묶어라. "
        "대표 이름 하나만 name에 쓰고, 나머지 호칭은 전부 aliases 배열에 넣어라(없으면 빈 배열).\n"
        '순수 JSON만: {"entities":[{"name":"장인","aliases":["빙장","봉필씨"],"type":"PERSON",'
        '"description":"직책/역할"}]}\n\n'
    ),
    "조직·집단": (
        '다음 텍스트에 등장하는 모든 "조직·회사·기관·단체·정부·법인"을 빠짐없이 뽑아라. '
        "사람·장소·날짜·숫자는 넣지 마라. 이름은 가장 짧은 표준 명칭.\n"
        '순수 JSON만: {"entities":[{"name":"이름","type":"ORGANIZATION","description":"짧은 설명"}]}\n\n'
    ),
    "사물·작품·개념·사건": (
        '다음 텍스트에 등장하는 모든 "사물·제품·서비스·앱·작품·기술·개념·사건"을 빠짐없이 뽑아라. '
        "사람·조직·장소·날짜·숫자는 넣지 마라. 이름은 가장 짧은 표준 명칭.\n"
        '순수 JSON만: {"entities":[{"name":"이름","type":"OBJECT|WORK|CONCEPT|EVENT","description":"짧은 설명"}]}\n\n'
    ),
    "장소": (
        '다음 텍스트에 등장하는 모든 "장소·지역·건물"을 빠짐없이 뽑아라. '
        "그 외(사람·조직·날짜)는 넣지 마라.\n"
        '순수 JSON만: {"entities":[{"name":"이름","type":"LOCATION","description":"짧은 설명"}]}\n\n'
    ),
}

# 타입군 분할을 끈(extraction_entity_type_split=False) 경우에 쓰는 통합 엔티티 프롬프트 — 4콜을 1콜로 줍인다.
# 위 4개 그룹의 지시를 한 번에 담되 타입 목록을 명시해, 분할본과 같은 온톨로지로 답하게 한다.
# 분할본보다 요구가 넓어 누락이 늘 수 있다(그래서 기본값은 분할) — 속도를 사는 옵션이다.
# [ASSUMPTION·범위결정] coreference 지시는 사람에만 건다 — 조직·사물 호칭 변형은 A/B 근거가 없고
# 서로 다른 조직을 우연히 엮을 위험(카카오/카카오뱅크류)이 이득보다 크다(implementation_plan.md §과제1.2).
_ENTITY_COMBINED_PROMPT = (
    "다음 텍스트에 등장하는 모든 대상을 빠짐없이 뽑아라. "
    "사람(직책·역할 포함), 조직·회사·기관·단체·정부·법인, 사물·제품·서비스·앱·작품·기술·개념·사건, "
    "장소·지역·건물을 모두 포함한다. 날짜·숫자·비율은 넣지 마라. 이름은 가장 짧은 표준 명칭.\n"
    '이야기를 "나/내/저"로 이끄는 1인칭 화자(주인공)도 반드시 한 사람으로 뽑아라(이름이 없으면 name을 "나"로).\n'
    "사람은 같은 대상을 여러 호칭으로 부르면 반드시 하나로 묶어라. 대표 이름 하나만 name에 쓰고 "
    "나머지 호칭은 aliases 배열에 넣어라(사람이 아닌 대상은 aliases를 비워둬도 된다).\n"
    'type은 다음 중 하나: PERSON|ORGANIZATION|LOCATION|OBJECT|WORK|CONCEPT|EVENT\n'
    '순수 JSON만: {"entities":[{"name":"장인","aliases":["빙장"],"type":"PERSON","description":"짧은 설명"}]}\n\n'
)

# 엔티티 패스 전용 어휘 힌트(이름만 — 이 패스는 관계를 뽑지 않아 predicate 힌트가 필요 없다).
_ENTITY_PASS_VOCAB_HINT = """\
이미 그래프에 쓰이고 있는 이름이야. 같은 대상이면 새로 만들지 말고 아래 표현을 정확히 그대로 재사용해.
- 기존 엔티티 이름: {names}

"""

# 패스 B(관계) — 패스 A가 뽑은 엔티티 목록을 주입하고, 방향이 헷갈리는 관계는 예시로 콕 박아 강제한다.
# (방향 역전·창업자 누락을 고친 핵심이 '엔티티 목록 + 방향 예시'였다 — PoC 실측.)
_RELATION_PASS_PROMPT = """\
아래 [엔티티 목록]에 있는 대상들 사이의 관계만, 텍스트가 실제로 말하는 것만 뽑아라.
이름은 반드시 목록의 표기를 그대로 써라. 관계에는 방향이 있다. 아래 예시의 방향을 정확히 따르라:
- 설립/창립: 설립한 사람·모체가 source. 예) 김범수 -[FOUNDED]-> 카카오
- 소유(모회사→자회사): 카카오 -[OWNS]-> 카카오뱅크
- 지분 보유(주주→회사): 국민연금공단 -[HOLDS_STAKE]-> 카카오
- 인수: 인수한 쪽이 source. 예) 카카오 -[ACQUIRED]-> 로엔엔터테인먼트
- 분사: 모체가 source. 예) 카카오 -[SPUN_OFF]-> 카카오모빌리티
- 운영: 운영 주체가 source. 예) 카카오모빌리티 -[OPERATES]-> 카카오 T
- 부모/자식: 윗사람이 source. 예) 아버지 -[PARENT_OF]-> 아들
- 혼인/약혼: 예) 철수 -[BETROTHED_TO]-> 영희
- 사람 밑에서 일함: 일하는 쪽이 source. 예) 머슴 -[WORKS_FOR]-> 주인
- 거주/체류: 사는 쪽이 source. 예) 노인 -[LIVES_IN]-> 마을
- 약속: 약속한 쪽이 source. 예) 장인 -[PROMISED_TO]-> 사위
- 갈등/다툼: 예) 형 -[CONFLICTS_WITH]-> 동생

위 예시는 '방향'을 보여주려는 것일 뿐 허용 목록이 아니다. 소설·일기·기사 등 어떤 글이든
그 글이 실제로 말하는 관계를 뽑아라. 기업 관계가 아니어도 된다 — 사람과 사람, 사람과 장소·사물
사이의 관계도 모두 대상이다.

지킬 것(어기면 그래프가 못 쓰게 된다):
- 텍스트에 그렇게 적혀 있는 관계만 뽑아라. 같은 문장·같은 목록에 나왔다는 이유만으로 잇지 마라.
  근거 문장을 댈 수 없으면 그 관계는 넣지 마라.
- 한 쌍 사이에는 가장 정확한 관계 하나만 남겨라. 같은 두 대상을 여러 predicate로 중복해 잇지 마라.
- 확실한 게 없으면 적게 뽑아라. 빈 배열도 정답이다 — 억지로 채우지 마라.
- RELATED_TO는 구체 관계가 정말 없을 때만 쓰는 최후의 수단이다.
- 같은 대상의 다른 호칭끼리 잇지 마라(예: '장인님'과 '빙장'은 같은 사람이다).
predicate는 영어 대문자 스네이크. 가능하면 다음을 먼저 재사용: {seed_predicates}
날짜/시점은 valid_from 속성으로만(엔티티로 만들지 마, 없으면 빈 문자열).
순수 JSON만 출력: {{"relations":[{{"source":"...","target":"...","predicate":"...","valid_from":""}}]}}
{known_predicates_hint}
[엔티티 목록]
{entity_list}

텍스트:
{chunk}
"""


# backend 미지정(None)은 '기본값 백엔드'로 해소한다. 라우터(llm_adapter.generate)는 None을 Gemini로
# 보내므로, 이 해소가 없으면 추출 함수를 직접 호출할 때 문서가 조용히 외부 경로로 향한다 —
# 이 프로젝트의 기본은 완전 로컬이고(규칙 1) settings.ingest_backend가 그 단일 출처다.
# (키가 없는 머신에서는 클라이언트 생성 단계에서 즉시 실패해 전송은 없었지만, 키를 넣는 순간 살아나는 경로다.)
def _resolve_backend(backend: str | None) -> str:
    return backend or settings.ingest_backend


# 숫자에 붙어 다니는 단위·조수사. 긴 것부터 지워야 "시간"이 "시"+"간"으로 쪼개지지 않는다.
_NUMBER_UNITS = tuple(
    sorted(
        ("퍼센트", "시간", "억", "조", "만", "천", "백", "원", "달러", "명", "개", "주", "건",
         "회", "배", "위", "년", "월", "일", "시", "분", "초", "%"),
        key=len,
        reverse=True,
    )
)


# 날짜·비율·수량 같은 '수치'가 엔티티로 올라오는 것을 모델 지시와 무관하게 코드로 최종 차단한다.
# 판정 방식: 숫자가 든 이름에서 숫자·구분자·단위를 걷어내고 남는 게 없으면 그건 대상이 아니라 수치다.
# 단위를 열거해 부분일치로 잡던 옛 방식은 열거에 없는 형태를 그대로 통과시켰다("1,057만" — "만 명"에는
# 걸리지만 공백 없는 단위에는 안 걸림). 남는 글자를 보는 쪽이 열거 누락에 무너지지 않는다.
# 숫자가 아예 없는 이름은 손대지 않는다 — "아이폰 15"·"코로나19"처럼 숫자를 품은 진짜 이름도
# 잔여 글자가 남아 살아남는다. 엔티티 패스 파싱 시점과 _reconcile의 고아 끝점 승격 시점 둘 다에서 쓴다.
def _is_noise_name(name: str) -> bool:
    if not name:
        return True
    if not re.search(r"\d", name):
        return False
    residue = re.sub(r"[\d,.\s]", "", name)
    for unit in _NUMBER_UNITS:
        residue = residue.replace(unit, "")
    return residue == ""


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
    backend = _resolve_backend(backend)
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
    backend = _resolve_backend(backend)
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


def _build_entity_pass_prompt(group_instruction: str, chunk: str, known_names: list[str]) -> str:
    relevant_names = [name for name in known_names if name and name in chunk][: settings.max_name_hints]
    vocab_hint = ""
    if relevant_names:
        vocab_hint = _ENTITY_PASS_VOCAB_HINT.format(names=", ".join(relevant_names))
    return vocab_hint + group_instruction + chunk


# 패스 A 응답 하나(그룹 1개분)를 파싱해 엔티티만 뽑는다. 개별 항목이 스키마에 안 맞으면 그 항목만 버리고
# 계속한다(청크 전체를 버리지 않음). 노이즈 필터는 여기서 1차로 걸고 _reconcile에서 방어적으로 한 번 더 건다.
def _parse_entity_group(raw_text: str) -> list[ExtractedEntity]:
    cleaned = raw_text.replace("```json", "").replace("```", "").strip()
    data = json.loads(cleaned)
    if isinstance(data, dict):
        data = _normalize_field_names(data)
    items = data.get("entities") if isinstance(data, dict) else None
    entities = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            entity = ExtractedEntity.model_validate(item)
        except ValidationError:
            continue
        if _is_noise_name(entity.name):
            continue
        entities.append(entity)
    return entities


# 패스 A(엔티티) — 청크 하나를 4개 타입군(_ENTITY_TYPE_GROUPS)으로 나눠 그룹마다 1콜씩 좁게 묻는다.
# extraction_entity_type_split=False면 통합 프롬프트 1콜로 대신한다(콜 수↓, recall↓).
# 그룹 하나가 실패(호출 오류/JSON 파싱 실패)해도 다른 그룹은 계속 진행한다 — 그룹 전부가 실패했을 때만
# 이 청크를 통째로 건너뛴다(None 반환). 반환값은 dedup 전 원시 목록 — dedup/타입충돌은 _reconcile 소관.
def extract_entities_pass(
    chunk: str,
    known_names: list[str] | None = None,
    model: str | None = None,
    backend: str | None = None,
    type_split: bool | None = None,
) -> list[ExtractedEntity] | None:
    backend = _resolve_backend(backend)
    entities: list[ExtractedEntity] = []
    ok_groups = 0
    # type_split이 None이면 하위호환: 기존 전역 토글(extraction_entity_type_split)을 그대로 읽는다.
    # 명시되면(프로파일이 결정한 값) 그 값이 전역 토글보다 우선한다.
    split = settings.extraction_entity_type_split if type_split is None else type_split
    groups = _ENTITY_TYPE_GROUPS if split else {"통합": _ENTITY_COMBINED_PROMPT}
    for group_label, instruction in groups.items():
        prompt = _build_entity_pass_prompt(instruction, chunk, known_names or [])
        try:
            raw_text = generate(
                prompt, model=model, backend=backend,
                temperature=settings.extraction_temperature, format_json=True,
            )
            entities.extend(_parse_entity_group(raw_text))
            ok_groups += 1
        except json.JSONDecodeError as exc:
            logger.warning("엔티티 패스[%s] 응답 파싱 실패, 이 타입군은 건너뜀: %s", group_label, exc)
        except Exception as exc:
            logger.warning("엔티티 패스[%s] 호출 실패, 이 타입군은 건너뜀: %s", group_label, exc)
    if ok_groups == 0:
        logger.error("엔티티 패스 전체 실패, 이 청크는 건너뜀")
        return None
    return entities


# 패스 B 응답을 파싱해 관계만 뽑는다. 개별 항목이 스키마에 안 맞으면 그 항목만 버린다.
def _parse_relations(raw_text: str) -> list[ExtractedRelation]:
    cleaned = raw_text.replace("```json", "").replace("```", "").strip()
    data = json.loads(cleaned)
    if isinstance(data, dict):
        data = _normalize_field_names(data)
    items = data.get("relations") if isinstance(data, dict) else None
    relations = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            relations.append(ExtractedRelation.model_validate(item))
        except ValidationError:
            continue
    return relations


# 패스 B(관계) — 패스 A가 뽑은 엔티티 이름 목록을 주입해 그 사이의 관계만 묻는다(방향 예시 포함).
# 엔티티가 하나도 없으면 물을 대상이 없으므로 호출 자체를 생략한다. 실패해도 빈 리스트만 반환하고
# 예외를 올리지 않는다 — 관계 패스 실패가 이미 뽑힌 엔티티까지 버리게 하면 안 된다(계획 ④).
def extract_relations_pass(
    chunk: str,
    entity_names: list[str],
    known_predicates: list[str] | None = None,
    model: str | None = None,
    backend: str | None = None,
) -> list[ExtractedRelation]:
    backend = _resolve_backend(backend)
    if not entity_names:
        return []
    known_predicates_hint = ""
    if known_predicates:
        known_predicates_hint = f"이미 이 컬렉션에 쓰인 관계 이름(같은 뜻이면 재사용): {', '.join(known_predicates)}\n"
    prompt = _RELATION_PASS_PROMPT.format(
        seed_predicates=_SEED_PREDICATES,
        known_predicates_hint=known_predicates_hint,
        entity_list=", ".join(entity_names),
        chunk=chunk,
    )
    try:
        raw_text = generate(
            prompt, model=model, backend=backend,
            temperature=settings.extraction_temperature, format_json=True,
        )
    except Exception as exc:
        logger.warning("관계 패스 호출 실패, 관계 없이 진행: %s", exc)
        return []
    try:
        return _parse_relations(raw_text)
    except json.JSONDecodeError as exc:
        logger.warning("관계 패스 응답 파싱 실패, 관계 없이 진행: %s", exc)
        return []


# 타입 충돌 우선순위 — 값이 작을수록 이긴다.
# 좁게 물어본 그룹(사람/조직/장소)의 답이 '사물·작품·개념·사건' 포괄 그룹의 답을 이긴다. 포괄 그룹은
# 질문 범위가 넓어 장소·인물까지 끌어오는 과잉 주장이 잦기 때문이다(제주도를 EVENT로 잡던 실측).
# DATE는 엔티티로 승격시키지 않는 값이라 OTHER와 같은 최하위로 둔다.
_TYPE_PRIORITY: dict[EntityType, int] = {
    EntityType.PERSON: 0,
    EntityType.ORGANIZATION: 0,
    EntityType.LOCATION: 0,
    EntityType.OBJECT: 1,
    EntityType.WORK: 1,
    EntityType.CONCEPT: 1,
    EntityType.EVENT: 1,
    EntityType.DATE: 2,
    EntityType.OTHER: 2,
}


# 같은 이름이 두 타입군에서 다른 타입으로 올라왔을 때 어느 쪽을 남길지 정한다.
# 우선순위가 다르면 순회 순서와 무관하게 좁은 쪽이 이긴다. 우선순위가 같은 진짜 충돌(예: PERSON vs
# ORGANIZATION)은 먼저 온 쪽을 유지한다 — 어느 쪽이 옳은지 코드가 알 방법이 없다.
# 둘 다 실질 타입인 충돌은 무엇이 버려졌는지 남긴다(OTHER가 낀 건 충돌이 아니라 보강이라 조용히 넘어간다).
def _resolve_type_conflict(name: str, existing: EntityType, incoming: EntityType) -> EntityType:
    if existing == incoming:
        return existing
    incoming_wins = _TYPE_PRIORITY[incoming] < _TYPE_PRIORITY[existing]
    winner, loser = (incoming, existing) if incoming_wins else (existing, incoming)
    if EntityType.OTHER not in (existing, incoming):
        logger.warning("엔티티 타입 충돌: %s (%s 유지, %s 무시)", name, winner.value, loser.value)
    return winner


# 패스 A/B 결과를 하나의 ExtractionResult로 합치는 순수 함수(LLM 없음) — dedup·타입충돌·고아 끝점 처리.
# 1) 엔티티는 이름(name) 키로 dedup. 타입 충돌: 구체 타입이 OTHER를 이기고, 둘 다 구체면 먼저 것을 유지하며
#    경고를 남긴다. description은 비어있지 않은 쪽 우선, 둘 다 있으면 먼저 것.
# 2) 관계는 (source, predicate, target) 3튜플로 dedup(먼저 것 유지).
# 3) 관계 끝점이 엔티티 목록에 없으면(패스 A가 놓쳤지만 패스 B가 같은 청크에서 실존을 확인한 경우)
#    OTHER로 최소 엔티티를 추가해 관계를 살린다 — graph_manager.upsert_relation이 양 끝 노드가
#    없으면 관계를 조용히 버리기 때문에 필수(안 하면 "김범수→FOUNDED→카카오"가 죽는다).
#    단, 끝점 이름 자체가 노이즈(_is_noise_name)면 엔티티를 만들지 않고 그 관계를 통째로 버린다.
def _reconcile(entities: list[ExtractedEntity], relations: list[ExtractedRelation]) -> ExtractionResult:
    entity_map: dict[str, ExtractedEntity] = {}
    for entity in entities:
        if _is_noise_name(entity.name):
            continue
        existing = entity_map.get(entity.name)
        if existing is None:
            entity_map[entity.name] = entity
            continue
        winner_type = _resolve_type_conflict(entity.name, existing.type, entity.type)
        winner_desc = existing.description or entity.description
        # 별칭은 버리지 않고 합집합으로 합친다(옛 구현은 마지막 것으로 덮어써 앞서 뽑힌 호칭이 소실됐다).
        winner_aliases = list(dict.fromkeys(existing.aliases + entity.aliases))
        entity_map[entity.name] = ExtractedEntity(
            name=entity.name, type=winner_type, description=winner_desc, aliases=winner_aliases
        )

    relation_map: dict[tuple[str, str, str], ExtractedRelation] = {}
    for relation in relations:
        key = (relation.source, relation.predicate, relation.target)
        if key not in relation_map:
            relation_map[key] = relation

    # 한 쌍(source,target)당 관계 1개로 축약한다(방향은 보존 — A→B와 B→A는 별개 쌍).
    # 프롬프트가 "한 쌍 하나만" 지시해도 확률적이라 관측된 데카르트 곱 스팸(장인님-WORKS_FOR->장모님 +
    # 장인님-PARENT_OF->... 동시 등장)을 코드로 최종 강제한다. 드물게 진짜 복수 관계(장인이 사위의
    # 부모이자 고용주)를 버리는 트레이드오프를 감수한다 — 서사에선 드물고 스팸 피해가 압도적이다.
    seen_pairs: set[tuple[str, str]] = set()
    pair_deduped_relations: list[ExtractedRelation] = []
    for relation in relation_map.values():
        pair = (relation.source, relation.target)
        if pair in seen_pairs:
            logger.warning(
                "관계 폐기(한 쌍 중복 — 한 쌍당 1개만 유지): %s -[%s]-> %s",
                relation.source, relation.predicate, relation.target,
            )
            continue
        seen_pairs.add(pair)
        pair_deduped_relations.append(relation)

    kept_relations: list[ExtractedRelation] = []
    orphan_endpoints = 0
    total_endpoints = 0
    for relation in pair_deduped_relations:
        endpoints = (relation.source, relation.target)
        total_endpoints += len(endpoints)
        if any(ep not in entity_map and _is_noise_name(ep) for ep in endpoints):
            logger.warning("관계 폐기(끝점이 노이즈 이름): %s -[%s]-> %s", relation.source, relation.predicate, relation.target)
            continue
        for ep in endpoints:
            if ep not in entity_map:
                entity_map[ep] = ExtractedEntity(name=ep, type=EntityType.OTHER, description="")
                orphan_endpoints += 1
        kept_relations.append(relation)

    if total_endpoints and orphan_endpoints > total_endpoints / 2:
        logger.warning(
            "고아 끝점 과다(%d/%d) — 패스 B가 목록과 다른 표기를 쓰는 이름 드리프트 의심", orphan_endpoints, total_endpoints
        )

    return ExtractionResult(entities=list(entity_map.values()), relations=kept_relations)


# 분해 추출 오케스트레이션 — 패스 A(엔티티, 타입군 4분할) → 패스 B(관계, 엔티티 목록 주입) → _reconcile.
# 패스 A가 완전히 실패하면(그룹 전부 실패) 이 청크를 통째로 건너뛴다(None). 패스 B 실패는 빈 관계로
# 흡수되고 엔티티는 그대로 살아남는다(위 extract_relations_pass 계약).
def extract_chunk_decomposed(
    chunk: str,
    known_names: list[str] | None = None,
    known_predicates: list[str] | None = None,
    model: str | None = None,
    backend: str | None = None,
    type_split: bool | None = None,
) -> ExtractionResult | None:
    backend = _resolve_backend(backend)
    entities = extract_entities_pass(chunk, known_names, model=model, backend=backend, type_split=type_split)
    if entities is None:
        return None
    entity_names = [e.name for e in entities]
    relations = extract_relations_pass(chunk, entity_names, known_predicates, model=model, backend=backend)
    return _reconcile(entities, relations)


# name이 그 컬렉션의 기존 엔티티 정식 이름/alias와 정확히 일치하면 그 정식 이름으로 치환한다.
# 일치하는 게 alias 매칭이었다면(이름 자체와는 다름) 그 사실을 alias로 기록해둔다.
def _resolve_canonical_name(collection: str, name: str) -> str:
    canonical = graph_manager.find_canonical_name(collection, name)
    if canonical is not None:
        if canonical != name:
            graph_manager.add_alias(collection, canonical, name)
        return canonical
    # [한국어] 인덱싱 시점에 바로 합친다 — 조사가 눌어붙은 표기("길동이")면 조사를 뗀 형태("길동")가 이미
    # 그래프에 있는지 재확인해, 있으면 그 캐노니컬로 합치고 원 표기는 alias로 남긴다(기존에 있는 이름으로만
    # 합치므로 짧은 이름을 잘못 만들어낼 위험 없음). 없으면 원래 이름 그대로 신규 저장.
    stripped = entity_resolution.strip_trailing_josa(name)
    if stripped != name:
        canonical = graph_manager.find_canonical_name(collection, stripped)
        if canonical is not None:
            graph_manager.add_alias(collection, canonical, name)
            return canonical
    return name


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
        # [coref] 엔티티 패스가 뽑은 동일인 별칭을 실제로 그래프에 등록한다 — 프롬프트 지시만으론
        # 그래프에 안 남는다. 별칭이 이미 별개 노드로 존재하면(예: 앞선 청크가 "빙장"을 독립 인물로
        # 잘못 만든 경우) merge_entity_into로 그 노드의 관계를 canonical로 흡수하고 노드를 지운다
        # (5조각을 실제로 하나로). ingest 시점 노드 병합은 되돌리기 어려워 coref_ingest_merge로 끌 수
        # 있게 둔다(False면 문자열 별칭만 등록하고 병합은 하지 않음).
        # PERSON에만 적용한다 — coreference 프롬프트 지시도 사람 그룹에만 걸었고(§과제1.2 범위결정),
        # 조직·사물 타입에 aliases가 실려 오면(모델 오작동/할루시네이션) 서로 다른 조직이 이름 우연으로
        # 오병합되는 회귀(카카오≠카카오뱅크)를 코드로 원천 차단한다.
        if entity.type != EntityType.PERSON and entity.aliases:
            logger.warning("PERSON이 아닌 엔티티의 aliases 무시(오병합 방지): %s(%s)", entity.name, entity.type.value)
        for alias in entity.aliases if entity.type == EntityType.PERSON else []:
            if alias == resolved_name:
                continue
            if settings.coref_ingest_merge:
                existing_canonical = graph_manager.find_canonical_name(collection, alias)
                if existing_canonical is not None and existing_canonical != resolved_name:
                    graph_manager.merge_entity_into(
                        collection, keep_name=resolved_name, drop_name=existing_canonical
                    )
            graph_manager.add_alias(collection, resolved_name, alias)
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
# backend별 auto 해소 대상 — 로컬/구독 백엔드는 구조화 출력 강제가 안 돼 분해 추출이 기본이다(계획 ④).
_DECOMPOSED_AUTO_BACKENDS = ("ollama", "claude_cli", "codex_cli")


# extraction_mode("auto"|"single"|"decomposed")를 실제 모드로 해소한다. single/decomposed는 backend와
# 무관하게 강제. auto는 backend로 판단: 로컬/구독=decomposed(구조화 출력 강제 불가라 좁혀 물어야 함),
# gemini/미지정=single(response_schema가 predicate 등을 이미 강제해줌).
def _resolve_extraction_mode(mode: str, backend: str | None) -> str:
    if mode in ("single", "decomposed"):
        return mode
    return "decomposed" if backend in _DECOMPOSED_AUTO_BACKENDS else "single"


# profile("quality"|"fast"|"auto")을 실제 type_split(bool)로 해소한다 — implementation_plan.md ②.
# quality→True(5콜), fast→False(2콜), auto→ 청크수가 임계값 이하면 True(짧은 문서=quality),
# 초과면 False(긴 문서=fast). 알 수 없는 값(오타 등)은 True로 안전 폴백(무손실 쪽이 기본).
def _resolve_type_split(profile: str, chunk_count: int) -> bool:
    if profile == "fast":
        return False
    if profile == "auto":
        return chunk_count <= settings.extraction_fast_chunk_threshold
    return True


# 1인칭 단수 주어의 닫힌 소집합(KISS) — entity_resolution.strip_trailing_josa가 1~2자를 조사분리 대상에서
# 보호하므로 이 표기들만 안정적으로 살아남는다. "나무"·"나리" 같은 부분일치는 정확 일치가 아니라 안 걸린다.
_FIRST_PERSON_PRONOUNS = frozenset({"나", "내", "저"})


# 파일명 접두사에 붙은 타임스탬프(예: 1787216682412_봄봄.md)를 벗겨 사람이 읽는 문서 라벨만 남긴다.
# 1인칭 승격(document 범위)의 대표 이름에 타임스탬프가 그대로 노출되지 않게 하기 위함.
def _doc_label_from_file_name(file_name: str) -> str:
    return re.sub(r"^\d+_", "", Path(file_name).stem)


# scope("document"|"collection")에 맞는 1인칭 대표 이름을 만든다. document는 문서마다 분리해
# 다작 코퍼스(소설 여러 편)의 화자가 한 노드로 오병합되는 것을 막고, collection은 컬렉션 전체를
# 한 화자로 합쳐 같은 글쓴이의 일기 같은 코퍼스에 맞춘다(코드가 코퍼스 성격을 알 수 없어 config로 남김).
def _first_person_label(scope: str, doc_label: str) -> str:
    return "화자" if scope == "collection" else f"화자({doc_label})"


# "나"류 1인칭 단수 주어를 문서 단위(또는 컬렉션 단위) 대표 엔티티로 승격하는 순수 함수(LLM 없음).
# scope="off"면 결과를 그대로 반환(포착만 하고 승격 안 함). process_file이 store_extraction 직전에 호출한다
# (그 지점에만 사람이 읽는 file_name이 있다 — store_extraction은 source_id만 받는다).
def _promote_first_person(
    result: ExtractionResult, doc_label: str, scope: str | None = None
) -> ExtractionResult:
    scope = settings.first_person_scope if scope is None else scope
    if scope == "off":
        return result
    label = _first_person_label(scope, doc_label)

    def rename(name: str) -> str:
        return label if name in _FIRST_PERSON_PRONOUNS else name

    entities = [
        ExtractedEntity(name=rename(e.name), type=e.type, description=e.description, aliases=e.aliases)
        for e in result.entities
    ]
    relations = [
        ExtractedRelation(
            source=rename(r.source), target=rename(r.target), predicate=r.predicate, valid_from=r.valid_from
        )
        for r in result.relations
    ]
    return ExtractionResult(entities=entities, relations=relations)


def process_file(
    file_path: Path,
    collection: str,
    model: str | None = None,
    glean_rounds: int | None = None,
    backend: str | None = None,
    extraction_mode: str | None = None,
    profile: str | None = None,
) -> bool:
    # 미지정은 기본 백엔드(settings.ingest_backend)로 먼저 해소한다 — 해소 전에 판단하면 로컬로 돌면서
    # Gemini RPD를 깎는 오집계가 난다. 실제 Gemini일 때만 record_api_usage를 탄다(아래 _is_gemini).
    backend = _resolve_backend(backend)
    _is_gemini = backend == "gemini"
    mode = _resolve_extraction_mode(extraction_mode or settings.extraction_mode, backend)
    content = file_path.read_text(encoding="utf-8")
    file_name = file_path.name
    content_hash = document_store.compute_hash(content)

    # 재개 가능 인제스트(B) — 이전 시도의 진행 마커가 있고, 파일이 그때와 같으며, 아직 미완료면
    # 같은 source_id를 재사용해 완료된 청크(done_chunks)부터 이어간다(implementation_plan.md ③).
    progress = sqlite_manager.get_ingest_progress(collection, file_name)
    resuming = bool(
        progress
        and progress["content_hash"] == content_hash
        and progress["done_chunks"] < progress["total_chunks"]
    )
    if resuming:
        source_id = progress["source_id"]
        start_idx = progress["done_chunks"]
    else:
        if not document_store.needs_processing(collection, file_name, content_hash):
            logger.info("[%s] %s 은(는) 변경사항이 없어 건너뜁니다.", collection, file_name)
            return False
        source_id = document_store.prepare_replacement(file_name)
        start_idx = 0

    # 변경 감지/원본 보존은 raw(content)로 끝냈으니, 청킹·임베딩·추출 입력만 표 노이즈를 걷어낸 정리본으로 쓴다.
    cleaned = document_store.clean_markdown(content)
    chunks = document_store.chunk_text(cleaned, settings.chunk_size, settings.chunk_overlap)

    # chunk_size 등 설정이 중단 사이 바뀌어 청크 수가 어긋나면 재개를 폐기하고 처음부터 다시 돈다
    # (같은 source_id는 재사용 — 그래프 쓰기가 멱등이라 안전, implementation_plan.md ③ 정합성 5).
    if resuming and len(chunks) != progress["total_chunks"]:
        resuming = False
        start_idx = 0

    if not resuming:
        sqlite_manager.upsert_ingest_progress(collection, file_name, source_id, content_hash, len(chunks), 0)

    # gleaning 라운드 수 결정(인자 우선, 없으면 설정값).
    rounds = settings.glean_rounds if glean_rounds is None else glean_rounds
    # 청크마다 1차 LLM 호출 1번이 나가므로, 청크 수만큼 오늘 사용량에 기록한다(RPD 추적).
    # gleaning 추가 호출은 실제 소비한 만큼 아래에서 따로 더한다.
    if _is_gemini:
        sqlite_manager.record_api_usage(len(chunks))
    vector_manager.add_chunks(source_id, chunks, collection)  # upsert(멱등) — 재개 시 재add해도 중복 없음

    # A: 프로파일이 type_split을 결정한다 — decomposed 모드에서만 의미 있다(single/gemini는 개념이 없어 무시).
    resolved_profile = profile or settings.extraction_profile
    type_split = _resolve_type_split(resolved_profile, len(chunks)) if mode == "decomposed" else None
    # 1인칭 승격 라벨은 파일명에서 한 번만 뽑는다(청크마다 동일 — file_name은 루프 내내 안 바뀜).
    doc_label = _doc_label_from_file_name(file_name)

    extra_calls = 0
    for i, chunk in enumerate(chunks):
        if i < start_idx:
            continue  # 재개: 이미 완료된 청크는 재추출하지 않는다.
        # 청크마다 매번 새로 가져온다 — 같은 문서를 처리하는 중에도 앞 청크가 막 만든
        # 엔티티/관계 이름을 뒤 청크가 못 보면, 한 문서 안에서도 표현이 갈라진다(91청크 실제 검증으로 확인됨).
        # 어휘 힌트는 '이 컬렉션' 범위로만 모은다 — 다른 사업의 이름이 섞여 들어가지 않게.
        known_names = graph_manager.get_known_entity_names([collection])
        known_predicates = graph_manager.get_known_predicates([collection])

        if mode == "decomposed":
            # 분해 모드는 패스 분리 자체가 gleaning의 역할(놓친 것 캐기)을 이미 하므로 gleaning은 스킵한다(계획 ④).
            result = extract_chunk_decomposed(
                chunk, known_names, known_predicates, model=model, backend=backend, type_split=type_split
            )
        else:
            result = extract_chunk(chunk, known_names, known_predicates, model=model, backend=backend)
            # gleaning: 놓친 엔티티/관계를 몇 번 더 캐내 누적한다(옵트인, rounds>0일 때만).
            if result is not None and rounds > 0:
                result, calls = glean_chunk(chunk, result, rounds, model=model, backend=backend)
                extra_calls += calls
        if result is None:
            continue
        result = _promote_first_person(result, doc_label)
        try:
            store_extraction(collection, result, source_id)
            # 저장 성공 직후에만 durable 마커 증가. store가 예외로 실패하면 마커를 안 올려, 재개 시 그 청크를
            # 다시 처리한다(멱등이라 안전). except 밖에 두면 실패 청크가 완료로 스킵돼 데이터가 유실된다.
            sqlite_manager.set_ingest_progress_done(collection, file_name, i + 1)
        except Exception as exc:
            logger.error("그래프 저장 실패, 이 청크 결과는 건너뜀: %s", exc)

    if extra_calls and _is_gemini:
        sqlite_manager.record_api_usage(extra_calls)  # gleaning으로 추가 소비한 호출 기록(Gemini만)
    document_store.commit_document(source_id, collection, file_name, content, content_hash)
    sqlite_manager.delete_ingest_progress(collection, file_name)  # 커밋 성공 후에만 진행 마커 제거
    # [M2] 그래프가 바뀌었으니 이 컬렉션의 커뮤니티는 재빌드가 필요하다(LLM 없이 플래그만 세팅 — 값싼 인제스트 경로 불변).
    sqlite_manager.mark_communities_dirty(collection)
    logger.info("[%s] %s 처리 완료 (source_id=%s, 청크 %d개)", collection, file_name, source_id, len(chunks))
    return True
