# 커뮤니티 리포트 생성(community_reporter, M3)을 mock 백엔드로 단위검증한다:
# (a) 멤버→프롬프트 구성, (b) bottom-up 순서(자식 리포트가 부모 재료로 쓰임),
# (c) 레벨→백엔드 라우팅(하위=ollama/최상위=claude_cli), (d) 파싱.
# generate는 항상 mock으로 차단해 네트워크 없이 1초 내 종료된다. 실 Ollama/CLI 통합은 파일 끝의
# skipif 스모크 테스트로 격리(desc_summarizer/local_llm_adapter와 동형의 원칙).
import json
import shutil

import pytest
import requests

import pipeline.community_reporter as community_reporter
from config import settings
from db import graph_manager, sqlite_manager

C = "c1"


# --- (a) 멤버→프롬프트 구성 ---


def test_build_leaf_prompt_includes_member_names_descriptions_and_relations():
    entities = [{"name": "A", "description": "A 설명"}, {"name": "B", "description": "B 설명"}]
    relations = [{"source": "A", "target": "B", "predicate": "RELATED_TO"}]

    prompt = community_reporter._build_leaf_prompt(entities, relations)

    assert "A" in prompt and "A 설명" in prompt
    assert "B" in prompt and "B 설명" in prompt
    assert "RELATED_TO" in prompt


def test_build_leaf_prompt_handles_no_relations():
    entities = [{"name": "A", "description": ""}]

    prompt = community_reporter._build_leaf_prompt(entities, [])

    assert "A" in prompt
    assert "(관계 없음)" in prompt
    assert "(바깥으로 이어지는 관계 없음)" in prompt  # 외부 연결 미지정 시 안내


def test_build_leaf_prompt_includes_external_links():
    # 커뮤니티 경계를 넘는 관계(외부 연결)가 프롬프트에 담겨야 글로벌 검색이 그룹 간 연결을 답할 수 있다.
    entities = [{"name": "A", "description": "내부"}]
    internal = [{"source": "A", "target": "A", "predicate": "SELF"}]
    external = [{"source": "A", "target": "Z(외부그룹)", "predicate": "MEETS"}]

    prompt = community_reporter._build_leaf_prompt(entities, internal, external)

    assert "Z(외부그룹)" in prompt and "MEETS" in prompt
    assert "다른 그룹으로 이어지는 관계" in prompt


def test_build_parent_prompt_includes_child_titles_and_summaries():
    child_reports = [
        {"title": "AB묶음", "summary": "A와 B의 묶음"},
        {"title": "CD묶음", "summary": "C와 D의 묶음"},
    ]

    prompt = community_reporter._build_parent_prompt(child_reports)

    assert "AB묶음" in prompt and "A와 B의 묶음" in prompt
    assert "CD묶음" in prompt and "C와 D의 묶음" in prompt


# --- (d) 파싱 ---


def test_parse_report_strips_code_fence_and_parses_json():
    raw = '```json\n{"title": "제목", "summary": "요약", "rating": 7}\n```'

    result = community_reporter._parse_report(raw)

    assert result == {"title": "제목", "summary": "요약", "rating": 7.0}


def test_parse_report_missing_rating_defaults_to_none():
    result = community_reporter._parse_report('{"title": "제목", "summary": "요약"}')
    assert result["rating"] is None


def test_parse_report_non_numeric_rating_defaults_to_none():
    result = community_reporter._parse_report('{"title": "제목", "summary": "요약", "rating": "모름"}')
    assert result["rating"] is None


def test_parse_report_missing_title_raises():
    with pytest.raises(ValueError):
        community_reporter._parse_report('{"summary": "요약만 있음"}')


def test_parse_report_invalid_json_raises():
    with pytest.raises(json.JSONDecodeError):
        community_reporter._parse_report("이건 JSON이 아님")


# --- (c) 레벨→백엔드 라우팅 ---


def test_backend_for_level_routes_top_vs_bulk_by_default():
    # 기본 report_cli_top_levels=1 -> 레벨 0만 top(claude_cli), 레벨 1 이상은 bulk(ollama).
    assert community_reporter._backend_for_level(0) == settings.community_report_top_backend
    assert community_reporter._backend_for_level(1) == settings.community_report_bulk_backend
    assert community_reporter._backend_for_level(2) == settings.community_report_bulk_backend


def test_backend_for_level_respects_config_override(monkeypatch):
    monkeypatch.setattr(settings, "report_cli_top_levels", 2)

    assert community_reporter._backend_for_level(1) == settings.community_report_top_backend
    assert community_reporter._backend_for_level(2) == settings.community_report_bulk_backend


# --- (b) bottom-up 순서 + 저장 ---


def test_generate_reports_no_communities_returns_zero_without_calling_llm(monkeypatch):
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    calls = []
    monkeypatch.setattr(community_reporter, "generate", lambda *a, **k: calls.append(1) or "무시됨")

    assert community_reporter.generate_reports(C) == 0
    assert calls == []


def test_generate_reports_bottom_up_uses_child_reports_as_material(monkeypatch):
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    for name in ["A", "B", "C_", "D"]:
        graph_manager.upsert_entity(C, name, "OTHER", f"{name} 설명")
    graph_manager.upsert_relation(C, "A", "B", "RELATED_TO", "", "doc1")
    graph_manager.upsert_relation(C, "C_", "D", "RELATED_TO", "", "doc1")
    sqlite_manager.replace_communities(
        C,
        [
            {
                "community_id": "root", "level": 0, "parent_community_id": None,
                "entity_names": ["A", "B", "C_", "D"], "size": 4, "graph_signature": "sig",
            },
            {
                "community_id": "child1", "level": 1, "parent_community_id": "root",
                "entity_names": ["A", "B"], "size": 2, "graph_signature": "sig",
            },
            {
                "community_id": "child2", "level": 1, "parent_community_id": "root",
                "entity_names": ["C_", "D"], "size": 2, "graph_signature": "sig",
            },
        ],
    )

    calls = []

    def fake_generate(prompt, backend=None, model=None):
        calls.append({"prompt": prompt, "backend": backend})
        if len(calls) == 1:
            return json.dumps({"title": "AB묶음", "summary": "A와 B의 묶음", "rating": 3})
        if len(calls) == 2:
            return json.dumps({"title": "CD묶음", "summary": "C와 D의 묶음", "rating": 4})
        # 3번째 호출 = 부모(root) — 앞선 두 자식 리포트의 title/summary가 프롬프트 재료로 들어와야 한다.
        assert "AB묶음" in prompt and "A와 B의 묶음" in prompt
        assert "CD묶음" in prompt and "C와 D의 묶음" in prompt
        return json.dumps({"title": "전체", "summary": "AB묶음+CD묶음 종합", "rating": 5})

    monkeypatch.setattr(community_reporter, "generate", fake_generate)

    n = community_reporter.generate_reports(C)

    assert n == 3
    # 자식(레벨1, 2개) 먼저 -> 둘 다 bulk(ollama), 부모(레벨0) 나중 -> top(claude_cli).
    assert [c["backend"] for c in calls] == ["ollama", "ollama", "claude_cli"]
    assert "A" in calls[0]["prompt"] and "B" in calls[0]["prompt"]
    assert "C_" in calls[1]["prompt"] and "D" in calls[1]["prompt"]

    stored_root = sqlite_manager.get_community_report(C, "root")
    assert stored_root["title"] == "전체"
    assert stored_root["level"] == 0
    stored_child = sqlite_manager.get_community_report(C, "child1")
    assert stored_child["title"] == "AB묶음"
    assert stored_child["level"] == 1


def test_generate_reports_skips_failed_community_but_continues_others(monkeypatch):
    # 한 커뮤니티 리포트 생성이 실패해도(LLM 오류) 다른 커뮤니티 처리를 막지 않는다(장애 격리).
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "실패엔티티", "OTHER", "")
    graph_manager.upsert_entity(C, "성공엔티티", "OTHER", "")
    sqlite_manager.replace_communities(
        C,
        [
            {
                "community_id": "실패", "level": 0, "parent_community_id": None,
                "entity_names": ["실패엔티티"], "size": 1, "graph_signature": "sig",
            },
            {
                "community_id": "성공", "level": 0, "parent_community_id": None,
                "entity_names": ["성공엔티티"], "size": 1, "graph_signature": "sig",
            },
        ],
    )

    def flaky_generate(prompt, backend=None, model=None):
        if "실패엔티티" in prompt:
            raise RuntimeError("백엔드 연결 실패")
        return json.dumps({"title": "성공 제목", "summary": "성공 요약", "rating": 1})

    monkeypatch.setattr(community_reporter, "generate", flaky_generate)

    n = community_reporter.generate_reports(C)

    assert n == 1
    assert sqlite_manager.get_community_report(C, "실패") is None
    assert sqlite_manager.get_community_report(C, "성공")["title"] == "성공 제목"


def test_generate_reports_parent_skipped_when_all_children_fail(monkeypatch):
    # 자식 리포트가 전부 실패하면 재료가 없으므로 부모도 건너뛴다(부모 LLM 호출 자체를 하지 않음).
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "A", "OTHER", "")
    sqlite_manager.replace_communities(
        C,
        [
            {
                "community_id": "root", "level": 0, "parent_community_id": None,
                "entity_names": ["A"], "size": 1, "graph_signature": "sig",
            },
            {
                "community_id": "child1", "level": 1, "parent_community_id": "root",
                "entity_names": ["A"], "size": 1, "graph_signature": "sig",
            },
        ],
    )

    calls = []

    def always_fail(prompt, backend=None, model=None):
        calls.append(prompt)
        raise RuntimeError("항상 실패")

    monkeypatch.setattr(community_reporter, "generate", always_fail)

    n = community_reporter.generate_reports(C)

    assert n == 0
    assert len(calls) == 1  # 자식(child1) 시도만 있고, 부모(root)는 재료가 없어 호출 자체가 없어야 함


def test_generate_reports_replaces_stale_reports_from_previous_run(monkeypatch):
    # 재빌드 시 이전 리포트를 통째로 지우고 새로 쓴다(M3는 전체 재생성 — 낡은 community_id의 유령 리포트 방지).
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    sqlite_manager.upsert_community_report(C, "옛날커뮤니티", 0, "옛제목", "옛요약", None)
    graph_manager.upsert_entity(C, "A", "OTHER", "")
    sqlite_manager.replace_communities(
        C,
        [
            {
                "community_id": "새커뮤니티", "level": 0, "parent_community_id": None,
                "entity_names": ["A"], "size": 1, "graph_signature": "sig",
            },
        ],
    )
    monkeypatch.setattr(
        community_reporter, "generate",
        lambda *a, **k: json.dumps({"title": "새제목", "summary": "새요약", "rating": 1}),
    )

    community_reporter.generate_reports(C)

    assert sqlite_manager.get_community_report(C, "옛날커뮤니티") is None
    assert sqlite_manager.get_community_report(C, "새커뮤니티")["title"] == "새제목"


# --- 실 백엔드 스모크(격리) ---
# 실 Ollama/CLI가 로컬에서 응답할 때만 도는 통합 테스트 — 없으면 스킵(네트워크 없이 CI 통과 원칙 준수).
# 통과하면 stdout에 title/summary가 찍히므로(-s 옵션), 결과 문서에 샘플 리포트로 붙일 수 있다.


def _ollama_reachable() -> bool:
    try:
        requests.get(f"{settings.ollama_base_url}/api/tags", timeout=1.0)
        return True
    except requests.exceptions.RequestException:
        return False


@pytest.mark.skipif(not _ollama_reachable(), reason="Ollama가 로컬에서 응답하지 않음(미기동)")
def test_generate_reports_against_real_ollama_smoke(monkeypatch):
    # 모든 레벨을 bulk 백엔드(ollama)로 강제해, Ollama만으로 실제 리포트가 생성되는지 확인한다.
    monkeypatch.setattr(settings, "report_cli_top_levels", 0)
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "홍길동", "Person", "조선시대의 의적으로 활빈당을 이끌었다.")
    graph_manager.upsert_entity(C, "활빈당", "Organization", "가난한 백성을 돕던 의적 단체.")
    graph_manager.upsert_relation(C, "홍길동", "활빈당", "LEADS", "", "doc1")
    sqlite_manager.replace_communities(
        C,
        [
            {
                "community_id": "leaf1", "level": 0, "parent_community_id": None,
                "entity_names": ["홍길동", "활빈당"], "size": 2, "graph_signature": "sig",
            },
        ],
    )

    n = community_reporter.generate_reports(C)

    assert n == 1
    report = sqlite_manager.get_community_report(C, "leaf1")
    assert report["title"] and report["summary"]
    print(f"\n[Ollama 샘플 리포트] title={report['title']!r}")
    print(f"[Ollama 샘플 리포트] summary={report['summary']!r}")
    print(f"[Ollama 샘플 리포트] rating={report['rating']!r}")


@pytest.mark.skipif(shutil.which(settings.claude_cli_path) is None, reason="claude CLI가 PATH에 없음")
def test_generate_reports_against_real_claude_cli_smoke():
    # 기본 설정(report_cli_top_levels=1)이면 레벨 0은 top 백엔드(claude_cli)로 라우팅된다.
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "세종대왕", "Person", "훈민정음을 창제한 조선의 왕.")
    graph_manager.upsert_entity(C, "훈민정음", "Concept", "한글을 창제 당시 이르던 이름.")
    graph_manager.upsert_relation(C, "세종대왕", "훈민정음", "CREATED", "", "doc1")
    sqlite_manager.replace_communities(
        C,
        [
            {
                "community_id": "leaf1", "level": 0, "parent_community_id": None,
                "entity_names": ["세종대왕", "훈민정음"], "size": 2, "graph_signature": "sig",
            },
        ],
    )

    n = community_reporter.generate_reports(C)

    assert n == 1
    report = sqlite_manager.get_community_report(C, "leaf1")
    assert report["title"] and report["summary"]
    print(f"\n[Claude CLI 샘플 리포트] title={report['title']!r}")
    print(f"[Claude CLI 샘플 리포트] summary={report['summary']!r}")
    print(f"[Claude CLI 샘플 리포트] rating={report['rating']!r}")


# --- [M5] 증분 재계산: content_signature 기반 재사용 ---


def test_generate_reports_reuses_unchanged_reports_on_rebuild(monkeypatch):
    # 그래프가 그대로면 재빌드 시 LLM 재요약을 하지 않고(추가 콜 0) 기존 리포트를 그대로 재사용한다.
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    graph_manager.upsert_entity(C, "A", "OTHER", "A 설명")
    graph_manager.upsert_entity(C, "B", "OTHER", "B 설명")
    graph_manager.upsert_relation(C, "A", "B", "RELATED_TO", "", "doc1")
    sqlite_manager.replace_communities(
        C,
        [{"community_id": "c1", "level": 0, "parent_community_id": None,
          "entity_names": ["A", "B"], "size": 2, "graph_signature": "sig"}],
    )
    calls = []
    monkeypatch.setattr(
        community_reporter, "generate",
        lambda prompt, backend=None, model=None: calls.append(1) or json.dumps({"title": "T", "summary": "S", "rating": 1}),
    )

    community_reporter.generate_reports(C)
    assert len(calls) == 1  # 첫 빌드 = 1콜
    first = sqlite_manager.get_community_report(C, "c1")
    assert first["content_signature"]  # 시그니처가 저장됨

    community_reporter.generate_reports(C)  # 그래프 불변 상태로 재빌드
    assert len(calls) == 1  # 재사용 → 추가 콜 없음
    second = sqlite_manager.get_community_report(C, "c1")
    assert (second["title"], second["summary"]) == (first["title"], first["summary"])
    assert second["content_signature"] == first["content_signature"]


def test_generate_reports_regenerates_only_changed_community(monkeypatch):
    # 한 커뮤니티 멤버의 '설명'만 바뀌어도(멤버십 불변) 그 커뮤니티만 재생성되고 나머지는 재사용된다.
    # (community_id는 이름만 해시하므로 못 잡는 변화 — content_signature가 잡아야 정확하다.)
    graph_manager.init_schema()
    sqlite_manager.init_schema()
    for name in ["A", "B", "X", "Y"]:
        graph_manager.upsert_entity(C, name, "OTHER", f"{name} 설명")
    graph_manager.upsert_relation(C, "A", "B", "RELATED_TO", "", "doc1")
    graph_manager.upsert_relation(C, "X", "Y", "RELATED_TO", "", "doc1")
    sqlite_manager.replace_communities(
        C,
        [
            {"community_id": "cAB", "level": 0, "parent_community_id": None,
             "entity_names": ["A", "B"], "size": 2, "graph_signature": "s"},
            {"community_id": "cXY", "level": 0, "parent_community_id": None,
             "entity_names": ["X", "Y"], "size": 2, "graph_signature": "s"},
        ],
    )
    calls = []
    monkeypatch.setattr(
        community_reporter, "generate",
        lambda prompt, backend=None, model=None: calls.append(prompt) or json.dumps({"title": "T", "summary": "S", "rating": 1}),
    )

    community_reporter.generate_reports(C)
    assert len(calls) == 2  # 첫 빌드 = 2 커뮤니티 = 2콜
    calls.clear()

    graph_manager.upsert_entity(C, "A", "OTHER", "A 설명 변경됨")  # cAB 멤버의 설명만 변경
    community_reporter.generate_reports(C)
    assert len(calls) == 1  # cAB만 재생성(cXY는 재사용)
    assert "A 설명 변경됨" in calls[0]  # 재생성된 프롬프트가 cAB의 것
