# LLM 추출 결과를 DB에 넣기 전 검증하는 Pydantic 스키마 (입구 검증 전용).
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


# 엔티티 type을 고정된 온톨로지로 가둔다 — 같은 뜻의 타입이 파편화(Person/사람/인물)되는 것을 막는다.
# 목록에 없는 값은 OTHER로 정규화한다(아래 ExtractedEntity 검증자).
class EntityType(StrEnum):
    PERSON = "PERSON"
    ORGANIZATION = "ORGANIZATION"
    LOCATION = "LOCATION"
    EVENT = "EVENT"
    WORK = "WORK"
    CONCEPT = "CONCEPT"
    OBJECT = "OBJECT"
    DATE = "DATE"
    OTHER = "OTHER"


# LLM이 추출한 엔티티(명사) 하나를 표현한다.
class ExtractedEntity(BaseModel):
    name: str = Field(min_length=1)
    type: EntityType = EntityType.OTHER
    description: str = ""

    @field_validator("type", mode="before")
    @classmethod
    # 모델이 목록 밖 type(소문자/한글/오타 등)을 내놓아도 검증을 통째로 실패시키지 않고 OTHER로 흡수한다.
    def _normalize_type(cls, value: object) -> object:
        if isinstance(value, EntityType):
            return value
        if isinstance(value, str) and value.strip().upper() in EntityType.__members__:
            return value.strip().upper()
        return EntityType.OTHER


# LLM이 추출한 엔티티 간 관계 하나를 표현한다.
class ExtractedRelation(BaseModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    valid_from: str = ""


# LLM 추출 응답 전체(엔티티 목록 + 관계 목록)를 표현한다.
class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relations: list[ExtractedRelation] = Field(default_factory=list)
