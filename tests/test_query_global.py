# 글로벌(map-reduce) 검색(query.answer_question_global, M4)을 mock 백엔드로 단위검증한다.
# [글로벌 검색 재설계] 관문을 LLM 채점에서 결정적 임베딩 랭킹으로, MAP을 "채점"에서 "증거추출"로,
# 복합질문을 코드 결정적 분해로 바꿨다 — 이 파일도 새 흐름(_decompose_question/_cosine_rank/
# _rank_reports/_select_reports/_map_report_evidence/_collect_evidence/_reduce_evidence)에 맞춰 갱신했다.
# generate·embed_texts는 항상 mock으로 차단해 네트워크 없이 1초 내 종료된다(실 Ollama 통합은 맨 끝
# skipif 스모크로 격리).
import json
import logging
import math

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


# [D4 수리] 다차원 결정적 가짜 임베딩 — 옛 1차원 버전은 코사인 유사도가 부호만 남아 전부 동점이 되는
# 바람에 np.argsort(-similarities)를 np.argsort(similarities)로 뒤집어도 랭킹 테스트가 전부 통과했다
# (랭킹 로직이 사실상 검증되지 않음). 질의(index 0)는 [1.0, 0.0], 리포트 i(1-based, 총 n개)는
# [cos θ_i, sin θ_i](θ_i = i * (π / (2 * (n + 1))))를 받아 유사도(=cos θ_i)가 입력 순서대로
# 엄격히 감소한다(동률 없음) — "입력 순서 = 랭킹 순서"라는 기존 테스트들의 전제는 유지하면서, 정렬
# 방향을 뒤집으면 반환 순서가 실제로 뒤바뀌게 만든다. MAP/REDUCE/오케스트레이션을 검증하는 테스트에서
# 순서를 예측 가능하게 하려고 쓴다.
def _ranked_embed_texts(texts):
    n = len(texts) - 1
    vectors = [[1.0, 0.0]]
    for i in range(1, n + 1):
        theta = i * (math.pi / (2 * (n + 1)))
        vectors.append([math.cos(theta), math.sin(theta)])
    return vectors


# --- 순수 함수: 복합질문 분해(_decompose_question, LLM 미사용) ---


# [D3 보수화] 옛 test_decompose_splits_on_enumeration_markers는 삭제했다 — 새 규칙(조각이 전부
# "질문 형태"일 때만 분해)에서는 "카카오·네이버·쿠팡 및 배달의민족 그리고 토스의 실적은?"의 앞쪽 조각들이
# "?"로도 원질문의 마지막 어절로도 끝나지 않아 분해 자체가 통째로 포기되고 [원질문]만 남는다(이 파일
# 아래 test_decompose_keeps_middot_and_bare_keyword_intact·test_decompose_splits_genuine_multi_question이
# 그 대체 회귀선이다).


@pytest.mark.parametrize(
    "question",
    [
        "한·미 정상회담의 주요 의제는?",
        "가·나·다·라·마 사업의 예산은?",
        "김철수 및 박영희의 역할은?",
        "매출 및 영업이익 추이를 알려줘",
    ],
)
def test_decompose_keeps_middot_and_bare_keyword_intact(question):
    # '·'는 구분자에서 빠졌으므로(한·미처럼 복합명사 용도가 더 흔함) 무조건 원질문 그대로.
    # '및'이 있어도 조각(예: "김철수", "매출")이 "?"로도 원질문 마지막 어절로도 끝나지 않으면
    # 분해를 통째로 포기한다(all-or-nothing) — 없는 개념을 서브질문으로 만들지 않는다.
    assert query._decompose_question(question) == [question]


@pytest.mark.parametrize(
    "question, expected",
    [
        (
            "카카오의 실적은? 그리고 네이버의 전략은?",
            ["카카오의 실적은? 그리고 네이버의 전략은?", "카카오의 실적은?", "네이버의 전략은?"],
        ),
        (
            "카카오 실적을 알려줘 그리고 네이버 전략을 알려줘",
            [
                "카카오 실적을 알려줘 그리고 네이버 전략을 알려줘",
                "카카오 실적을 알려줘",
                "네이버 전략을 알려줘",
            ],
        ),
    ],
)
def test_decompose_splits_genuine_multi_question(question, expected):
    # 두 케이스 모두 조각이 전부 "?"로 끝나거나(1번) 원질문의 마지막 어절 "알려줘"로 끝나(2번)
    # 진짜 분해 가능한 복합질문으로 판정된다.
    assert query._decompose_question(question) == expected


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


def test_cosine_rank_ties_are_deterministic():
    # 완전 동일 벡터라 유사도가 전부 동률일 때도 매 호출 같은 순서가 나오는지 확인한다(동률에서만
    # 정렬 불안정성이 드러나므로, 이 케이스가 D4가 노렸던 "안정 정렬" 성질의 실제 회귀 방지선이다).
    query_vec = [1.0, 0.0]
    report_vecs = [[1.0, 0.0]] * 6

    results = [query._cosine_rank(query_vec, report_vecs) for _ in range(20)]

    assert all(r == results[0] for r in results)


# --- 순수 함수: 서브질문별 리포트 랭킹(_rank_reports, 다차원 가짜 임베딩으로 실제 정렬 검증) ---


def test_rank_reports_orders_by_similarity_and_excludes_query(monkeypatch):
    # _ranked_embed_texts는 "먼저 넣은 리포트일수록 유사도가 높다"는 성질을 갖는다 — 그 성질을 이용해
    # 사람이 읽을 땐 순위와 반대로(4위부터) 리스트를 구성해, _rank_reports가 실제로 유사도 내림차순
    # 정렬을 수행하는지(놉으로 두거나 뒤집으면 걸리는지) 확인한다.
    reports = [
        {"title": "4위 리포트", "summary": "네 번째로 유사"},
        {"title": "3위 리포트", "summary": "세 번째로 유사"},
        {"title": "2위 리포트", "summary": "두 번째로 유사"},
        {"title": "1위 리포트", "summary": "가장 유사"},
    ]
    monkeypatch.setattr(query, "embed_texts", _ranked_embed_texts)

    ranked = query._rank_reports("질문?", reports)

    assert [r["title"] for r in ranked] == ["4위 리포트", "3위 리포트", "2위 리포트", "1위 리포트"]
    assert len(ranked) == 4  # 질의 벡터(vectors[0])가 리포트로 새어들지 않음(vectors[1:] 슬라이싱)


# --- 순수 함수: 상대 기준 투입(_select_reports) ---


def _ranked(scores: list[float]) -> list[dict]:
    return [{"id": i, query._RANK_SCORE_KEY: s} for i, s in enumerate(scores)]


def test_select_reports_admits_by_relative_score(monkeypatch):
    monkeypatch.setattr(settings, "global_search_relative_ratio", 0.5)
    monkeypatch.setattr(settings, "global_search_min_reports", 1)
    monkeypatch.setattr(settings, "global_search_max_reports", 100)

    # 1위가 압도적인 '좁은 질문' 모양 — 기준(1.0*0.5=0.5) 미만은 잘린다.
    assert len(query._select_reports(_ranked([1.0, 0.9, 0.45, 0.2]))) == 2
    # 점수가 몰려 있는 '광역 질문' 모양 — 같은 규칙인데 폭이 저절로 넓어진다.
    assert len(query._select_reports(_ranked([0.5, 0.48, 0.46, 0.44]))) == 4


def test_select_reports_enforces_floor_and_cap(monkeypatch):
    monkeypatch.setattr(settings, "global_search_relative_ratio", 0.9)
    monkeypatch.setattr(settings, "global_search_min_reports", 3)
    monkeypatch.setattr(settings, "global_search_max_reports", 100)

    # 기준이 빡세도 하한만큼은 무조건 투입("재료 0" 방지). 리포트가 하한보다 적으면 전량.
    assert len(query._select_reports(_ranked([1.0, 0.2, 0.1, 0.05]))) == 3
    assert len(query._select_reports(_ranked([1.0, 0.2]))) == 2
    assert query._select_reports([]) == []

    # 상한은 기준을 통과한 것도 자른다(로컬 모델 폭주 방지).
    monkeypatch.setattr(settings, "global_search_relative_ratio", 0.1)
    monkeypatch.setattr(settings, "global_search_max_reports", 4)
    assert len(query._select_reports(_ranked([1.0] * 10))) == 4


# --- MAP: 증거추출(_map_report_evidence, generate 목킹) ---


def test_map_evidence_empty_points_treated_as_irrelevant(monkeypatch):
    # 여분 키("reasoning")가 붙어도 무해하게 무시되는지 함께 확인한다(오타 키와 달리 "여분"은 실패가
    # 아니다 — extra="forbid"를 안 쓰는 이유, MapEvidence 기대 동작 표 6행).
    report = {"collection": C1, "community_id": "c1", "title": "제목", "summary": "요약"}
    monkeypatch.setattr(
        query, "generate",
        lambda *a, **k: json.dumps({"evidence_points": [], "reasoning": "잡담"}),
    )

    assert query._map_report_evidence(report, "질문?") == []


# --- [D1 수리] 무진단 빈 답변 차단: 키 누락/옛 포맷/오타 키는 None + 경고 로그(빈 리스트와 구분) ---


def test_map_evidence_missing_key_is_failure_not_empty(monkeypatch, caplog):
    # evidence_points 키 자체가 없는 JSON({}) — 필수 필드 누락이라 ValidationError → None + 경고 1건.
    report = {"collection": C1, "community_id": "c1", "title": "제목", "summary": "요약"}
    monkeypatch.setattr(query, "generate", lambda *a, **k: json.dumps({}))

    with caplog.at_level(logging.WARNING, logger="query"):
        result = query._map_report_evidence(report, "질문?")

    assert result is None  # 빈 리스트([])가 아니라 실패임이 타입으로 드러난다
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"


@pytest.mark.parametrize(
    "raw_json",
    [
        json.dumps({"relevance": 9, "partial_answer": "정상적으로 보이는 옛 포맷 응답"}),  # 옛 포맷
        json.dumps({"evidencePoints": ["근거"]}),  # 오타 키(camelCase)
    ],
    ids=["old_relevance_format", "typo_key_evidencePoints"],
)
def test_map_evidence_old_format_is_failure(monkeypatch, caplog, raw_json):
    # 둘 다 "유효한 JSON"이지만 evidence_points 키가 없으므로 조용히 빈 리스트로 통과해선 안 되고,
    # None + 경고 1건으로 실패가 드러나야 한다(이번 결함군의 핵심 증상 — 전 리포트가 이 상태면 사용자는
    # "관련 내용을 찾지 못했습니다"만 보고 단서가 0이었다).
    report = {"collection": C1, "community_id": "c1", "title": "제목", "summary": "요약"}
    monkeypatch.setattr(query, "generate", lambda *a, **k: raw_json)

    with caplog.at_level(logging.WARNING, logger="query"):
        result = query._map_report_evidence(report, "질문?")

    assert result is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"


def test_map_evidence_skips_parse_failure_and_continues(monkeypatch):
    # 리포트 하나는 깨진 JSON을 주고, 다른 하나는 정상 응답을 준다 — 깨진 쪽만 실패로 집계되고(스킵)
    # 계속 진행되는지 _collect_evidence 수준에서 확인한다(장애 격리). [D1 수리] 반환형이 튜플이 됐으므로
    # (bundles, stats)로 언패킹하고, 실패 1건이 stats["failed"]에 잡히는지도 함께 확인한다.
    reports = [
        {"collection": C1, "community_id": "bad", "title": "깨진 응답", "summary": "요약"},
        {"collection": C1, "community_id": "good", "title": "정상 응답", "summary": "요약"},
    ]
    monkeypatch.setattr(query, "embed_texts", _ranked_embed_texts)

    def fake_generate(prompt, backend=None, temperature=None, format_json=False):
        if "깨진 응답" in prompt:
            return "이건 JSON이 아님"
        return json.dumps({"evidence_points": ["정상 근거"]})

    monkeypatch.setattr(query, "generate", fake_generate)

    bundles, stats = query._collect_evidence(["질문?"], reports)

    assert len(bundles) == 1
    assert bundles[0]["title"] == "정상 응답"
    assert bundles[0]["points"] == ["정상 근거"]
    assert stats == {"examined": 2, "calls": 2, "failed": 1}


# --- 서브질문별 독립 랭킹(_collect_evidence) ---


def test_collect_evidence_ranks_per_subquery(monkeypatch):
    # 기준을 최대로 좁혀(ratio=1.0·하한 1) 서브질문마다 1등 1개씩만 뽑히게 한 뒤, 서로 다른 리포트가
    # 나오는지 확인한다
    # (전역 K가 아니라 서브질문별 독립 랭킹 — A질문이 B리포트를 밀어내지 않아야 한다).
    monkeypatch.setattr(settings, "global_search_relative_ratio", 1.0)
    monkeypatch.setattr(settings, "global_search_min_reports", 1)
    monkeypatch.setattr(settings, "global_search_max_reports", 100)
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

    bundles, stats = query._collect_evidence(["A질문", "B질문"], reports)

    titles_by_subquery = {b["subquery"]: b["title"] for b in bundles}
    assert titles_by_subquery["A질문"] == "A주제 리포트"
    assert titles_by_subquery["B질문"] == "B주제 리포트"
    assert len(bundles) == 2  # 서브질문마다 정확히 1개씩(1등만) — 서로 밀어내지 않음
    assert stats["failed"] == 0  # 전부 정상 응답이라 실패 0건


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
    monkeypatch.setattr(query, "embed_texts", _ranked_embed_texts)

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
    monkeypatch.setattr(query, "embed_texts", _ranked_embed_texts)

    result = query.answer_question_global("질문?", collections=[C1])

    assert result == query._NO_RELEVANT_MESSAGE
    assert len(calls) == 1  # MAP 1콜만 있고 REDUCE 호출은 없어야 함(낭비 호출 방지)


# --- [D1 수리] MAP 실패가 사용자가 보는 반환 문자열에 각주로 드러나는지 ---


def test_global_answer_appends_map_failure_note(monkeypatch):
    # 리포트 2개 중 하나는 깨진 JSON(MAP 실패 1건), 하나는 정상 근거(성공 1건) → 총 MAP 2콜.
    # 반환 문자열에 "※ MAP 호출 1/2건이 실패했습니다"가 포함돼야 한다 — 로그만으로는 CLI 밖에서
    # 안 보이던 실패가 이제 사용자가 실제로 읽는 답변 표면에 드러난다는 증거.
    sqlite_manager.init_schema()
    _seed_report(C1, "good", 0, "정상 리포트", "요약")
    _seed_report(C1, "bad", 0, "깨진 리포트", "요약")
    monkeypatch.setattr(query, "embed_texts", _ranked_embed_texts)

    def fake_generate(prompt, backend=None, temperature=None, format_json=False):
        if "깨진 리포트" in prompt:
            return "이건 JSON이 아님"
        if "리포트 제목:" in prompt:  # MAP 프롬프트만 이 라벨을 가짐(REDUCE와 구분)
            return json.dumps({"evidence_points": ["근거"]})
        return "종합 답변"

    monkeypatch.setattr(query, "generate", fake_generate)

    result = query.answer_question_global("질문?", collections=[C1])

    assert result == "종합 답변 ※ MAP 호출 1/2건이 실패했습니다 — 로그를 확인하세요."


def test_global_answer_bytes_identical_when_no_map_failures(monkeypatch):
    # [바이트 동일 불변] MAP 실패가 0건이면 반환 문자열은 각주 없이 REDUCE 출력 그대로여야 한다 —
    # 기존 등가 단언 테스트들(test_global_all_irrelevant 등)을 깨지 않는다는 것을 문자열 그대로
    # 비교(in이 아니라 ==)해 직접 증명한다.
    sqlite_manager.init_schema()
    _seed_report(C1, "a1", 0, "리포트", "요약")
    monkeypatch.setattr(query, "embed_texts", _ranked_embed_texts)
    monkeypatch.setattr(
        query, "generate",
        lambda *a, **k: json.dumps({"evidence_points": ["근거"]}) if "리포트 제목:" in a[0] else "종합 답변",
    )

    result = query.answer_question_global("질문?", collections=[C1])

    assert result == "종합 답변"  # 각주 없음, 바이트 동일


# --- RPD 기록은 backend가 gemini로 해석될 때만 ---


def test_map_and_reduce_do_not_record_usage_with_default_ollama_backend(monkeypatch):
    sqlite_manager.init_schema()
    _seed_report(C1, "a1", 0, "리포트", "요약")
    monkeypatch.setattr(
        query, "generate",
        lambda *a, **k: json.dumps({"evidence_points": ["근거"]}) if "리포트 제목:" in a[0] else "답",
    )
    monkeypatch.setattr(query, "embed_texts", _ranked_embed_texts)

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
    monkeypatch.setattr(query, "embed_texts", _ranked_embed_texts)

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
    monkeypatch.setattr(query, "embed_texts", _ranked_embed_texts)

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
    monkeypatch.setattr(query, "embed_texts", _ranked_embed_texts)

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


# 실제 임베딩(bge-m3)으로 상대 기준의 핵심 성질을 고정한다 — 가짜 점수로는 증명되지 않는 부분.
# 성질: 같은 코퍼스·같은 규칙인데도 '좁은 질문'은 좁게, '광역 질문'은 넓게 투입된다.
# 옛 고정 K는 이 둘을 같은 폭으로 다뤄, 광역 질문에서 정작 답을 담은 리포트를 잘라냈다(실측 6~13위).
# conftest.py가 이름의 real_embedding을 보고 GRAG_RUN_LLM_SMOKE=1 일 때만 실행한다.
def test_relative_admission_widens_for_broad_questions_real_embedding():
    reports = [
        {"collection": C1, "community_id": "hong", "title": "홍길동과 활빈당",
         "summary": "홍길동은 조선시대의 서자 출신 의적으로 활빈당을 이끌고 탐관오리의 재물을 빼앗아 백성에게 나눠주었다."},
        {"collection": C1, "community_id": "bombom", "title": "봄봄의 데릴사위",
         "summary": "봄봄에서 '나'는 점순이와 혼인시켜 준다는 장인의 약속만 믿고 삼 년 넘게 머슴처럼 일한다."},
        {"collection": C1, "community_id": "dongbaek", "title": "동백꽃의 점순이",
         "summary": "동백꽃에서 점순이는 감자를 건네지만 거절당하자 우리 집 닭을 괴롭힌다."},
        {"collection": C1, "community_id": "kakao", "title": "카카오의 성장",
         "summary": "카카오는 김범수가 창업했고 다음커뮤니케이션과 합병했다."},
        {"collection": C1, "community_id": "weather", "title": "날씨 기록",
         "summary": "지난주 기온은 평년보다 높았고 주말에 비가 내렸다."},
        {"collection": C1, "community_id": "recipe", "title": "김치찌개 조리법",
         "summary": "돼지고기와 신김치를 볶다가 물을 붓고 두부와 대파를 넣어 끓인다."},
    ]

    narrow = query._select_reports(query._rank_reports("카카오는 누가 창업했어?", reports))
    broad = query._select_reports(query._rank_reports("각 소설의 주요 인물과 관계를 정리해줘", reports))

    # 좁은 질문은 정답이 1등이고 폭이 좁다.
    assert narrow[0]["community_id"] == "kakao"
    assert len(narrow) < len(reports)
    # 광역 질문은 세 소설을 모두 담아야 한다 — 고정 K였다면 하나가 잘려나가던 자리.
    assert {"hong", "bombom", "dongbaek"} <= {r["community_id"] for r in broad}
    assert len(broad) > len(narrow)
