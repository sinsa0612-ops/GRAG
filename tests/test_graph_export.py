# graph_export.build_gexf가 엔티티/관계로부터 Gephi가 읽을 수 있는 유효한 GEXF를 만드는지 확인한다.
import xml.etree.ElementTree as ET

from graph_export import build_gexf

_GEXF_NS = "http://gexf.net/1.3"


# 한글 노드/엣지가 GEXF에 정확히 들어가고, well-formed XML로 파싱되는지 확인한다.
def test_build_gexf_includes_all_nodes_and_edges():
    entities = [
        {"collection": "c1", "name": "강택리", "type": "Person", "description": "기획자 & 작가"},
        {"collection": "c1", "name": "ISA계좌", "type": "Asset", "description": "절세용 계좌"},
    ]
    relations = [
        {
            "collection": "c1",
            "source": "강택리",
            "predicate": "MANAGES",
            "target": "ISA계좌",
            "valid_from": "2024-01",
            "source_doc": "doc_1",
        }
    ]

    gexf = build_gexf(entities, relations)
    root = ET.fromstring(gexf)  # 파싱되면 well-formed (& 같은 특수문자 이스케이프 검증 포함)

    nodes = root.findall(f".//{{{_GEXF_NS}}}node")
    edges = root.findall(f".//{{{_GEXF_NS}}}edge")
    # 노드 라벨은 이름, 내부 id는 (컬렉션::이름) 합성.
    assert {n.get("label") for n in nodes} == {"강택리", "ISA계좌"}
    assert {n.get("id") for n in nodes} == {"c1::강택리", "c1::ISA계좌"}
    assert len(edges) == 1
    assert edges[0].get("label") == "MANAGES"
    assert edges[0].get("source") == "c1::강택리"


# 같은 이름이라도 컬렉션이 다르면 GEXF에서 별개 노드(다른 id)로 나와야 한다.
def test_build_gexf_separates_same_name_across_collections():
    entities = [
        {"collection": "사업A", "name": "김부장", "type": "PERSON", "description": ""},
        {"collection": "사업B", "name": "김부장", "type": "PERSON", "description": ""},
    ]
    gexf = build_gexf(entities, [])
    root = ET.fromstring(gexf)
    assert {n.get("id") for n in root.findall(f".//{{{_GEXF_NS}}}node")} == {
        "사업A::김부장",
        "사업B::김부장",
    }


# 끝점 노드가 없는 깨진 관계는 엣지로 쓰지 않고 조용히 걸러내는지 확인한다.
def test_build_gexf_skips_dangling_edges():
    entities = [{"collection": "c1", "name": "강택리", "type": "Person", "description": ""}]
    relations = [
        {
            "collection": "c1",
            "source": "강택리",
            "predicate": "KNOWS",
            "target": "없는노드",
            "valid_from": "",
            "source_doc": "",
        }
    ]

    gexf = build_gexf(entities, relations)
    root = ET.fromstring(gexf)

    assert len(root.findall(f".//{{{_GEXF_NS}}}edge")) == 0


# 빈 그래프여도 예외 없이 유효한 GEXF 골격을 만드는지 확인한다.
def test_build_gexf_handles_empty_graph():
    gexf = build_gexf([], [])
    root = ET.fromstring(gexf)
    assert root.tag == f"{{{_GEXF_NS}}}gexf"
