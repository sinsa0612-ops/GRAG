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


def test_process_file_marks_collection_communities_dirty(tmp_path, monkeypatch):
    # [M2] 인제스트는 값싼 기본 경로를 유지하면서도(LLM 커뮤니티 작업 0), 해당 컬렉션을
    # '커뮤니티 재빌드 필요'로만 표시해야 한다(addendum §C-3 — 그래프 변이 계층에서의 dirty 마킹).
    monkeypatch.setattr(ingest, "generate", lambda prompt, **kwargs: VALID_RESPONSE)
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    file_path = tmp_path / "memo.md"
    file_path.write_text("강택리는 기획자다.", encoding="utf-8")

    assert sqlite_manager.is_communities_dirty(C) is True  # 아직 빌드된 적 없음 — 기본이 dirty
    sqlite_manager.clear_communities_dirty(C, "이전-서명")  # 방금 빌드해서 깨끗해졌다고 가정

    assert ingest.process_file(file_path, C) is True

    assert sqlite_manager.is_communities_dirty(C) is True


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


def test_parse_extraction_normalizes_alias_field_names():
    # 구조화 출력 미지원 모델(Gemma)이 name 대신 id/text/entity, source/target 대신 subject/object를
    # 써도 검증에서 버려지지 않고 흡수돼야 한다(실측된 실패 재현).
    raw = json.dumps(
        {
            "entities": [
                {"id": "여름", "type": "DATE"},
                {"text": "벽난로", "type": "OBJECT"},
                {"entity": "영국", "type": "LOCATION"},
            ],
            "relations": [{"subject": "스크루지", "predicate": "OWNS", "object": "벽난로"}],
        }
    )
    result = ingest._parse_extraction(raw)
    assert {e.name for e in result.entities} == {"여름", "벽난로", "영국"}
    assert (result.relations[0].source, result.relations[0].target) == ("스크루지", "벽난로")


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


def test_resolve_canonical_name_merges_josa_variant_at_index_time():
    # [한국어] 인덱싱 시점에 바로: 이미 "길동"이 있으면 조사 붙은 "길동이"는 새 노드 대신 "길동"으로 합친다.
    graph_manager.init_schema()
    graph_manager.upsert_entity(C, "길동", "PERSON", "홍길동전 주인공")

    resolved = ingest._resolve_canonical_name(C, "길동이")

    assert resolved == "길동"
    assert graph_manager.find_canonical_name(C, "길동이") == "길동"  # alias로 등록돼 이후에도 합쳐짐


def test_resolve_canonical_name_josa_only_merges_into_existing():
    # 조사 뗀 형태가 그래프에 없으면 원 표기를 그대로 신규 저장한다(짧은/새 이름 오병합 방지).
    graph_manager.init_schema()
    resolved = ingest._resolve_canonical_name(C, "길동이")  # "길동"이 아직 없음
    assert resolved == "길동이"


def test_store_extraction_merges_known_alias_instead_of_creating_new_node():
    graph_manager.init_schema()
    sqlite_manager.init_schema()  # [M1.5] store_extraction이 설명 후보도 sqlite에 병행 적재하므로 필요
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


def test_glean_chunk_merges_missed_and_stops_early(monkeypatch):
    # gleaning: 1차 결과(A)에 이어 라운드1이 B를 추가하고, 라운드2가 빈 결과면 조기 종료해야 한다.
    from schemas import ExtractedEntity, ExtractionResult

    base = ExtractionResult(entities=[ExtractedEntity(name="A", type="PERSON", description="")], relations=[])
    responses = iter(
        [
            json.dumps({"entities": [{"name": "B", "type": "PERSON", "description": ""}], "relations": []}),
            json.dumps({"entities": [], "relations": []}),  # 새로운 게 없음 → 조기 종료
        ]
    )
    monkeypatch.setattr(ingest, "generate", lambda prompt, **kwargs: next(responses))

    result, extra_calls = ingest.glean_chunk("텍스트", base, rounds=5)

    assert {e.name for e in result.entities} == {"A", "B"}
    assert extra_calls == 2  # 라운드1(B 추가) + 라운드2(빈 결과, 조기 종료). 라운드3~5는 호출 안 함


def test_glean_chunk_dedupes_repeated_entities(monkeypatch):
    # gleaning 라운드가 이미 있는 이름(A)을 또 내놓아도 중복 노드를 만들지 않고, 새 게 없으니 멈춘다.
    from schemas import ExtractedEntity, ExtractionResult

    base = ExtractionResult(entities=[ExtractedEntity(name="A", type="PERSON", description="")], relations=[])
    monkeypatch.setattr(
        ingest, "generate",
        lambda prompt, **kwargs: json.dumps({"entities": [{"name": "A", "type": "PERSON", "description": ""}], "relations": []}),
    )

    result, extra_calls = ingest.glean_chunk("텍스트", base, rounds=3)

    assert [e.name for e in result.entities] == ["A"]  # 중복 없음
    assert extra_calls == 1  # 첫 라운드에서 새 게 없어 바로 종료


# --- M1.5: 설명 후보(entity_desc_candidates) 적재 + hot-path 불변 ---


def test_store_extraction_fills_description_and_records_candidate():
    # hot-path 회귀 방지: description은 지금처럼 즉시 채워지고(로컬 질의가 그걸 씀),
    # source_doc 키의 후보도 '병행 추가'로 함께 적재돼야 한다(둘 다, 후자만이 아님).
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    from schemas import ExtractedEntity, ExtractionResult

    result = ExtractionResult(
        entities=[ExtractedEntity(name="강택리", type="PERSON", description="기획자")], relations=[]
    )
    ingest.store_extraction(C, result, "doc1")

    entity = graph_manager.get_entity(C, "강택리")
    assert entity["description"] == "기획자"  # hot-path 불변
    assert sqlite_manager.get_desc_candidates(C, "강택리") == ["기획자"]


def test_store_extraction_keys_candidates_by_source_doc():
    # 같은 엔티티가 다른 문서에서 다시 언급되면, source_doc이 다른 별개 후보로 쌓여야 한다.
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    from schemas import ExtractedEntity, ExtractionResult

    result_doc1 = ExtractionResult(
        entities=[ExtractedEntity(name="강택리", type="PERSON", description="1번 문서 설명")], relations=[]
    )
    result_doc2 = ExtractionResult(
        entities=[ExtractedEntity(name="강택리", type="PERSON", description="2번 문서 설명")], relations=[]
    )
    ingest.store_extraction(C, result_doc1, "doc1")
    ingest.store_extraction(C, result_doc2, "doc2")

    assert sqlite_manager.get_desc_candidates(C, "강택리") == ["1번 문서 설명", "2번 문서 설명"]
    # hot-path description은 가장 최근 upsert 값으로 남아있어야 한다(현행 동작 불변).
    assert graph_manager.get_entity(C, "강택리")["description"] == "2번 문서 설명"


def test_store_extraction_skips_candidate_for_empty_description():
    # 빈 description은 통합할 재료가 아니므로 후보로 남기지 않는다(빈 문자열이 카운트를 오염시키지 않게).
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    from schemas import ExtractedEntity, ExtractionResult

    result = ExtractionResult(
        entities=[ExtractedEntity(name="강택리", type="PERSON", description="")], relations=[]
    )
    ingest.store_extraction(C, result, "doc1")

    assert sqlite_manager.get_desc_candidates(C, "강택리") == []


def test_process_file_records_desc_candidates_end_to_end(tmp_path, monkeypatch):
    # ingest CLI 경로(process_file) 전체를 통해서도 후보가 source_doc 키로 남는지 확인한다.
    monkeypatch.setattr(ingest, "generate", lambda prompt, **kwargs: VALID_RESPONSE)
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    file_path = tmp_path / "memo.md"
    file_path.write_text("강택리는 기획자다.", encoding="utf-8")
    ingest.process_file(file_path, C)

    source_id = sqlite_manager.get_document_source_id(C, "memo.md")
    assert sqlite_manager.get_desc_candidates(C, "강택리") == ["기획자"]
    # 저장된 후보가 실제로 그 문서의 source_id를 키로 쓰는지 확인(캐스케이드 삭제가 정확히 짚을 수 있어야 함).
    with sqlite_manager.get_connection() as conn:
        row = conn.execute(
            "SELECT source_doc FROM entity_desc_candidates WHERE collection = ? AND entity_name = ?",
            (C, "강택리"),
        ).fetchone()
    assert row[0] == source_id


def test_process_file_gleaning_adds_missed_entities(tmp_path, monkeypatch):
    # process_file에 glean_rounds=1을 주면, 1차(에이)에 이어 gleaning이 찾은 비이도 저장돼야 한다.
    def fake_generate(prompt, **kwargs):
        if "놓친" in prompt:  # _GLEAN_PROMPT 분기
            return json.dumps({"entities": [{"name": "비이", "type": "Person", "description": ""}], "relations": []})
        return json.dumps({"entities": [{"name": "에이", "type": "Person", "description": ""}], "relations": []})

    monkeypatch.setattr(ingest, "generate", fake_generate)
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    file_path = tmp_path / "memo.md"
    file_path.write_text("에이는 사람이다.", encoding="utf-8")

    assert ingest.process_file(file_path, C, glean_rounds=1) is True
    names = {e["name"] for e in graph_manager.get_all_entities()}
    assert {"에이", "비이"} <= names


# --- 추출 백엔드 선택(--backend) + RPD 한도 기록 스킵 ---


def test_process_file_ollama_backend_skips_rpd_usage(tmp_path, monkeypatch):
    # ollama(로컬 무료)/CLI(구독) 백엔드는 Gemini 일일 한도(RPD)와 무관하므로 record_api_usage를 호출하면 안 된다.
    monkeypatch.setattr(ingest, "generate", lambda prompt, **kwargs: VALID_RESPONSE)
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    calls = {"n": 0}
    monkeypatch.setattr(sqlite_manager, "record_api_usage", lambda n: calls.__setitem__("n", calls["n"] + 1))
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    file_path = tmp_path / "memo.md"
    file_path.write_text("강택리는 기획자다.", encoding="utf-8")

    assert ingest.process_file(file_path, C, backend="ollama") is True
    assert calls["n"] == 0  # 로컬/구독 백엔드는 RPD를 소비하지 않음


def test_process_file_default_backend_records_rpd_usage(tmp_path, monkeypatch):
    # 대조군: 기본(Gemini) 백엔드는 기존대로 청크 수만큼 RPD를 기록해야 한다(스킵이 무조건이 아님을 고정).
    monkeypatch.setattr(ingest, "generate", lambda prompt, **kwargs: VALID_RESPONSE)
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    calls = {"n": 0}
    monkeypatch.setattr(sqlite_manager, "record_api_usage", lambda n: calls.__setitem__("n", calls["n"] + 1))
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    file_path = tmp_path / "memo.md"
    file_path.write_text("강택리는 기획자다.", encoding="utf-8")

    assert ingest.process_file(file_path, C) is True  # backend 미지정 = Gemini
    assert calls["n"] >= 1  # 기존 RPD 기록 경로 불변


def test_process_file_forwards_backend_to_generate(tmp_path, monkeypatch):
    # process_file → extract_chunk → generate 로 backend 문자열이 그대로 전달돼야 한다(라우터가 어댑터를 고를 수 있게).
    seen_backends = []

    def capturing_generate(prompt, **kwargs):
        seen_backends.append(kwargs.get("backend"))
        return VALID_RESPONSE

    monkeypatch.setattr(ingest, "generate", capturing_generate)
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    monkeypatch.setattr(sqlite_manager, "record_api_usage", lambda n: None)
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    file_path = tmp_path / "memo.md"
    file_path.write_text("강택리는 기획자다.", encoding="utf-8")

    ingest.process_file(file_path, C, backend="ollama")
    assert seen_backends and all(b == "ollama" for b in seen_backends)


# --- 분해 추출(decomposed extraction) — implementation_plan.md ②~⑤ + PoC(poc_v2.py) 정식화 ---


def test_is_noise_name_blocks_dates_percents_and_pure_numbers():
    assert ingest._is_noise_name("1995년 2월 16일") is True
    assert ingest._is_noise_name("27.6%") is True
    assert ingest._is_noise_name("1,057만 명") is True
    assert ingest._is_noise_name("1,057") is True
    assert ingest._is_noise_name("") is True
    assert ingest._is_noise_name("카카오") is False
    assert ingest._is_noise_name("김범수") is False


# 단위가 공백 없이 숫자에 붙은 형태 — 옛 필터가 "만 명"만 알아서 그대로 통과시키던 구멍.
def test_is_noise_name_blocks_korean_units_without_space():
    assert ingest._is_noise_name("1,057만") is True
    assert ingest._is_noise_name("3조원") is True
    assert ingest._is_noise_name("5천억") is True
    assert ingest._is_noise_name("2025년") is True
    assert ingest._is_noise_name("13.30%") is True
    assert ingest._is_noise_name("1시간 30분") is True


# 숫자를 품었지만 진짜 이름인 것들은 살아남아야 한다(필터가 과하게 먹으면 실제 엔티티가 사라진다).
def test_is_noise_name_keeps_real_names_containing_digits():
    assert ingest._is_noise_name("아이폰 15") is False
    assert ingest._is_noise_name("코로나19") is False
    assert ingest._is_noise_name("카카오 T") is False
    assert ingest._is_noise_name("제2공장") is False
    assert ingest._is_noise_name("G7") is False


def test_reconcile_dedupes_entities_by_name_and_relations_by_triple():
    from schemas import ExtractedEntity, ExtractedRelation

    entities = [
        ExtractedEntity(name="카카오", type="ORGANIZATION", description="회사"),
        ExtractedEntity(name="카카오", type="ORGANIZATION", description="회사"),
    ]
    relations = [
        ExtractedRelation(source="김범수", target="카카오", predicate="FOUNDED", valid_from="1998"),
        ExtractedRelation(source="김범수", target="카카오", predicate="FOUNDED", valid_from="1998"),
    ]
    result = ingest._reconcile(entities, relations)

    assert len(result.entities) == 2  # 카카오 dedup + 고아 김범수 승격
    assert len(result.relations) == 1


def test_reconcile_type_conflict_concrete_beats_other():
    from schemas import ExtractedEntity

    entities = [
        ExtractedEntity(name="카카오", type="OTHER", description=""),
        ExtractedEntity(name="카카오", type="ORGANIZATION", description="회사"),
    ]
    result = ingest._reconcile(entities, [])

    assert len(result.entities) == 1
    assert result.entities[0].type.value == "ORGANIZATION"
    assert result.entities[0].description == "회사"


def test_reconcile_type_conflict_between_two_concrete_types_keeps_first_and_warns(caplog):
    from schemas import ExtractedEntity

    entities = [
        ExtractedEntity(name="카카오뱅크", type="ORGANIZATION", description="첫번째"),
        ExtractedEntity(name="카카오뱅크", type="WORK", description="두번째"),
    ]
    with caplog.at_level("WARNING"):
        result = ingest._reconcile(entities, [])

    assert len(result.entities) == 1
    assert result.entities[0].type.value == "ORGANIZATION"  # 먼저 것 유지
    assert result.entities[0].description == "첫번째"
    assert any("타입 충돌" in r.message for r in caplog.records)


def test_reconcile_description_prefers_nonempty_then_first():
    from schemas import ExtractedEntity

    entities = [
        ExtractedEntity(name="A", type="PERSON", description=""),
        ExtractedEntity(name="A", type="PERSON", description="나중 설명"),
    ]
    result = ingest._reconcile(entities, [])
    assert result.entities[0].description == "나중 설명"  # 앞이 비어있으면 뒤 것 채택


def test_reconcile_adds_orphan_endpoint_as_other_and_keeps_relation():
    # 성공기준 (b) 회귀 가드: 패스 A가 카카오를 놓쳤어도 패스 B의 관계가 죽지 않아야 한다.
    from schemas import ExtractedEntity, ExtractedRelation

    entities = [ExtractedEntity(name="김범수", type="PERSON", description="창업자")]
    relations = [ExtractedRelation(source="김범수", target="카카오", predicate="FOUNDED", valid_from="1998")]

    result = ingest._reconcile(entities, relations)

    names = {e.name: e for e in result.entities}
    assert "카카오" in names
    assert names["카카오"].type.value == "OTHER"
    assert len(result.relations) == 1


def test_reconcile_drops_relation_when_orphan_endpoint_is_noise():
    from schemas import ExtractedEntity, ExtractedRelation

    entities = [ExtractedEntity(name="카카오", type="ORGANIZATION", description="")]
    relations = [
        ExtractedRelation(source="카카오", target="27.6%", predicate="OWNS", valid_from=""),
    ]

    result = ingest._reconcile(entities, relations)

    assert result.relations == []
    assert "27.6%" not in {e.name for e in result.entities}


def test_reconcile_returns_valid_extraction_result_type():
    from schemas import ExtractionResult

    result = ingest._reconcile([], [])
    assert isinstance(result, ExtractionResult)
    assert result.entities == []
    assert result.relations == []


def test_extract_entities_pass_calls_all_type_groups_and_filters_noise(monkeypatch):
    calls = []

    def fake_generate(prompt, **kwargs):
        idx = len(calls)
        calls.append(prompt)
        responses = [
            {"entities": [{"name": "정신아", "type": "PERSON", "description": "대표"}]},
            {"entities": [{"name": "카카오", "type": "ORGANIZATION", "description": ""}]},
            {"entities": [{"name": "카카오톡", "type": "WORK", "description": ""}, {"name": "27.6%", "type": "OTHER"}]},
            {"entities": [{"name": "판교", "type": "LOCATION", "description": ""}]},
        ]
        return json.dumps(responses[idx])

    monkeypatch.setattr(ingest, "generate", fake_generate)

    entities = ingest.extract_entities_pass("아무 텍스트")

    assert len(calls) == 4  # 타입군 4개 = 4콜
    names = {e.name for e in entities}
    assert names == {"정신아", "카카오", "카카오톡", "판교"}  # 노이즈(27.6%)는 걸러짐


def test_extract_entities_pass_partial_group_failure_keeps_others(monkeypatch):
    def flaky_generate(prompt, **kwargs):
        if '"PERSON"' in prompt:  # 사람 그룹만 실패시킨다(타입 리터럴로 그룹 식별, 문구 변경에 안전)
            raise RuntimeError("일시적 오류")
        return json.dumps({"entities": [{"name": "카카오", "type": "ORGANIZATION", "description": ""}]})

    monkeypatch.setattr(ingest, "generate", flaky_generate)

    entities = ingest.extract_entities_pass("아무 텍스트")

    assert entities is not None
    assert any(e.name == "카카오" for e in entities)


def test_extract_entities_pass_returns_none_when_all_groups_fail(monkeypatch):
    def boom(prompt, **kwargs):
        raise RuntimeError("Ollama 다운")

    monkeypatch.setattr(ingest, "generate", boom)

    assert ingest.extract_entities_pass("아무 텍스트") is None


def test_extract_relations_pass_skips_call_when_no_entities(monkeypatch):
    calls = {"n": 0}

    def fake_generate(prompt, **kwargs):
        calls["n"] += 1
        return json.dumps({"relations": []})

    monkeypatch.setattr(ingest, "generate", fake_generate)

    result = ingest.extract_relations_pass("텍스트", [])

    assert result == []
    assert calls["n"] == 0


def test_extract_relations_pass_injects_entity_list_and_direction_examples(monkeypatch):
    captured = []

    def fake_generate(prompt, **kwargs):
        captured.append(prompt)
        return json.dumps(
            {"relations": [{"source": "김범수", "target": "카카오", "predicate": "FOUNDED", "valid_from": "1998"}]}
        )

    monkeypatch.setattr(ingest, "generate", fake_generate)

    result = ingest.extract_relations_pass("텍스트", ["김범수", "카카오"], ["OWNS"])

    assert "김범수, 카카오" in captured[0]
    assert "FOUNDED" in captured[0]  # 방향 예시가 프롬프트에 콕 박혀 있어야 함
    assert result[0].predicate == "FOUNDED"


def test_extract_relations_pass_failure_returns_empty_list(monkeypatch):
    def boom(prompt, **kwargs):
        raise RuntimeError("일시적 오류")

    monkeypatch.setattr(ingest, "generate", boom)

    assert ingest.extract_relations_pass("텍스트", ["카카오"]) == []


def test_extract_chunk_decomposed_orchestrates_passes_and_reconciles(monkeypatch):
    def fake_generate(prompt, **kwargs):
        if "[엔티티 목록]" in prompt:
            return json.dumps(
                {"relations": [{"source": "김범수", "target": "카카오", "predicate": "FOUNDED", "valid_from": ""}]}
            )
        if '"PERSON"' in prompt:
            return json.dumps({"entities": [{"name": "김범수", "type": "PERSON", "description": ""}]})
        return json.dumps({"entities": []})

    monkeypatch.setattr(ingest, "generate", fake_generate)

    result = ingest.extract_chunk_decomposed("아무 텍스트")

    names = {e.name for e in result.entities}
    assert {"김범수", "카카오"} <= names
    assert result.relations[0].predicate == "FOUNDED"


def test_extract_chunk_decomposed_returns_none_when_entity_pass_fails(monkeypatch):
    def boom(prompt, **kwargs):
        raise RuntimeError("전체 실패")

    monkeypatch.setattr(ingest, "generate", boom)

    assert ingest.extract_chunk_decomposed("아무 텍스트") is None


def test_resolve_extraction_mode_auto_routes_by_backend():
    assert ingest._resolve_extraction_mode("auto", "ollama") == "decomposed"
    assert ingest._resolve_extraction_mode("auto", "claude_cli") == "decomposed"
    assert ingest._resolve_extraction_mode("auto", "codex_cli") == "decomposed"
    assert ingest._resolve_extraction_mode("auto", "gemini") == "single"
    assert ingest._resolve_extraction_mode("auto", None) == "single"


def test_resolve_extraction_mode_explicit_overrides_backend():
    assert ingest._resolve_extraction_mode("single", "ollama") == "single"
    assert ingest._resolve_extraction_mode("decomposed", "gemini") == "decomposed"


def test_process_file_decomposed_mode_dispatches_to_extract_chunk_decomposed(tmp_path, monkeypatch):
    calls = {"decomposed": 0, "single": 0, "glean": 0}
    monkeypatch.setattr(
        ingest, "extract_chunk_decomposed",
        lambda *a, **k: calls.__setitem__("decomposed", calls["decomposed"] + 1) or ExtractionResultStub(),
    )
    monkeypatch.setattr(ingest, "extract_chunk", lambda *a, **k: calls.__setitem__("single", calls["single"] + 1))
    monkeypatch.setattr(ingest, "glean_chunk", lambda *a, **k: calls.__setitem__("glean", calls["glean"] + 1))
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    file_path = tmp_path / "memo.md"
    file_path.write_text("강택리는 기획자다.", encoding="utf-8")

    ingest.process_file(file_path, C, backend="ollama", glean_rounds=3, extraction_mode="decomposed")

    assert calls["decomposed"] == 1
    assert calls["single"] == 0
    assert calls["glean"] == 0  # 분해 모드는 gleaning을 스킵해야 한다(계획 ④)


def test_process_file_single_mode_still_uses_extract_chunk_and_gleaning(tmp_path, monkeypatch):
    calls = {"decomposed": 0, "single": 0, "glean": 0}
    monkeypatch.setattr(
        ingest, "extract_chunk_decomposed",
        lambda *a, **k: calls.__setitem__("decomposed", calls["decomposed"] + 1) or ExtractionResultStub(),
    )

    def fake_extract_chunk(*a, **k):
        calls["single"] += 1
        return ExtractionResultStub()

    def fake_glean_chunk(chunk, base, rounds, **k):
        calls["glean"] += 1
        return base, 1

    monkeypatch.setattr(ingest, "extract_chunk", fake_extract_chunk)
    monkeypatch.setattr(ingest, "glean_chunk", fake_glean_chunk)
    monkeypatch.setattr("db.vector_manager.add_chunks", lambda *a, **k: None)
    sqlite_manager.init_schema()
    graph_manager.init_schema()

    file_path = tmp_path / "memo.md"
    file_path.write_text("강택리는 기획자다.", encoding="utf-8")

    ingest.process_file(file_path, C, backend="gemini", glean_rounds=1, extraction_mode="auto")

    assert calls["single"] == 1
    assert calls["decomposed"] == 0
    assert calls["glean"] == 1


def ExtractionResultStub():
    from schemas import ExtractionResult

    return ExtractionResult(entities=[], relations=[])


# 타입군 순회 순서(사람→조직→사물·개념→장소)가 결과를 가르면 안 된다. 포괄 그룹('사물·작품·개념·사건')이
# 먼저 돌아 장소를 EVENT로 낚아채도, 좁게 물은 장소 그룹의 답이 이겨야 한다(제주도가 EVENT로 굳던 실측).
def test_reconcile_narrow_type_beats_catch_all_regardless_of_order():
    from schemas import ExtractedEntity

    entities = [
        ExtractedEntity(name="제주도", type="EVENT", description="포괄 그룹이 먼저 주장"),
        ExtractedEntity(name="제주도", type="LOCATION", description="장소 그룹이 나중에 주장"),
    ]
    result = ingest._reconcile(entities, [])

    assert result.entities[0].type.value == "LOCATION"

    # 반대 순서로 들어와도 같은 결론이어야 한다(순서 무관 = 편향 없음).
    result_reversed = ingest._reconcile(list(reversed(entities)), [])
    assert result_reversed.entities[0].type.value == "LOCATION"


# 타입군 분할 토글 — 계획에는 있었지만 구현이 빠져 4분할이 하드코딩돼 있던 항목.
def test_entity_type_split_off_uses_single_combined_call(monkeypatch):
    from config import settings

    calls = []

    def fake_generate(prompt, **kwargs):
        calls.append(prompt)
        return json.dumps(
            {"entities": [
                {"name": "정신아", "type": "PERSON", "description": "대표"},
                {"name": "판교", "type": "LOCATION", "description": ""},
            ]}
        )

    monkeypatch.setattr(ingest, "generate", fake_generate)
    monkeypatch.setattr(settings, "extraction_entity_type_split", False)

    entities = ingest.extract_entities_pass("아무 텍스트")

    assert len(calls) == 1  # 4콜 → 1콜
    assert {e.name for e in entities} == {"정신아", "판교"}
    # 통합 프롬프트도 같은 온톨로지로 답하도록 타입 목록을 담고 있어야 한다.
    assert "PERSON|ORGANIZATION|LOCATION" in calls[0]
