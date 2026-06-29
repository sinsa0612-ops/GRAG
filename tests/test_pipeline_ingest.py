# 추출+저장 파이프라인이 LLM 출력을 검증하고 컬렉션별 그래프 DB에 올바르게 반영하는지 확인한다.
import json

import pytest

import pipeline.ingest as ingest
from config import settings
from db import document_store, graph_manager, sqlite_manager

C = "c1"

VALID_RESPONSE = json.dumps(
    {
        "entities": [{"name": "강택리", "type": "Person", "description": "기획자"}],
        "relations": [],
    }
)


def test_process_file_extracts_and_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "generate", lambda prompt, **kwargs: VALID_RESPONSE)
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    file_path = tmp_path / "memo.md"
    file_path.write_text("강택리는 기획자다.", encoding="utf-8")

    assert ingest.process_file(file_path, C) is True
    names = {e["name"] for e in graph_manager.get_all_entities()}
    assert "강택리" in names


def test_process_file_skips_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "generate", lambda prompt, **kwargs: VALID_RESPONSE)
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    file_path = tmp_path / "memo.md"
    file_path.write_text("강택리는 기획자다.", encoding="utf-8")

    assert ingest.process_file(file_path, C) is True
    assert ingest.process_file(file_path, C) is False


def test_extract_chunk_handles_malformed_json(monkeypatch):
    monkeypatch.setattr(ingest, "generate", lambda prompt, **kwargs: "이건 JSON이 아님")
    assert ingest.extract_chunk("아무 텍스트") is None


def test_extract_chunk_handles_schema_violation(monkeypatch):
    broken = json.dumps({"entities": [{"type": "Person"}]})
    monkeypatch.setattr(ingest, "generate", lambda prompt, **kwargs: broken)
    assert ingest.extract_chunk("아무 텍스트") is None


def test_parse_extraction_strips_markdown_fence():
    fenced = f"```json\n{VALID_RESPONSE}\n```"
    result = ingest._parse_extraction(fenced)
    assert result.entities[0].name == "강택리"


def test_extract_chunk_handles_llm_call_failure(monkeypatch):
    def boom(prompt, **kwargs):
        raise RuntimeError("일시적 네트워크 오류")

    monkeypatch.setattr(ingest, "generate", boom)
    assert ingest.extract_chunk("아무 텍스트") is None


def test_structural_failure_prevents_commit(tmp_path, monkeypatch):
    # 벡터 저장처럼 청크 루프 '바깥'에서 실패하면, 처리완료 도장이 찍히지 않아야 한다.
    monkeypatch.setattr(ingest, "generate", lambda prompt, **kwargs: VALID_RESPONSE)

    def broken_add_chunks(*a, **k):
        raise RuntimeError("벡터 저장소 다운")

    monkeypatch.setattr("db.vector_manager.add_chunks", broken_add_chunks)
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    file_path = tmp_path / "memo.md"
    file_path.write_text("강택리는 기획자다.", encoding="utf-8")

    with pytest.raises(RuntimeError):
        ingest.process_file(file_path, C)

    content_hash = document_store.compute_hash(file_path.read_text(encoding="utf-8"))
    assert document_store.needs_processing(C, "memo.md", content_hash) is True


def test_chunk_level_failure_does_not_block_other_chunks(tmp_path, monkeypatch):
    # 청크 3개 중 첫 호출만 실패시키고, 나머지는 정상적으로 처리되는지 확인한다.
    call_count = {"n": 0}

    def flaky_generate(prompt, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("일시적 네트워크 오류")
        return VALID_RESPONSE

    monkeypatch.setattr(ingest, "generate", flaky_generate)
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    monkeypatch.setattr(settings, "chunk_size", 10)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    file_path = tmp_path / "memo.md"
    file_path.write_text("강택리는 기획자다. 강택리는 ISA계좌를 운영한다.", encoding="utf-8")

    assert ingest.process_file(file_path, C) is True
    assert call_count["n"] > 1

    names = {e["name"] for e in graph_manager.get_all_entities()}
    assert "강택리" in names

    content_hash = document_store.compute_hash(file_path.read_text(encoding="utf-8"))
    assert document_store.needs_processing(C, "memo.md", content_hash) is False


def test_build_prompt_includes_known_vocabulary():
    # 이름은 '청크에 등장하는 것'만 힌트로 들어가므로, 청크 안에 이름이 포함되도록 둔다.
    prompt = ingest._build_prompt("ISA계좌 관련 텍스트", ["ISA계좌"], ["MANAGES"])
    assert "ISA계좌" in prompt
    assert "MANAGES" in prompt
    assert "PERSON" in prompt


def test_build_prompt_omits_name_hint_for_names_absent_from_chunk():
    prompt = ingest._build_prompt("전혀 다른 텍스트", ["ISA계좌"], [])
    assert "기존 엔티티 이름" not in prompt


def test_build_prompt_omits_hint_when_vocabulary_empty():
    prompt = ingest._build_prompt("텍스트", [], [])
    assert "기존 엔티티 이름" not in prompt


def test_process_file_passes_known_vocabulary_to_prompt(tmp_path, monkeypatch):
    # 같은 컬렉션에 이미 ISA계좌가 있고 새 문서가 그 이름을 언급하면, 재사용 힌트로 프롬프트에 포함돼야 한다.
    captured_prompts = []

    def capturing_generate(prompt, **kwargs):
        captured_prompts.append(prompt)
        return VALID_RESPONSE

    monkeypatch.setattr(ingest, "generate", capturing_generate)
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "절세용 계좌")

    file_path = tmp_path / "memo.md"
    file_path.write_text("강택리는 ISA계좌를 운영한다.", encoding="utf-8")

    ingest.process_file(file_path, C)

    assert any("기존 엔티티 이름: ISA계좌" in p for p in captured_prompts)


def test_process_file_refreshes_vocabulary_between_chunks(tmp_path, monkeypatch):
    # 청크 1이 만든 엔티티 이름이, 같은 파일을 처리하는 청크 2의 어휘 힌트에도 보여야 한다.
    first_response = json.dumps(
        {"entities": [{"name": "강택리", "type": "Person", "description": "기획자"}], "relations": []}
    )
    empty_response = json.dumps({"entities": [], "relations": []})
    captured_prompts = []

    def capturing_generate(prompt, **kwargs):
        captured_prompts.append(prompt)
        return first_response if len(captured_prompts) == 1 else empty_response

    monkeypatch.setattr(ingest, "generate", capturing_generate)
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    monkeypatch.setattr(settings, "chunk_size", 10)
    monkeypatch.setattr(settings, "chunk_overlap", 0)
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    file_path = tmp_path / "memo.md"
    file_path.write_text("강택리는 기획자다. 강택리는 ISA계좌를 운영한다.", encoding="utf-8")

    ingest.process_file(file_path, C)

    assert len(captured_prompts) >= 2
    # 첫 청크(강택리를 만들기 전)엔 힌트가 없고, 그 이후 같은 이름을 언급하는 청크엔 힌트가 보여야 한다.
    # (문장 경계 청킹에선 같은 이름이 등장하는 '후속' 청크가 정확히 두 번째가 아닐 수 있어 인덱스를 고정하지 않는다.)
    assert "기존 엔티티 이름: 강택리" not in captured_prompts[0]
    assert any("기존 엔티티 이름: 강택리" in p for p in captured_prompts[1:])


def test_resolve_canonical_name_routes_alias_to_existing_entity():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "절세용 계좌")
    graph_manager.add_alias(C, "ISA계좌", "ISA 계좌")

    resolved = ingest._resolve_canonical_name(C, "ISA 계좌")

    assert resolved == "ISA계좌"


def test_resolve_canonical_name_keeps_genuinely_new_name():
    graph_manager.init_schema()
    resolved = ingest._resolve_canonical_name(C, "처음 보는 엔티티")
    assert resolved == "처음 보는 엔티티"


def test_store_extraction_merges_known_alias_instead_of_creating_new_node():
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "ISA계좌", "Asset", "절세용 계좌")
    graph_manager.add_alias(C, "ISA계좌", "ISA 계좌")

    from schemas import ExtractedEntity, ExtractionResult

    result = ExtractionResult(
        entities=[ExtractedEntity(name="ISA 계좌", type="Asset", description="절세 계좌")],
        relations=[],
    )
    ingest.store_extraction(C, result, "doc1")

    names = {e["name"] for e in graph_manager.get_all_entities()}
    assert names == {"ISA계좌"}
