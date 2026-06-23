# Pydantic 스키마가 LLM 출력의 입구 검증 역할을 제대로 하는지 확인한다.
import pytest
from pydantic import ValidationError

from schemas import EntityType, ExtractedEntity, ExtractionResult


def test_valid_extraction_parses():
    data = {
        "entities": [{"name": "강택리", "type": "Person", "description": "기획자"}],
        "relations": [
            {"source": "강택리", "target": "ISA계좌", "predicate": "MANAGES", "valid_from": "2026-01"}
        ],
    }
    result = ExtractionResult.model_validate(data)
    assert result.entities[0].name == "강택리"
    assert result.relations[0].predicate == "MANAGES"


def test_missing_required_field_rejected():
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate({"entities": [{"type": "Person"}]})


def test_empty_extraction_is_valid():
    result = ExtractionResult.model_validate({})
    assert result.entities == []
    assert result.relations == []


def test_known_type_is_normalized_to_uppercase_enum():
    # 대소문자/영문 표기 차이는 표준 Enum 값으로 흡수한다.
    entity = ExtractedEntity(name="강택리", type="person")
    assert entity.type is EntityType.PERSON


def test_unknown_type_falls_back_to_other_instead_of_failing():
    # 온톨로지 밖 type(한글/오타)은 검증을 깨지 않고 OTHER로 흡수해 데이터를 버리지 않는다.
    entity = ExtractedEntity(name="ISA계좌", type="자산")
    assert entity.type is EntityType.OTHER


def test_missing_type_defaults_to_other():
    entity = ExtractedEntity(name="무언가")
    assert entity.type is EntityType.OTHER
