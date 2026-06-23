# graph_viz.build_graph_html이 엔티티/관계로부터 유효한 인터랙티브 그래프 HTML을 만드는지 확인한다.
import json

from graph_viz import build_graph_html


def test_build_graph_html_includes_all_nodes_and_edges():
    entities = [
        {"name": "강택리", "type": "Person", "description": "기획자"},
        {"name": "ISA계좌", "type": "Asset", "description": "절세용 계좌"},
    ]
    relations = [
        {"source": "강택리", "predicate": "MANAGES", "target": "ISA계좌", "valid_from": "", "source_doc": ""}
    ]

    html = build_graph_html(entities, relations)

    # pyvis는 JSON을 ensure_ascii로 직렬화해서 한글이 \uXXXX로 이스케이프된다 (브라우저에서는 정상 동작 — 실제 검증 완료).
    assert json.dumps("강택리")[1:-1] in html
    assert json.dumps("ISA계좌")[1:-1] in html
    assert "MANAGES" in html
    assert "vis-network" in html.lower() or "vis.js" in html.lower()


def test_build_graph_html_handles_empty_graph():
    html = build_graph_html([], [])
    assert "vis-network" in html.lower() or "vis.js" in html.lower()
