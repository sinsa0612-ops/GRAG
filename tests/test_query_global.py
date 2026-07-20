# 글로벌(map-reduce) 검색(query.answer_question_global, M4)을 mock 백엔드로 단위검증한다:
# (a) MAP 관련도 필터(0점/파싱실패 버림), (b) REDUCE 종합(관련도 상위 순으로 프롬프트에 나열),
# (c) 레벨 필터, (d) --all(collections=None)의 컬렉션별 union 격벽, (e) RPD 기록은 gemini일 때만,
# (f) 리포트 없음/전부 무관 두 경우 모두 LLM 낭비 호출 없이 안내 문자열을 돌려줌.
# generate는 항상 mock으로 차단해 네트워크 없이 1초 내 종료된다(실 Ollama 통합은 별도 e2e에서 확인).
import json

import pytest
import requests

import graphrag_cli
import query
from config import settings
from db import graph_manager, sqlite_manager

C1 = "사업A"
C2 = "사업B"


def _seed_report(collection: str, community_id: str, level: int, title: str, summary: str) -> None:
    sqlite_manager.upsert_community_report(collection, community_id, level, title, summary, None)


# --- (a) MAP 관련도 필터 ---


def test_map_reports_drops_zero_relevance(monkeypatch):
    reports = [
        {"collection": C1, "community_id": "c1", "title": "무관 주제", "summary": "요약1"},
        {"collection": C1, "community_id": "c2", "title": "관련 주제", "summary": "요약2"},
    ]

    def fake_generate(prompt, backend=None, model=None):
        if "무관 주제" in prompt:
            return json.dumps({"relevance": 0, "partial_answer": "무관함"})
        return json.dumps({"relevance": 7, "partial_answer": "관련 부분답변"})

    monkeypatch.setattr(query, "generate", fake_generate)

    scored = query._map_reports(reports, "질문?")

    assert len(scored) == 1
    assert scored[0]["partial_answer"] == "관련 부분답변"


def test_map_reports_skips_parse_failure_and_continues(monkeypatch):
    reports = [
        {"collection": C1, "community_id": "bad", "title": "깨진 응답", "summary": "요약"},
        {"collection": C1, "community_id": "good", "title": "정상 응답", "summary": "요약"},
    ]

    def fake_generate(prompt, backend=None, model=None):
        if "깨진 응답" in prompt:
            return "이건 JSON이 아님"
        return json.dumps({"relevance": 5, "partial_answer": "정상 부분답변"})

    monkeypatch.setattr(query, "generate", fake_generate)

    scored = query._map_reports(reports, "질문?")

    assert len(scored) == 1
    assert scored[0]["partial_answer"] == "정상 부분답변"


def test_map_reports_drops_negative_relevance(monkeypatch):
    reports = [{"collection": C1, "community_id": "c1", "title": "T", "summary": "S"}]
    monkeypatch.setattr(
        query, "generate",
        lambda *a, **k: json.dumps({"relevance": -3, "partial_answer": "이상한 값"}),
    )

    assert query._map_reports(reports, "질문?") == []


# --- (b) REDUCE 종합 ---


def test_reduce_answers_sorts_by_relevance_before_prompting(monkeypatch):
    scored = [
        {"relevance": 3, "partial_answer": "낮은 관련도 답변"},
        {"relevance": 9, "partial_answer": "높은 관련도 답변"},
    ]
    captured = {}

    def fake_generate(prompt, backend=None, model=None):
        captured["prompt"] = prompt
        captured["backend"] = backend
        return "종합된 최종 답변"

    monkeypatch.setattr(query, "generate", fake_generate)

    result = query._reduce_answers(scored, "질문?")

    assert result == "종합된 최종 답변"
    prompt = captured["prompt"]
    assert prompt.index("높은 관련도 답변") < prompt.index("낮은 관련도 답변")  # 관련도 상위가 먼저
    assert captured["backend"] == settings.global_search_reduce_backend  # 하드코딩 아닌 config 값


# --- (c) 레벨 필터 ---


def test_answer_question_global_filters_by_level(monkeypatch):
    sqlite_manager.init_schema()
    _seed_report(C1, "top", 0, "최상위 리포트", "최상위 요약")
    _seed_report(C1, "leaf", 1, "리프 리포트", "리프 요약")

    captured_prompts = []

    def fake_generate(prompt, backend=None, model=None):
        captured_prompts.append(prompt)
        return json.dumps({"relevance": 5, "partial_answer": "부분답변"})

    monkeypatch.setattr(query, "generate", fake_generate)

    query.answer_question_global("질문?", collections=[C1], level=1)

    map_prompts = captured_prompts[:-1]  # 마지막 호출은 REDUCE
    assert any("리프 리포트" in p for p in map_prompts)
    assert not any("최상위 리포트" in p for p in map_prompts)


def test_answer_question_global_defaults_to_configured_level(monkeypatch):
    sqlite_manager.init_schema()
    _seed_report(C1, "top", settings.global_search_default_level, "기본레벨 리포트", "요약")
    _seed_report(C1, "other", settings.global_search_default_level + 1, "다른레벨 리포트", "요약")

    captured_prompts = []

    def fake_generate(prompt, backend=None, model=None):
        captured_prompts.append(prompt)
        return json.dumps({"relevance": 5, "partial_answer": "부분답변"})

    monkeypatch.setattr(query, "generate", fake_generate)

    query.answer_question_global("질문?", collections=[C1])  # level 미지정

    map_prompts = captured_prompts[:-1]
    assert any("기본레벨 리포트" in p for p in map_prompts)
    assert not any("다른레벨 리포트" in p for p in map_prompts)


# --- (d) --all(collections=None)의 컬렉션별 union 격벽 ---


def test_answer_question_global_all_unions_reports_across_collections(monkeypatch):
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    # collections=None(--all)은 _print_communities_status와 동일하게 "문서/그래프 어느 쪽에든 존재가
    # 확인된 컬렉션"을 나열해 대상으로 삼으므로, 각 컬렉션에 엔티티를 하나씩 심어 존재를 표시해 둔다.
    graph_manager.upsert_entity(C1, "A엔티티", "OTHER", "")
    graph_manager.upsert_entity(C2, "B엔티티", "OTHER", "")
    _seed_report(C1, "a1", 0, "사업A 리포트", "사업A 요약")
    _seed_report(C2, "b1", 0, "사업B 리포트", "사업B 요약")

    captured_prompts = []

    def fake_generate(prompt, backend=None, model=None):
        captured_prompts.append(prompt)
        return json.dumps({"relevance": 5, "partial_answer": "부분답변"})

    monkeypatch.setattr(query, "generate", fake_generate)

    result = query.answer_question_global("질문?", collections=None)

    map_prompts = captured_prompts[:-1]
    assert any("사업A 리포트" in p for p in map_prompts)
    assert any("사업B 리포트" in p for p in map_prompts)  # 두 컬렉션 리포트가 모두 union됨
    # REDUCE는 LLM 원시 응답을 그대로 반환한다(파싱 없음) — fake_generate가 항상 같은 JSON 문자열을 주므로 그대로 나온다.
    assert result == json.dumps({"relevance": 5, "partial_answer": "부분답변"})


def test_answer_question_global_scoped_collection_excludes_others(monkeypatch):
    # 격벽: collections=[C1]로 좁히면 C2의 리포트는 절대 섞이지 않는다.
    sqlite_manager.init_schema()
    _seed_report(C1, "a1", 0, "사업A 전용 리포트", "요약")
    _seed_report(C2, "b1", 0, "사업B 전용 리포트", "요약")

    captured_prompts = []
    monkeypatch.setattr(
        query, "generate",
        lambda prompt, backend=None, model=None: captured_prompts.append(prompt)
        or json.dumps({"relevance": 5, "partial_answer": "부분답변"}),
    )

    query.answer_question_global("질문?", collections=[C1])

    map_prompts = captured_prompts[:-1]
    assert any("사업A 전용 리포트" in p for p in map_prompts)
    assert not any("사업B 전용 리포트" in p for p in map_prompts)


# --- 리포트 없음 / 전부 무관(관련도 0) ---


def test_answer_question_global_no_reports_returns_guidance_without_llm_call(monkeypatch):
    # 커뮤니티가 한 번도 안 빌드된(리포트 자체가 없는) 스코프 — MAP도 REDUCE도 호출하지 않는다.
    sqlite_manager.init_schema()
    calls = []
    monkeypatch.setattr(query, "generate", lambda *a, **k: calls.append(1) or "무시됨")

    result = query.answer_question_global("질문?", collections=["없는컬렉션"])

    assert result == query._NO_REPORTS_MESSAGE
    assert calls == []


def test_answer_question_global_all_irrelevant_skips_reduce_call(monkeypatch):
    # 리포트는 있지만(빌드는 됨) MAP이 전부 관련도 0을 주면, REDUCE는 아예 호출하지 않고 안내만 돌려준다.
    sqlite_manager.init_schema()
    _seed_report(C1, "a1", 0, "무관 리포트", "요약")
    calls = []

    def fake_generate(prompt, backend=None, model=None):
        calls.append(prompt)
        return json.dumps({"relevance": 0, "partial_answer": ""})

    monkeypatch.setattr(query, "generate", fake_generate)

    result = query.answer_question_global("질문?", collections=[C1])

    assert result == query._NO_RELEVANT_MESSAGE
    assert len(calls) == 1  # MAP 1콜만 있고 REDUCE 호출은 없어야 함(낭비 호출 방지)


# --- (e) RPD 기록은 backend가 gemini로 해석될 때만 ---


def test_map_and_reduce_do_not_record_usage_with_default_ollama_backend(monkeypatch):
    sqlite_manager.init_schema()
    _seed_report(C1, "a1", 0, "리포트", "요약")
    monkeypatch.setattr(
        query, "generate",
        lambda *a, **k: json.dumps({"relevance": 5, "partial_answer": "답변"}),
    )

    query.answer_question_global("질문?", collections=[C1])

    assert sqlite_manager.get_api_usage_today() == 0  # 기본 backend=ollama는 무료라 미기록


def test_map_and_reduce_record_usage_when_backend_is_gemini(monkeypatch):
    sqlite_manager.init_schema()
    _seed_report(C1, "a1", 0, "리포트", "요약")
    monkeypatch.setattr(settings, "global_search_map_backend", "gemini")
    monkeypatch.setattr(settings, "global_search_reduce_backend", "gemini")
    monkeypatch.setattr(
        query, "generate",
        lambda *a, **k: json.dumps({"relevance": 5, "partial_answer": "답변"}),
    )

    query.answer_question_global("질문?", collections=[C1])

    # MAP 1콜(리포트 1개) + REDUCE 1콜 = 2건 기록되어야 한다.
    assert sqlite_manager.get_api_usage_today() == 2


# --- CLI --mode 배선: 신규 global + 기존 local 회귀 ---


def test_cli_query_mode_defaults_to_local_and_behaves_unchanged(monkeypatch, capsys):
    # --mode를 생략하면(기본 local) 기존과 동일하게 answer_question 경로로 간다 — M4 이전 회귀 확인.
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    monkeypatch.setattr("query.vector_manager.query_similar", lambda q, top_k=8, collections=None: [])
    monkeypatch.setattr(query, "generate", lambda prompt: "로컬답변")

    graphrag_cli.main(["query", "아무 질문"])

    out = capsys.readouterr().out
    assert "로컬답변" in out


def test_cli_query_mode_global_dispatches_to_answer_question_global_when_fresh(monkeypatch, capsys):
    # 커뮤니티가 빌드돼 있고(clear_communities_dirty로 fresh 표시) stale이 아니면 answer_question_global을
    # 그대로 호출한다(graphrag_cli._cmd_query_global의 stale 폴백 분기를 타지 않음).
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    _seed_report(C1, "a1", 0, "제목", "요약")
    sqlite_manager.clear_communities_dirty(C1, "sig")

    def fake_generate(prompt, backend=None, model=None):
        if "제목" in prompt:
            return json.dumps({"relevance": 5, "partial_answer": "부분답"})
        return "글로벌답변"

    monkeypatch.setattr(query, "generate", fake_generate)

    graphrag_cli.main(["query", "아무 질문", "--mode", "global", "--collection", C1])

    out = capsys.readouterr().out
    assert "글로벌답변" in out


def test_cli_query_mode_global_passes_level_through(monkeypatch, capsys):
    # --level이 answer_question_global까지 그대로 전달되는지 확인한다(레벨1만 대상이 되어야 함).
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    _seed_report(C1, "top", 0, "레벨0제목", "레벨0요약")
    _seed_report(C1, "leaf", 1, "레벨1제목", "레벨1요약")
    sqlite_manager.clear_communities_dirty(C1, "sig")

    def fake_generate(prompt, backend=None, model=None):
        if "레벨0제목" in prompt:
            pytest.fail("--level 1을 줬는데 레벨0 리포트가 MAP에 들어옴")
        if "레벨1제목" in prompt:
            return json.dumps({"relevance": 5, "partial_answer": "부분답"})
        return "레벨1글로벌답변"

    monkeypatch.setattr(query, "generate", fake_generate)

    graphrag_cli.main(["query", "아무 질문", "--mode", "global", "--collection", C1, "--level", "1"])

    out = capsys.readouterr().out
    assert "레벨1글로벌답변" in out


def test_cli_query_mode_global_falls_back_to_local_when_stale(monkeypatch, capsys):
    # [ASSUMPTION] 커뮤니티가 없거나 stale(재빌드 필요)이면 안내를 찍고 로컬 검색으로 즉시 폴백한다
    # (graphrag_cli._cmd_query_global). communities build를 한 번도 안 했으니 기본 dirty=True다.
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    monkeypatch.setattr("query.vector_manager.query_similar", lambda q, top_k=8, collections=None: [])
    monkeypatch.setattr(query, "generate", lambda prompt: "로컬 폴백 답변")

    graphrag_cli.main(["query", "아무 질문", "--mode", "global", "--collection", C1])

    out = capsys.readouterr().out
    assert "재빌드" in out
    assert "로컬 폴백 답변" in out


# --- 실 Ollama 스모크(옵트인 격리) ---
# conftest.py가 이름에 real_ollama가 든 테스트를 GRAG_RUN_LLM_SMOKE=1일 때만 실행하도록 막는다
# (기본 pytest 실행에서는 네트워크/실서비스 호출 없이 빠르게 끝나야 한다는 조직 원칙 — community_reporter와 동형).


def _ollama_reachable() -> bool:
    try:
        requests.get(f"{settings.ollama_base_url}/api/tags", timeout=1.0)
        return True
    except requests.exceptions.RequestException:
        return False


@pytest.mark.skipif(not _ollama_reachable(), reason="Ollama가 로컬에서 응답하지 않음(미기동)")
def test_answer_question_global_real_ollama_smoke():
    # 실제 Ollama로 MAP+REDUCE를 모두 태워, 글로벌 검색이 실제로 종합 답변을 만들어내는지 확인한다.
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    _seed_report(
        C1, "leaf1", 0, "홍길동과 활빈당",
        "홍길동은 조선시대의 의적으로 활빈당을 이끌었고, 가난한 백성을 도왔다.",
    )
    _seed_report(
        C1, "leaf2", 0, "이순신과 거북선",
        "이순신은 임진왜란 때 거북선을 이용해 왜군을 물리친 조선의 명장이다.",
    )

    answer = query.answer_question_global("이 자료 전체에서 다뤄지는 주요 인물은 누구야?", collections=[C1])

    assert answer and answer not in (query._NO_REPORTS_MESSAGE, query._NO_RELEVANT_MESSAGE)
    print(f"\n[Ollama 글로벌 검색 샘플 답변] {answer!r}")
