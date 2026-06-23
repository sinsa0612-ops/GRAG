# 드래그앤드롭으로 메모를 올려 추출+그래프 생성까지 한 번에 실행하고, 그래프를 시각화하는 웹 UI.
import shutil
import tempfile
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

import process_inbox
from config import settings
from db import graph_manager, sqlite_manager, vector_manager
from graph_viz import build_graph_html
from pipeline import ingest

st.set_page_config(page_title="개인용 GraphRAG", layout="wide")
st.title("개인용 GraphRAG")

tab_upload, tab_graph, tab_status = st.tabs(["파일 업로드/처리", "그래프 시각화", "DB 현황"])


# 업로드된 파일 하나를 지정한 컬렉션(사업)으로 처리하고 성공/실패에 따라 분류 폴더로 옮긴다.
def _process_uploaded_file(uploaded_file, tmp_dir: Path, collection: str) -> None:
    tmp_path = tmp_dir / uploaded_file.name
    tmp_path.write_bytes(uploaded_file.getvalue())

    with st.spinner(f"{uploaded_file.name} 처리 중..."):
        try:
            processed = ingest.process_file(tmp_path, collection)
            (settings.processed_dir / collection).mkdir(parents=True, exist_ok=True)
            shutil.copy(tmp_path, settings.processed_dir / collection / uploaded_file.name)
            if processed:
                st.success(f"{uploaded_file.name} — 처리 완료 (컬렉션: {collection})")
            else:
                st.info(f"{uploaded_file.name} — 변경 없음(이미 처리된 내용)")
        except Exception as exc:
            (settings.failed_dir / collection).mkdir(parents=True, exist_ok=True)
            shutil.copy(tmp_path, settings.failed_dir / collection / uploaded_file.name)
            st.error(f"{uploaded_file.name} — 처리 실패: {exc}")


with tab_upload:
    st.subheader("메모 업로드")
    collection = st.text_input(
        "컬렉션(사업) 이름", value=settings.default_collection,
        help="같은 컬렉션 안에서만 엔티티가 연결됩니다. 사업별로 다른 이름을 쓰면 서로 격리됩니다.",
    ).strip() or settings.default_collection
    uploaded_files = st.file_uploader(
        "메모 파일(.md, .txt)을 드래그앤드롭하거나 선택하세요",
        type=["md", "txt"],
        accept_multiple_files=True,
    )

    if st.button("추출 + 그래프 생성 시작", disabled=not uploaded_files):
        with tempfile.TemporaryDirectory() as tmp_dir:
            for uploaded_file in uploaded_files:
                _process_uploaded_file(uploaded_file, Path(tmp_dir), collection)

    st.divider()
    st.subheader("inbox/ 폴더 일괄 처리")
    st.caption("inbox/ 폴더의 파일을 위에서 정한 컬렉션으로 한 번에 처리합니다.")
    if st.button("inbox/ 처리 시작"):
        with st.spinner("inbox/ 처리 중..."):
            process_inbox.process_inbox(collection)
        st.success(f"inbox/ 처리 완료 (컬렉션: {collection})")

with tab_graph:
    st.subheader("그래프 시각화")
    if st.button("새로고침", key="refresh_graph"):
        st.rerun()

    entities = graph_manager.get_all_entities()
    relations = graph_manager.get_all_relations()

    if not entities:
        st.info("아직 그래프에 엔티티가 없습니다. 먼저 메모를 업로드해보세요.")
    else:
        html = build_graph_html(entities, relations)
        components.html(html, height=670, scrolling=True)

with tab_status:
    st.subheader("DB 현황")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("문서 수", sqlite_manager.count_documents())
    col2.metric("벡터 청크 수", vector_manager.count_chunks())
    col3.metric("엔티티 수", graph_manager.count_entities())
    col4.metric("관계 수", graph_manager.count_relations())

    st.caption("사용 중인 엔티티 타입: " + ", ".join(graph_manager.get_known_types() or ["(없음)"]))
    st.caption("사용 중인 관계 이름: " + ", ".join(graph_manager.get_known_predicates() or ["(없음)"]))
