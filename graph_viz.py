# 그래프 엔티티/관계를 PyVis 인터랙티브 HTML로 변환하는 시각화 모듈.
from pyvis.network import Network

_PALETTE = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948", "#b07aa1", "#ff9da7"]


# 엔티티/관계 목록을 받아 드래그·줌이 되는 인터랙티브 그래프 HTML 문자열을 만든다.
# 같은 type끼리 같은 색으로 칠해서 한눈에 종류를 구분할 수 있게 한다.
def build_graph_html(entities: list[dict], relations: list[dict]) -> str:
    net = Network(height="650px", width="100%", directed=True, notebook=False)

    type_colors: dict[str, str] = {}
    for entity in entities:
        entity_type = entity["type"]
        if entity_type not in type_colors:
            type_colors[entity_type] = _PALETTE[len(type_colors) % len(_PALETTE)]
        net.add_node(
            entity["name"],
            label=entity["name"],
            title=f"[{entity['type']}] {entity['description']}",
            color=type_colors[entity_type],
        )

    for relation in relations:
        net.add_edge(relation["source"], relation["target"], label=relation["predicate"])

    return net.generate_html()
