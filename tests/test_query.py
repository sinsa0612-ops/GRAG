# query.answer_question이 그래프/벡터 컨텍스트를 올바르게 모아 프롬프트에 담는지 확인한다.
import query
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
