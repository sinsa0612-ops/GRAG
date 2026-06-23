# 그래프 엔티티/관계를 Gephi에서 바로 열 수 있는 GEXF 파일로 내보내는 모듈.
# (웹 UI 대신 Gephi로 그래프를 보기 위한 경로 — PyVis HTML 시각화와 별개의 독립 출력기.)
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from config import settings
from db import graph_manager

logger = logging.getLogger(__name__)

# GEXF 1.3 표준 네임스페이스(그래프 본체)와 시각화 확장(viz — 노드 색상용).
_GEXF_NS = "http://gexf.net/1.3"
_VIZ_NS = "http://gexf.net/1.3/viz"

# 엔티티 type별 색상 팔레트(RGB). Gephi에서 종류(type)별로 색이 자동 구분돼 보이게 한다.
_PALETTE = [
    (78, 121, 167),
    (242, 142, 43),
    (225, 87, 89),
    (118, 183, 178),
    (89, 161, 79),
    (237, 201, 72),
    (176, 122, 161),
    (255, 157, 167),
]


# type 이름을 받아 팔레트에서 항상 같은 색(RGB)을 돌려준다(처음 본 type에 순서대로 색을 배정).
def _color_for_type(entity_type: str, type_colors: dict[str, tuple[int, int, int]]) -> tuple[int, int, int]:
    if entity_type not in type_colors:
        type_colors[entity_type] = _PALETTE[len(type_colors) % len(_PALETTE)]
    return type_colors[entity_type]


# 속성값이 None이어도 XML 직렬화가 깨지지 않도록 빈 문자열로 안전하게 변환한다.
def _safe(value) -> str:
    return str(value) if value is not None else ""


# 엔티티/관계 목록을 Gephi용 GEXF(XML) 문자열로 변환한다.
# 노드에는 type/description, 엣지에는 predicate/valid_from/source_doc 속성을 함께 실어
# Gephi의 Data Laboratory에서 그대로 필터링·조회할 수 있게 한다.
def build_gexf(entities: list[dict], relations: list[dict]) -> str:
    ET.register_namespace("", _GEXF_NS)
    ET.register_namespace("viz", _VIZ_NS)

    gexf = ET.Element(f"{{{_GEXF_NS}}}gexf", version="1.3")
    meta = ET.SubElement(
        gexf, f"{{{_GEXF_NS}}}meta", lastmodifieddate=datetime.now().strftime("%Y-%m-%d")
    )
    ET.SubElement(meta, f"{{{_GEXF_NS}}}creator").text = "GraphRAG"
    ET.SubElement(meta, f"{{{_GEXF_NS}}}description").text = "GraphRAG 엔티티/관계 내보내기"

    graph = ET.SubElement(
        gexf, f"{{{_GEXF_NS}}}graph", mode="static", defaultedgetype="directed"
    )

    # 노드 속성 정의(collection, type, description) — Gephi가 컬럼으로 인식하게 미리 선언한다.
    node_attrs = ET.SubElement(graph, f"{{{_GEXF_NS}}}attributes", {"class": "node"})
    ET.SubElement(
        node_attrs, f"{{{_GEXF_NS}}}attribute", id="collection", title="collection", type="string"
    )
    ET.SubElement(node_attrs, f"{{{_GEXF_NS}}}attribute", id="type", title="type", type="string")
    ET.SubElement(
        node_attrs, f"{{{_GEXF_NS}}}attribute", id="description", title="description", type="string"
    )

    # 엣지 속성 정의(predicate, valid_from, source_doc).
    edge_attrs = ET.SubElement(graph, f"{{{_GEXF_NS}}}attributes", {"class": "edge"})
    ET.SubElement(
        edge_attrs, f"{{{_GEXF_NS}}}attribute", id="predicate", title="predicate", type="string"
    )
    ET.SubElement(
        edge_attrs, f"{{{_GEXF_NS}}}attribute", id="valid_from", title="valid_from", type="string"
    )
    ET.SubElement(
        edge_attrs, f"{{{_GEXF_NS}}}attribute", id="source_doc", title="source_doc", type="string"
    )
    ET.SubElement(
        edge_attrs, f"{{{_GEXF_NS}}}attribute", id="collection", title="collection", type="string"
    )

    nodes_el = ET.SubElement(graph, f"{{{_GEXF_NS}}}nodes")
    type_colors: dict[str, tuple[int, int, int]] = {}
    node_ids: set[str] = set()
    for entity in entities:
        name = entity["name"]
        collection = _safe(entity.get("collection"))
        # 같은 이름이 컬렉션마다 따로 있을 수 있어, 노드 id는 (컬렉션::이름) 합성으로 충돌을 막는다. 라벨은 이름만.
        nid = f"{collection}::{name}"
        node_ids.add(nid)
        node = ET.SubElement(nodes_el, f"{{{_GEXF_NS}}}node", id=nid, label=name)
        attvalues = ET.SubElement(node, f"{{{_GEXF_NS}}}attvalues")
        ET.SubElement(
            attvalues, f"{{{_GEXF_NS}}}attvalue", {"for": "collection", "value": collection}
        )
        ET.SubElement(
            attvalues, f"{{{_GEXF_NS}}}attvalue", {"for": "type", "value": _safe(entity.get("type"))}
        )
        ET.SubElement(
            attvalues,
            f"{{{_GEXF_NS}}}attvalue",
            {"for": "description", "value": _safe(entity.get("description"))},
        )
        r, g, b = _color_for_type(_safe(entity.get("type")), type_colors)
        ET.SubElement(node, f"{{{_VIZ_NS}}}color", r=str(r), g=str(g), b=str(b))

    edges_el = ET.SubElement(graph, f"{{{_GEXF_NS}}}edges")
    edge_id = 0
    for relation in relations:
        collection = _safe(relation.get("collection"))
        source = f"{collection}::{relation['source']}"
        target = f"{collection}::{relation['target']}"
        # 양 끝 노드가 모두 노드 목록에 있을 때만 엣지를 쓴다(Gephi가 깨진 참조를 거부하는 것을 방지).
        if source not in node_ids or target not in node_ids:
            logger.warning("엣지 건너뜀 — 끝점 노드 없음: %s -> %s", source, target)
            continue
        edge = ET.SubElement(
            edges_el,
            f"{{{_GEXF_NS}}}edge",
            id=str(edge_id),
            source=source,
            target=target,
            label=_safe(relation.get("predicate")),
        )
        attvalues = ET.SubElement(edge, f"{{{_GEXF_NS}}}attvalues")
        ET.SubElement(
            attvalues,
            f"{{{_GEXF_NS}}}attvalue",
            {"for": "predicate", "value": _safe(relation.get("predicate"))},
        )
        ET.SubElement(
            attvalues,
            f"{{{_GEXF_NS}}}attvalue",
            {"for": "valid_from", "value": _safe(relation.get("valid_from"))},
        )
        ET.SubElement(
            attvalues,
            f"{{{_GEXF_NS}}}attvalue",
            {"for": "source_doc", "value": _safe(relation.get("source_doc"))},
        )
        ET.SubElement(
            attvalues, f"{{{_GEXF_NS}}}attvalue", {"for": "collection", "value": collection}
        )
        edge_id += 1

    ET.indent(gexf)
    return ET.tostring(gexf, encoding="unicode", xml_declaration=True)


# 그래프를 GEXF 파일로 내보낸다. collections로 범위를 지정하면 그 컬렉션만, None이면 전체.
# exports/ 아래 타임스탬프 이름으로 저장하고 경로를 반환한다.
def export_graph(collections: list[str] | None = None) -> Path:
    entities = graph_manager.get_all_entities(collections)
    relations = graph_manager.get_all_relations(collections)
    gexf = build_gexf(entities, relations)

    settings.export_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = settings.export_dir / f"graph_{timestamp}.gexf"
    out_path.write_text(gexf, encoding="utf-8")
    logger.info(
        "GEXF 내보내기 완료: %s (엔티티 %d개, 관계 %d개)", out_path, len(entities), len(relations)
    )
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    export_graph()
