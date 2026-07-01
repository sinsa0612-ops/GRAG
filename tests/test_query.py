# query.answer_question이 그래프/벡터 컨텍스트를 올바르게 모아 프롬프트에 담는지 확인한다.
import query
from config import settings
from db import graph_manager, sqlite_manager


def test_gather_graph_context_includes_bidirectional_relations():
    graph_manager.init_schema()
    graph_manager.upsert_entity("c1", "강택리", "Person", "기획자")
    graph_manager.upsert_entity("c1", "ISA계좌", "Asset", "절세용 계좌")
    graph_manager.upsert_entity("c1", "말리", "Person", "옛 동업자")
    graph_manager.upsert_relation("c1", "강택리", "ISA계좌", "MANAGES", "2026-01", "doc1")
    graph_manager.upsert_relation("c1", "말리", "강택리", "FORMER_BUSINESS_PARTNER_OF", "", "doc1")

    context = query._gather_graph_context("강택리는 누구야?")

    assert "강택리" in context
    assert "ISA계좌" in context
    assert "말리" in context
    assert "MANAGES" in context
    assert "FORMER_BUSINESS_PARTNER_OF" in context


def test_gather_graph_context_returns_placeholder_when_no_match():
    graph_manager.init_schema()
    graph_manager.upsert_entity("c1", "강택리", "Person", "기획자")

    context = query._gather_graph_context("이 이야기의 메인 테마는 무엇인가?")

    assert "찾지 못함" in context


def test_gather_graph_context_surfaces_bridged_spanning():
    # 브릿지로 연결된 같은 대상이 여러 사업에 걸치면, 종합(--all) 질의에서 '걸침'과 양쪽 관계가 함께 보여야 한다.
    graph_manager.init_schema()
    graph_manager.upsert_entity("사업A", "김변호사", "Person", "법률 자문")
    graph_manager.upsert_entity("사업B", "김변호사", "Person", "투자 자문")
    graph_manager.upsert_entity("사업A", "ISA계좌", "Asset", "")
    graph_manager.upsert_entity("사업B", "B펀드", "Asset", "")
    graph_manager.upsert_relation("사업A", "김변호사", "ISA계좌", "MANAGES", "", "docA")
    graph_manager.upsert_relation("사업B", "김변호사", "B펀드", "ADVISES", "", "docB")
    graph_manager.add_bridge("사업A", "김변호사", "사업B", "김변호사")

    context = query._gather_graph_context("김변호사는 어떤 일을 해?")

    assert "걸쳐 있습니다" in context
    assert "MANAGES" in context
    assert "ADVISES" in context


def test_gather_graph_context_bridge_respects_scope():
    # 스코프를 사업A로 좁히면, 사업B로의 걸침/관계는 끌어오지 않는다(격벽 존중).
    graph_manager.init_schema()
    graph_manager.upsert_entity("사업A", "김변호사", "Person", "법률 자문")
    graph_manager.upsert_entity("사업B", "김변호사", "Person", "투자 자문")
    graph_manager.upsert_entity("사업B", "B펀드", "Asset", "")
    graph_manager.upsert_relation("사업B", "김변호사", "B펀드", "ADVISES", "", "docB")
    graph_manager.add_bridge("사업A", "김변호사", "사업B", "김변호사")

    context = query._gather_graph_context("김변호사는 어떤 일을 해?", collections=["사업A"])

    assert "걸쳐 있습니다" not in context
    assert "B펀드" not in context


def test_gather_graph_context_matches_entities_in_chunks():
    # 질문엔 이름이 없어도, 벡터로 찾은 본문 조각(extra_text)에 등장한 엔티티는 그래프로 끌어와야 한다(벡터→그래프 브릿지).
    graph_manager.init_schema()
    graph_manager.upsert_entity("c1", "강택리", "Person", "기획자")
    graph_manager.upsert_entity("c1", "ISA계좌", "Asset", "절세용 계좌")
    graph_manager.upsert_relation("c1", "강택리", "ISA계좌", "MANAGES", "", "doc1")

    context = query._gather_graph_context(
        "이 사람은 무슨 일을 해?", extra_text="본문에 강택리가 ISA계좌를 관리한다고 나온다."
    )

    assert "강택리" in context
    assert "MANAGES" in context


def test_gather_graph_context_ignores_single_char_names_in_chunks():
    # 한 글자 엔티티 이름은 본문 substring 매칭에서 오탐이 커서 제외한다(질문 직접 매칭에는 이 제한이 없음).
    graph_manager.init_schema()
    graph_manager.upsert_entity("c1", "A", "Org", "한 글자 이름")

    context = query._gather_graph_context("설명해줘", extra_text="여기 A 라는 글자가 있다.")

    assert "찾지 못함" in context


def test_answer_question_uses_configured_top_k_and_bridges_chunk_entities(monkeypatch):
    # top_k 미지정 시 설정값을 쓰고, 질문에 이름이 없어도 본문 조각의 엔티티 관계가 프롬프트에 담긴다.
    graph_manager.init_schema()
    sqlite_manager.init_schema()  # 답변 시 사용량 기록(api_usage)을 위해
    graph_manager.upsert_entity("c1", "강택리", "Person", "기획자")
    graph_manager.upsert_entity("c1", "ISA계좌", "Asset", "절세용 계좌")
    graph_manager.upsert_relation("c1", "강택리", "ISA계좌", "MANAGES", "", "doc1")

    captured = {}

    def fake_query_similar(q, top_k=8, collections=None):
        captured["top_k"] = top_k
        return ["강택리가 ISA계좌를 관리한다"]

    monkeypatch.setattr("query.vector_manager.query_similar", fake_query_similar)

    captured_prompts = []
    monkeypatch.setattr(query, "generate", lambda prompt: captured_prompts.append(prompt) or "답변")

    result = query.answer_question("이 사람은 무슨 일을 하나?")  # 질문에 엔티티 이름이 직접 없음

    assert result == "답변"
    assert captured["top_k"] == settings.retrieval_top_k  # 하드코딩 8이 아니라 설정값 사용
    assert "MANAGES" in captured_prompts[0]  # 본문 조각 → 그래프 브릿지로 관계가 표면화됨


def test_answer_question_uses_only_provided_context(monkeypatch):
    graph_manager.init_schema()
    sqlite_manager.init_schema()  # 답변 시 사용량 기록(api_usage)을 위해
    graph_manager.upsert_entity("c1", "강택리", "Person", "기획자")

    monkeypatch.setattr(
        "query.vector_manager.query_similar",
        lambda q, top_k=8, collections=None: ["관련 본문 일부"],
    )

    captured_prompts = []

    def capturing_generate(prompt):
        captured_prompts.append(prompt)
        return "답변"

    monkeypatch.setattr(query, "generate", capturing_generate)

    result = query.answer_question("강택리는 누구야?")

    assert result == "답변"
    assert "강택리" in captured_prompts[0]
    assert "관련 본문 일부" in captured_prompts[0]
