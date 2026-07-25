# 글로벌(map-reduce) 검색(query.answer_question_global, M4)을 mock 백엔드로 단위검증한다.
# [글로벌 검색 재설계] 관문을 LLM 채점에서 결정적 임베딩 랭킹으로, MAP을 "채점"에서 "증거추출"로,
# 복합질문을 코드 결정적 분해로 바꿨다 — 이 파일도 새 흐름(_decompose_question/_cosine_rank/
# _rank_reports/_select_top_k/_map_report_evidence/_collect_evidence/_reduce_evidence)에 맞춰 갱신했다.
# generate·embed_texts는 항상 mock으로 차단해 네트워크 없이 1초 내 종료된다(실 Ollama 통합은 맨 끝
# skipif 스모크로 격리).
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


# 순서를 보존하는 가짜 임베딩 — 질의는 항상 영벡터(정규화 시 그대로 [0])라 모든 리포트와의 코사인
# 유사도가 0으로 동률이 되고, np.argsort는 안정 정렬이라 원래 리스트 순서가 그대로 유지된다.
# 랭킹 자체가 아니라 MAP/REDUCE/오케스트레이션을 검증하는 테스트에서 순서를 예측 가능하게 하려고 쓴다.
def _order_preserving_embed_texts(texts):
    return [[float(i)] for i, _ in enumerate(texts)]


# --- 순수 함수: 복합질문 분해(_decompose_question, LLM 미사용) ---


def test_decompose_splits_on_enumeration_markers():
    # 구분자가 4개 들어간 질문 → 원질문 포함 조각 분해 + 상한(기본 4) 절단까지 한 번에 확인한다.
    question = "카카오·네이버·쿠팡 및 배달의민족 그리고 토스의 실적은?"

    result = query._decompose_question(question)

    assert result[0] == question  # 원질문이 항상 첫 자리에 포함(안전판)
    assert len(result) == settings.global_search_max_subqueries  # 절단 확인(기본 4)
    assert "카카오" in result and "네이버" in result  # 조각 분해 확인
    assert "토스의 실적은?" not in result  # 상한을 넘는 조각은 잘려나감


def test_decompose_does_not_split_relational():
    # 쉼표·'와/과'는 화이트리스트에 없으므로 관계형 질문은 분해되지 않아야 한다(오분해 방지 회귀).
    question = "홍길동과 활빈당의 관계는 무엇인가?"

    result = query._decompose_question(question)

    assert result == [question]


def test_decompose_falls_back_to_original():
    # 구분자는 있지만 조각이 전부 길이<2라 무효 → dedup 결과가 자연히 원질문 1개로 남는다(자동 폴백).
    question = "가·나"

    result = query._decompose_question(question)

    assert result == [question]


# --- 순수 함수: 코사인 랭킹(_cosine_rank, 실 임베딩 없이 결정성 검증) ---


def test_cosine_rank_is_deterministic_and_orders_by_similarity():
    query_vec = [1.0, 0.0]
    report_vecs = [[0.0, 1.0], [1.0, 0.0], [-1.0, 0.0]]  # 직교/동일/반대 방향

    first = query._cosine_rank(query_vec, report_vecs)
    second = query._cosine_rank(query_vec, report_vecs)

    assert first == [1, 0, 2]  # 유사도 1(동일) > 0(직교) > -1(반대) 순
    assert first == second  # N회 호출해도 항상 같은 순서(결정성)


# --- 순수 함수: top-K 하한 강제(_select_top_k) ---


def test_select_top_k_enforces_floor(monkeypatch):
    monkeypatch.setattr(settings, "global_search_top_k", 1)
    monkeypatch.setattr(settings, "global_search_min_reports", 3)
    ranked_10 = [{"id": i} for i in range(10)]
    ranked_2 = [{"id": i} for i in range(2)]

    assert len(query._select_top_k(ranked_10)) == 3  # top_k=1이어도 하한 3까지는 뽑음
    assert len(query._select_top_k(ranked_2)) == 2  # 리포트가 하한보다 적으면 전량


# --- MAP: 증거추출(_map_report_evidence, generate 목킹) ---


def test_map_evidence_empty_points_treated_as_irrelevant(monkeypatch):
    report = {"collection": C1, "community_id": "c1", "title": "제목", "summary": "요약"}
    monkeypatch.setattr(query, "generate", lambda *a, **k: json.dumps({"evidence_points": []}))

    assert query._map_report_evidence(report, "질문?") == []


def test_map_evidence_skips_parse_failure_and_continues(monkeypatch):
    # 리포트 하나는 깨진 JSON을 주고, 다른 하나는 정상 응답을 준다 — 깨진 쪽만 스킵되고 계속 진행되는지
    # _collect_evidence 수준에서 확인한다(장애 격리).
    reports = [
        {"collection": C1, "community_id": "bad", "title": "깨진 응답", "summary": "요약"},
        {"collection": C1, "community_id": "good", "title": "정상 응답", "summary": "요약"},
    ]
    monkeypatch.setattr(query, "embed_texts", _order_preserving_embed_texts)

    def fake_generate(prompt, backend=None, temperature=None, format_json=False):
        if "깨진 응답" in prompt:
            return "이건 JSON이 아님"
        return json.dumps({"evidence_points": ["정상 근거"]})

    monkeypatch.setattr(query, "generate", fake_generate)

    bundles = query._collect_evidence(["질문?"], reports)

    assert len(bundles) == 1
    assert bundles[0]["title"] == "정상 응답"
    assert bundles[0]["points"] == ["정상 근거"]


# --- 서브질문별 독립 top-K(_collect_evidence) ---


def test_collect_evidence_ranks_per_subquery(monkeypatch):
    # top_k=1·min_reports=1로 좁혀, 서브질문마다 실제로 다른 리포트 1개씩만 뽑히는지 확인한다
    # (전역 K가 아니라 서브질문별 독립 랭킹 — A질문이 B리포트를 밀어내지 않아야 한다).
    monkeypatch.setattr(settings, "global_search_top_k", 1)
    monkeypatch.setattr(settings, "global_search_min_reports", 1)
    reports = [
        {"collection": C1, "community_id": "a", "title": "A주제 리포트", "summary": "A 내용"},
        {"collection": C1, "community_id": "b", "title": "B주제 리포트", "summary": "B 내용"},
        {"collection": C1, "community_id": "c", "title": "C주제 리포트", "summary": "C 내용"},
    ]

    # 텍스트에 등장하는 A/B/C 글자에 따라 서로 직교하는 벡터를 준다 — 질문 "A질문"은 A주제만 1등으로 뽑고,
    # "B질문"은 B주제만 1등으로 뽑도록 만든 결정적 가짜 임베딩.
    def fake_embed_texts(texts):
        vecs = []
        for t in texts:
            if "A" in t:
                vecs.append([1.0, 0.0, 0.0])
            elif "B" in t:
                vecs.append([0.0, 1.0, 0.0])
            else:
                vecs.append([0.0, 0.0, 1.0])
        return vecs

    monkeypatch.setattr(query, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(
        query, "generate", lambda *a, **k: json.dumps({"evidence_points": ["근거"]})
    )

    bundles = query._collect_evidence(["A질문", "B질문"], reports)

    titles_by_subquery = {b["subquery"]: b["title"] for b in bundles}
    assert titles_by_subquery["A질문"] == "A주제 리포트"
    assert titles_by_subquery["B질문"] == "B주제 리포트"
    assert len(bundles) == 2  # 서브질문마다 정확히 1개씩(top_k=1) — 서로 밀어내지 않음


# --- MAP이 하드코딩이 아니라 설정값을 그대로 쓰는지(temperature/format_json) ---


def test_map_uses_temperature_zero_and_format_json(monkeypatch):
    monkeypatch.setattr(settings, "global_search_map_temperature", 0.37)  # 하드코딩이면 이 값이 안 씀
    report = {"collection": C1, "community_id": "c1", "title": "제목", "summary": "요약"}
    captured = {}

    def fake_generate(prompt, backend=None, temperature=None, format_json=False):
        captured["temperature"] = temperature
        captured["format_json"] = format_json
        return json.dumps({"evidence_points": []})

    monkeypatch.setattr(query, "generate", fake_generate)

    query._map_report_evidence(report, "질문?")

    assert captured["temperature"] == 0.37 == settings.global_search_map_temperature
    assert captured["format_json"] is True


# --- REDUCE가 서브질문·출처 라벨을 보존하는지 ---


def test_reduce_preserves_subquery_and_source_labels(monkeypatch):
    bundles = [
        {
            "subquery": "매출은?",
            "collection": C1,
            "community_id": "comm-7",
            "title": "실적 리포트",
            "points": ["매출이 늘었다"],
        }
    ]
    captured = {}

    def fake_generate(prompt, backend=None):
        captured["prompt"] = prompt
        return "종합 답변"

    monkeypatch.setattr(query, "generate", fake_generate)

    result = query._reduce_evidence(bundles, "원래 질문?")

    assert result == "종합 답변"
    prompt = captured["prompt"]
    assert "매출은?" in prompt  # 서브질문 라벨 보존
    assert "comm-7" in prompt and "실적 리포트" in prompt  # 출처(community_id·title) 보존
    assert "매출이 늘었다" in prompt  # 근거 포인트 본문 보존


# --- 리포트 없음 / 전부 무관 ---


def test_global_no_reports(monkeypatch):
    sqlite_manager.init_schema()
    calls = []
    monkeypatch.setattr(query, "generate", lambda *a, **k: calls.append(1) or "무시됨")
    monkeypatch.setattr(query, "embed_texts", _order_preserving_embed_texts)

    result = query.answer_question_global("질문?", collections=["없는컬렉션"])

    assert result == query._NO_REPORTS_MESSAGE
    assert calls == []  # 리포트가 없으면 LLM을 아예 부르지 않는다


def test_global_all_irrelevant(monkeypatch):
    sqlite_manager.init_schema()
    _seed_report(C1, "a1", 0, "무관 리포트", "요약")
    calls = []

    def fake_generate(prompt, backend=None, temperature=None, format_json=False):
        calls.append(prompt)
        return json.dumps({"evidence_points": []})

    monkeypatch.setattr(query, "generate", fake_generate)
    monkeypatch.setattr(query, "embed_texts", _order_preserving_embed_texts)

    result = query.answer_question_global("질문?", collections=[C1])

    assert result == query._NO_RELEVANT_MESSAGE
    assert len(calls) == 1  # MAP 1콜만 있고 REDUCE 호출은 없어야 함(낭비 호출 방지)


# --- RPD 기록은 backend가 gemini로 해석될 때만 ---


def test_map_and_reduce_do_not_record_usage_with_default_ollama_backend(monkeypatch):
    sqlite_manager.init_schema()
    _seed_report(C1, "a1", 0, "리포트", "요약")
    monkeypatch.setattr(
        query, "generate",
        lambda *a, **k: json.dumps({"evidence_points": ["근거"]}) if "리포트 제목:" in a[0] else "답",
    )
    monkeypatch.setattr(query, "embed_texts", _order_preserving_embed_texts)

    query.answer_question_global("질문?", collections=[C1])

    assert sqlite_manager.get_api_usage_today() == 0  # 기본 backend=ollama는 무료라 미기록


def test_map_and_reduce_record_usage_when_backend_is_gemini(monkeypatch):
    sqlite_manager.init_schema()
    _seed_report(C1, "a1", 0, "리포트", "요약")
    monkeypatch.setattr(settings, "global_search_map_backend", "gemini")
    monkeypatch.setattr(settings, "global_search_reduce_backend", "gemini")
    monkeypatch.setattr(
        query, "generate",
        lambda *a, **k: json.dumps({"evidence_points": ["근거"]}) if "리포트 제목:" in a[0] else "답",
    )
    monkeypatch.setattr(query, "embed_texts", _order_preserving_embed_texts)

    query.answer_question_global("질문?", collections=[C1])

    # MAP 1콜(리포트 1개·서브질문 1개) + REDUCE 1콜 = 2건 기록되어야 한다.
    assert sqlite_manager.get_api_usage_today() == 2


# --- CLI --mode 배선: 신규 global + 기존 local 회귀(시그니처 불변이라 기존 유지, fake_generate만 새 MAP JSON에 맞게 수정) ---


def test_cli_query_mode_defaults_to_local_and_behaves_unchanged(monkeypatch, capsys):
    # --mode를 생략하면(기본 local) 기존과 동일하게 answer_question 경로로 간다 — M4 이전 회귀 확인.
    sqlite_manager.init_schema()
    graph_manager.init_schema()
    monkeypatch.setattr("query.vector_manager.query_similar", lambda q, top_k=8, collections=None: [])
    monkeypatch.setattr(query, "generate", lambda prompt, **kwargs: "로컬답변")

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
    monkeypatch.setattr(query, "embed_texts", _order_preserving_embed_texts)

    def fake_generate(prompt, backend=None, temperature=None, format_json=False):
        if "리포트 제목:" in prompt:  # MAP 프롬프트만 이 라벨을 가짐(REDUCE와 구분)
            return json.dumps({"evidence_points": ["부분 근거"]})
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
    monkeypatch.setattr(query, "embed_texts", _order_preserving_embed_texts)

    def fake_generate(prompt, backend=None, temperature=None, format_json=False):
        if "레벨0제목" in prompt:
            pytest.fail("--level 1을 줬는데 레벨0 리포트가 MAP에 들어옴")
        if "리포트 제목:" in prompt:
            return json.dumps({"evidence_points": ["부분 근거"]})
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
    monkeypatch.setattr(query, "generate", lambda prompt, **kwargs: "로컬 폴백 답변")

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
def test_answer_question_global_deterministic_non_empty_real_ollama():
    # 실제 Ollama+bge-m3로 MAP+REDUCE를 모두 태워, 서사·복합 질문을 3회 반복해도 매번 non-empty이고
    # 안내 문자열이 아닌 답을 내는지 확인한다(비결정 탈락 재현 방지 — 이번 신뢰성 수정의 핵심 성공 기준).
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

    for i in range(3):
        answer = query.answer_question_global(
            "홍길동 및 이순신은 각각 어떤 인물이야?", collections=[C1]
        )
        assert answer and answer not in (query._NO_REPORTS_MESSAGE, query._NO_RELEVANT_MESSAGE)
        print(f"\n[Ollama 글로벌 검색 {i + 1}회차 답변] {answer!r}")
