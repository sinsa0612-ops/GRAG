# 설명 통합 요약(desc_summarizer)이 다중 후보만 LLM으로 병합하고, 단일 후보는 건너뛰는지 검증한다.
# generate는 mock으로 차단해 네트워크 없이 1초 내 종료된다. 실 Ollama 통합은 파일 끝의 skipif 테스트로 격리.
import pytest
import requests

import pipeline.desc_summarizer as desc_summarizer
from config import settings
from db import graph_manager, sqlite_manager

C = "c1"


def test_summarize_descriptions_merges_multiple_candidates(monkeypatch):
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "기획자")
    sqlite_manager.upsert_desc_candidate(C, "강택리", "doc_a", "기획자")
    sqlite_manager.upsert_desc_candidate(C, "강택리", "doc_b", "ISA계좌 운영자")

    captured = {}

    def fake_generate(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["backend"] = kwargs.get("backend")
        return "통합된 설명"

    monkeypatch.setattr(desc_summarizer, "generate", fake_generate)

    updated = desc_summarizer.summarize_descriptions(C)

    assert updated == 1
    assert captured["backend"] == "ollama"  # 배치 요약 기본 백엔드(spec §A)
    assert "기획자" in captured["prompt"] and "ISA계좌 운영자" in captured["prompt"]
    assert graph_manager.get_entity(C, "강택리")["description"] == "통합된 설명"


def test_summarize_descriptions_skips_single_candidate(monkeypatch):
    # 후보 1개는 통합할 게 없으므로 LLM 호출 자체가 없어야 한다(호출 절약).
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "강택리", "Person", "기획자")
    sqlite_manager.upsert_desc_candidate(C, "강택리", "doc_a", "기획자")

    calls = []
    monkeypatch.setattr(desc_summarizer, "generate", lambda prompt, **kwargs: calls.append(1) or "무시됨")

    updated = desc_summarizer.summarize_descriptions(C)

    assert updated == 0
    assert calls == []
    assert graph_manager.get_entity(C, "강택리")["description"] == "기획자"  # 원래 설명 그대로


def test_summarize_descriptions_respects_min_candidates_override(monkeypatch):
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "A", "Person", "a1")
    sqlite_manager.upsert_desc_candidate(C, "A", "doc_a", "a1")
    sqlite_manager.upsert_desc_candidate(C, "A", "doc_b", "a2")
    sqlite_manager.upsert_desc_candidate(C, "A", "doc_c", "a3")

    calls = []
    monkeypatch.setattr(desc_summarizer, "generate", lambda prompt, **kwargs: calls.append(1) or "요약")

    # min_candidates=4로 올리면 후보 3개는 임계 미달이라 스킵돼야 한다.
    updated = desc_summarizer.summarize_descriptions(C, min_candidates=4)

    assert updated == 0
    assert calls == []


def test_summarize_descriptions_uses_config_default_threshold(monkeypatch):
    # min_candidates 인자를 안 주면 config.desc_summary_min_candidates를 쓴다(설정 중앙화).
    monkeypatch.setattr(settings, "desc_summary_min_candidates", 3)
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "A", "Person", "a1")
    sqlite_manager.upsert_desc_candidate(C, "A", "doc_a", "a1")
    sqlite_manager.upsert_desc_candidate(C, "A", "doc_b", "a2")  # 2개뿐 -> 설정값 3 미달

    monkeypatch.setattr(desc_summarizer, "generate", lambda prompt, **kwargs: "요약")

    updated = desc_summarizer.summarize_descriptions(C)

    assert updated == 0


def test_summarize_descriptions_skips_on_llm_failure_and_keeps_other_entities(monkeypatch):
    # 한 엔티티 요약이 실패해도 다른 엔티티 처리를 막지 않고, 실패한 쪽은 기존 설명을 유지한다.
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "실패", "Person", "실패 원래설명")
    sqlite_manager.upsert_desc_candidate(C, "실패", "doc_a", "f1")
    sqlite_manager.upsert_desc_candidate(C, "실패", "doc_b", "f2")
    graph_manager.upsert_entity(C, "성공", "Person", "성공 원래설명")
    sqlite_manager.upsert_desc_candidate(C, "성공", "doc_c", "s1")
    sqlite_manager.upsert_desc_candidate(C, "성공", "doc_d", "s2")

    def flaky_generate(prompt, **kwargs):
        if "실패" in prompt:
            raise RuntimeError("Ollama 연결 실패")
        return "성공 통합 설명"

    monkeypatch.setattr(desc_summarizer, "generate", flaky_generate)

    updated = desc_summarizer.summarize_descriptions(C)

    assert updated == 1
    assert graph_manager.get_entity(C, "실패")["description"] == "실패 원래설명"
    assert graph_manager.get_entity(C, "성공")["description"] == "성공 통합 설명"


def test_summarize_descriptions_no_entities_returns_zero():
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    assert desc_summarizer.summarize_descriptions(C) == 0


# 실 Ollama 서버가 로컬에서 응답할 때만 도는 통합 테스트 — 없으면 스킵(네트워크 없이 CI 통과 원칙 준수).
def _ollama_reachable() -> bool:
    try:
        requests.get(f"{settings.ollama_base_url}/api/tags", timeout=1.0)
        return True
    except requests.exceptions.RequestException:
        return False


@pytest.mark.skipif(not _ollama_reachable(), reason="Ollama가 로컬에서 응답하지 않음(미기동)")
def test_summarize_descriptions_against_real_ollama_smoke():
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "테스트엔티티", "Person", "첫 설명")
    sqlite_manager.upsert_desc_candidate(C, "테스트엔티티", "doc_a", "낮에 회의를 주재했다.")
    sqlite_manager.upsert_desc_candidate(C, "테스트엔티티", "doc_b", "저녁에 보고서를 작성했다.")

    updated = desc_summarizer.summarize_descriptions(C)

    assert updated == 1
    new_description = graph_manager.get_entity(C, "테스트엔티티")["description"]
    assert isinstance(new_description, str) and len(new_description) > 0
