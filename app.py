# USAGE.md의 graphrag 명령들을 마우스로 편하게 실행하는 통합 웹 GUI.
# CLI(graphrag_cli.py)와 똑같이 db/*·pipeline/*·query·graph_export·backup_db/restore_db의
# 기존 함수를 그대로 호출하는 얇은 프런트엔드다(핵심 로직/스키마는 건드리지 않음).
import math
import shutil
import tempfile
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from config import settings
from db import graph_manager, sqlite_manager, vector_manager

st.set_page_config(page_title="개인용 GraphRAG", layout="wide")
st.title("개인용 GraphRAG")
st.caption("USAGE.md의 graphrag 명령을 버튼으로 실행하는 GUI입니다. 컬렉션=사업 단위로 격리됩니다.")


# ───────────────────────── 공통 헬퍼 ─────────────────────────


# 존재하는 모든 컬렉션 이름을 정렬해 돌려준다(문서가 있는 것 + 그래프에 있는 것의 합집합).
def _list_collections() -> list[str]:
    doc_counts = sqlite_manager.get_collection_doc_counts()
    return sorted(set(doc_counts) | set(graph_manager.get_all_collections()))


# 처리/저장 대상 '컬렉션 하나'를 고르는 위젯 — 기존 컬렉션을 고르거나 새 이름을 직접 입력하게 한다.
def _pick_collection(key: str, default: str | None = None) -> str:
    existing = _list_collections()
    default = default or settings.default_collection
    new_option = "➕ 새 컬렉션 직접 입력"
    options = existing + [new_option]
    index = existing.index(default) if default in existing else len(options) - 1
    choice = st.selectbox("컬렉션(사업)", options, index=index, key=f"{key}_sel")
    if choice == new_option:
        typed = st.text_input("새 컬렉션 이름", value=default, key=f"{key}_new").strip()
        return typed or settings.default_collection
    return choice


# 조회 '범위'를 고르는 위젯 — 전체 종합(--all) 또는 특정 컬렉션 여러 개.
# 반환값 None이면 전체 종합. 계층이 있으면 부모 선택 시 자손 컬렉션까지 자동으로 펼친다(CLI와 동일).
def _pick_scope(key: str) -> list[str] | None:
    existing = _list_collections()
    mode = st.radio(
        "범위", ["전체 종합(--all)", "특정 컬렉션 선택"], horizontal=True, key=f"{key}_mode"
    )
    if mode.startswith("전체") or not existing:
        return None
    chosen = st.multiselect("컬렉션 선택(여러 개 가능)", existing, key=f"{key}_ms")
    if not chosen:
        return None
    expanded: list[str] = []
    seen: set[str] = set()
    for name in chosen:
        for descendant in sqlite_manager.get_collection_descendants(name):
            if descendant not in seen:
                seen.add(descendant)
                expanded.append(descendant)
    return expanded or None


# 범위(None=전체)를 사람이 읽을 한 줄 라벨로 바꾼다.
def _scope_label(collections: list[str] | None) -> str:
    return "전체 종합(--all)" if collections is None else ", ".join(collections)


# ───────────────────────── 📊 현황 탭 ─────────────────────────


# 컬렉션 목록을 부모-자식 계층 트리(들여쓰기)로 그린다(graphrag collections와 동일 구성).
def _render_collections_tree() -> None:
    doc_counts = sqlite_manager.get_collection_doc_counts()
    all_collections = sorted(set(doc_counts) | set(graph_manager.get_all_collections()))
    if not all_collections:
        st.info("(아직 컬렉션이 없습니다)")
        return

    parent_of = sqlite_manager.list_collection_hierarchy()  # {자식: 부모}
    children_of: dict[str, list[str]] = {}
    for child, parent in parent_of.items():
        children_of.setdefault(parent, []).append(child)

    lines: list[str] = []

    # 한 컬렉션과 그 자손을 들여쓰기로 누적한다.
    def _walk(collection: str, depth: int) -> None:
        entity_count = graph_manager.count_entities([collection])
        indent = "    " * depth
        lines.append(
            f"{indent}- **{collection}**: 문서 {doc_counts.get(collection, 0)}개, 엔티티 {entity_count}개"
        )
        for child in sorted(children_of.get(collection, [])):
            _walk(child, depth + 1)

    roots = [
        c for c in all_collections
        if parent_of.get(c) is None or parent_of.get(c) not in all_collections
    ]
    for root in roots:
        _walk(root, 0)
    st.markdown("\n".join(lines))


# DB 현황(범위별 수치/타입/관계) + 오늘 사용량 + 고아 경고 + 컬렉션 트리를 보여준다.
def render_status_tab() -> None:
    from db import document_store

    scope = _pick_scope("status")
    st.caption(f"범위: {_scope_label(scope)}")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("문서 수", sqlite_manager.count_documents(scope))
    col2.metric("벡터 청크 수", vector_manager.count_chunks(scope))
    col3.metric("엔티티 수", graph_manager.count_entities(scope))
    col4.metric("관계 수", graph_manager.count_relations(scope))

    st.write("**엔티티 타입:** " + (", ".join(graph_manager.get_known_types(scope)) or "(없음)"))
    st.write("**관계 이름:** " + (", ".join(graph_manager.get_known_predicates(scope)) or "(없음)"))

    used = sqlite_manager.get_api_usage_today()
    limit = settings.llm_daily_limit
    st.write(f"**오늘 LLM 요청:** {used}/{limit} (남은 한도 {max(0, limit - used)})")

    orphan_count = len(document_store.find_orphaned_source_ids())
    if orphan_count:
        st.warning(f"고아 데이터 {orphan_count}건(전역) — 문서를 넣을 때 자동 정리됩니다.")

    st.divider()
    st.subheader("컬렉션 목록")
    _render_collections_tree()


# ───────────────────────── 📥 문서 넣기 탭 ─────────────────────────


# 처리 예정 (파일명, 본문) 목록으로 예상 요청 수와 오늘 한도를 보여주고, 초과 시 막을지 결정한다.
# 반환: (진행 가능 여부, 예상 합계). force=True면 초과여도 진행 가능으로 둔다(graphrag ingest의 한도 가드와 동일).
# is_gemini=False(ollama/CLI 백엔드)면 Gemini 일일 한도(RPD)와 무관하므로 예상 청크 수만 안내하고 절대 막지 않는다.
def _estimate_and_guard(
    items: list[tuple[str, str]], force: bool, glean: int = 0, is_gemini: bool = True
) -> tuple[bool, int]:
    from db import document_store

    # gleaning이 켜지면 청크당 최대 (1+glean)회 호출된다 → 예상/한도 계산에 곱한다.
    multiplier = 1 + max(0, glean)
    estimates = [(name, document_store.estimate_request_count(text)) for name, text in items]
    total = sum(n for _, n in estimates) * multiplier

    if not is_gemini:
        # 로컬(ollama)/구독(CLI) 백엔드: RPD 한도 없음 → 청크 수만 안내하고 항상 진행 가능.
        st.caption(f"예상 청크(호출) 수: 총 **{total}** — 로컬/구독 백엔드라 하루 한도(RPD)에 안 잡힙니다.")
        return True, total

    used = sqlite_manager.get_api_usage_today()
    limit = settings.llm_daily_limit
    remaining = max(0, limit - used)

    st.markdown(f"**오늘 사용:** {used}/{limit} · 남은 한도 **{remaining}**")
    if glean:
        st.caption(f"gleaning {glean}라운드 → 청크당 최대 {multiplier}회 호출")
    for name, n in estimates:
        detail = f"**{n}** 요청" if multiplier == 1 else f"{n}청크 × {multiplier} = **{n * multiplier}** 요청"
        st.write(f"- {name}: 예상 {detail}")
    st.markdown(f"**이번 작업 합계: {total} 요청** → 처리 후 {used + total}/{limit}")

    if used + total > limit and not force:
        st.error(f"⛔ 한도 초과 예상: {used} + {total} = {used + total} > {limit} — 처리가 막힙니다.")
        for name, n in estimates:
            eff = n * multiplier
            if eff > limit:
                pieces = math.ceil(eff / limit)
                cost = f"{n}청크" if multiplier == 1 else f"{n}청크×{multiplier}={eff}요청"
                st.warning(
                    f"· {name}({cost})는 하루 한도를 단독으로 넘습니다 "
                    f"→ 약 {pieces}개로 의미 단위(전반/후반 등)로 나눠 각각 넣으세요."
                )
        st.caption("그래도 강행하려면 위 '강행(--force)'를 체크하세요. (실제 한도에 닿으면 API가 막습니다.)")
        return False, total
    return True, total


# 문서 처리 직후 공통 마무리 — 고아 데이터 정리 + (옵션) 엔티티 자동 병합(graphrag ingest 후처리와 동일).
def _post_ingest(collection: str, no_merge: bool) -> None:
    from db import document_store

    removed = document_store.cleanup_orphaned_data()
    if removed:
        st.caption(f"고아 데이터 {removed}건 자동 정리됨")
    if not no_merge:
        from pipeline import entity_resolution

        with st.spinner("엔티티 자동 병합 중..."):
            entity_resolution.run([collection])
        st.caption("엔티티 자동 병합 완료")


# 업로드된 파일들을 임시폴더에 풀어 한 건씩 처리하고, 성공/실패에 따라 processed/·failed/로 복사한다.
def _run_ingest_uploaded(
    uploaded_files, collection: str, no_merge: bool, glean: int = 0, backend: str | None = None
) -> None:
    from pipeline import ingest

    with tempfile.TemporaryDirectory() as tmp_dir:
        for uploaded_file in uploaded_files:
            tmp_path = Path(tmp_dir) / uploaded_file.name
            tmp_path.write_bytes(uploaded_file.getvalue())
            with st.spinner(f"{uploaded_file.name} 처리 중..."):
                try:
                    changed = ingest.process_file(
                        tmp_path, collection, glean_rounds=glean, backend=backend
                    )
                    dest = settings.processed_dir / collection
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy(tmp_path, dest / uploaded_file.name)
                    if changed:
                        st.success(f"{uploaded_file.name} — 처리 완료 (컬렉션: {collection})")
                    else:
                        st.info(f"{uploaded_file.name} — 변경 없음(이미 처리됨)")
                except Exception as exc:
                    failed = settings.failed_dir / collection
                    failed.mkdir(parents=True, exist_ok=True)
                    shutil.copy(tmp_path, failed / uploaded_file.name)
                    st.error(f"{uploaded_file.name} — 처리 실패: {exc}")
    _post_ingest(collection, no_merge)


# inbox/ 폴더 전체를 지정 컬렉션으로 일괄 처리한다(graphrag ingest --inbox와 동일).
def _run_ingest_inbox(
    collection: str, no_merge: bool, glean: int = 0, backend: str | None = None
) -> None:
    import process_inbox

    with st.spinner("inbox/ 처리 중..."):
        process_inbox.process_inbox(collection, glean_rounds=glean, backend=backend)
    st.success(f"inbox/ 처리 완료 (컬렉션: {collection})")
    _post_ingest(collection, no_merge)


# 문서 추출+그래프 생성(ingest) 탭 — 업로드/inbox, 예상 요청 수·한도 가드, force/no-merge 옵션.
# GUI 백엔드 라벨 → process_file backend 인자. 완전 로컬이 기본이라 Ollama를 맨 앞(기본 선택)에 둔다.
# Gemini는 "None"으로 넘겨 기존 Gemini 경로와 100% 동일하게 동작한다(외부 업로드 — 옵트인).
_INGEST_BACKENDS = {
    "Ollama (로컬·기본)": "ollama",
    "Gemini (외부·키 필요)": None,
    "Claude CLI (외부·구독)": "claude_cli",
    "Codex CLI (외부·구독)": "codex_cli",
}


def render_ingest_tab() -> None:
    collection = _pick_collection("ingest")
    backend_label = st.selectbox(
        "추출 백엔드", list(_INGEST_BACKENDS), key="ingest_backend",
        help="Ollama=로컬 무료·데이터 외부로 안 나감(기본). Gemini/Claude/Codex=외부로 전송(빠르지만 업로드됨).",
    )
    backend = _INGEST_BACKENDS[backend_label]
    is_gemini = backend in (None, "gemini")
    if is_gemini and not settings.gemini_api_key:
        st.warning("`.env`에 GEMINI_API_KEY가 없습니다 — Gemini 추출은 실패합니다. 로컬을 쓰려면 'Ollama'를 고르세요.")

    source = st.radio(
        "처리할 대상", ["업로드한 파일", "inbox/ 폴더 일괄"], horizontal=True, key="ingest_src"
    )
    opt1, opt2 = st.columns(2)
    force = opt1.checkbox("하루 한도 초과여도 강행(--force)", key="ingest_force")
    no_merge = opt2.checkbox("처리 후 자동 병합 건너뛰기(--no-merge)", key="ingest_nomerge")
    glean = st.number_input(
        "gleaning 라운드 (0=끔)", min_value=0, max_value=5, value=0, step=1, key="ingest_glean",
        help="1 이상이면 청크마다 놓친 엔티티/관계를 그만큼 더 캐냅니다. 요청 수·시간이 (1+N)배로 늘지만 recall(포착량)이 올라갑니다.",
    )
    st.caption("문서는 약 2000자 단위로 잘려 청크당 LLM 호출 1번이 나갑니다(호출당 3초 대기). 긴 문서는 시간이 걸립니다.")

    if source == "업로드한 파일":
        uploaded_files = st.file_uploader(
            "메모 파일(.md, .txt)을 드래그앤드롭하거나 선택하세요",
            type=["md", "txt"],
            accept_multiple_files=True,
            key="ingest_uploader",
        )
        if not uploaded_files:
            st.info("파일을 올리면 예상 요청 수를 계산해 보여줍니다.")
            return
        items = [
            (f.name, f.getvalue().decode("utf-8", errors="replace")) for f in uploaded_files
        ]
        allowed, _ = _estimate_and_guard(items, force, glean, is_gemini)
        if st.button("추출 + 그래프 생성 시작", disabled=not allowed, key="ingest_run_uploaded"):
            _run_ingest_uploaded(uploaded_files, collection, no_merge, glean, backend)
    else:
        settings.inbox_dir.mkdir(parents=True, exist_ok=True)
        inbox_files = sorted(p for p in settings.inbox_dir.iterdir() if p.is_file())
        if not inbox_files:
            st.info(f"inbox/ 폴더가 비어 있습니다: {settings.inbox_dir}")
            return
        items = [(p.name, p.read_text(encoding="utf-8", errors="replace")) for p in inbox_files]
        allowed, _ = _estimate_and_guard(items, force, glean, is_gemini)
        if st.button("inbox/ 일괄 처리 시작", disabled=not allowed, key="ingest_run_inbox"):
            _run_ingest_inbox(collection, no_merge, glean, backend)


# ───────────────────────── 💬 질문 탭 ─────────────────────────


# 추출된 그래프+벡터 정보만 근거로 질문에 답하는 탭(graphrag query). 범위는 전체 또는 특정 컬렉션.
def render_query_tab() -> None:
    scope = _pick_scope("query")
    st.caption(f"범위: {_scope_label(scope)} · 그래프에 등록된 이름을 그대로 쓰면 정확도가 올라갑니다.")
    mode = st.radio(
        "검색 방식", ["로컬 (기본)", "글로벌 (주제·종합)"], horizontal=True, key="query_mode"
    )
    # 답변 합성 백엔드 — 기본 Ollama(무과금). 백엔드는 ingest 탭과 동일 집합을 재사용한다.
    _labels = list(_INGEST_BACKENDS)
    _default_idx = list(_INGEST_BACKENDS.values()).index("ollama")
    backend_label = st.selectbox(
        "답변 백엔드", _labels, index=_default_idx, key="query_backend",
        help="로컬 검색 답변을 어느 모델로 합성할지. 기본 Ollama=무과금. Gemini는 키·RPD 한도 필요.",
    )
    backend = _INGEST_BACKENDS[backend_label]
    if backend in (None, "gemini") and not settings.gemini_api_key:
        st.warning("`.env`에 GEMINI_API_KEY가 없습니다 — Gemini 답변은 실패합니다. 'Ollama'를 고르면 무과금으로 답합니다.")
    question = st.text_area("질문", placeholder="예: 김부장은 무슨 일을 해?", key="query_q")

    if st.button("질문하기", disabled=not question.strip(), key="query_run"):
        with st.spinner("답변 생성 중..."):
            try:
                if mode.startswith("글로벌"):
                    answer = _answer_global_with_fallback(question.strip(), scope, backend)
                else:
                    from query import answer_question

                    answer = answer_question(question.strip(), collections=scope, backend=backend)
                st.markdown("### 답변")
                st.write(answer)
            except Exception as exc:
                st.error(f"질문 처리 실패: {exc}")


# [M4] 글로벌(map-reduce) 질의를 실행한다. 스코프 중 커뮤니티가 stale(재빌드 필요 — 미빌드 포함,
# is_communities_dirty가 둘 다 판정)인 컬렉션이 있으면 CLI(--mode global)와 같은 논리로 재빌드 안내를
# 띄운 뒤 로컬 검색 답변으로 즉시 폴백한다.
def _answer_global_with_fallback(
    question: str, scope: list[str] | None, backend: str | None = None
) -> str:
    from query import answer_question, answer_question_global

    target = scope if scope is not None else _list_collections()
    stale = [c for c in target if sqlite_manager.is_communities_dirty(c)]
    if stale:
        st.warning(
            f"커뮤니티가 없거나 오래됨(재빌드 필요): {', '.join(stale)} "
            f"— 먼저 `graphrag communities build --collection <이름>`을 실행하세요. 우선 로컬 검색으로 답합니다."
        )
        return answer_question(question, collections=scope, backend=backend)
    return answer_question_global(question, collections=scope, level=None)


# ───────────────────────── 🕸️ 그래프 탭 ─────────────────────────


# 그래프 시각화(PyVis) + Gephi용 GEXF 내보내기/다운로드 탭(graphrag graph).
def render_graph_tab() -> None:
    scope = _pick_scope("graph")
    st.caption(f"범위: {_scope_label(scope)}")

    # 시각화는 무거울 수 있어 버튼으로만 그린다(매 상호작용마다 자동 렌더 방지).
    if st.button("그래프 표시 / 새로고침", key="graph_show"):
        st.session_state["graph_scope"] = scope
        st.session_state["show_graph"] = True
    if st.session_state.get("show_graph"):
        from graph_viz import build_graph_html

        shown_scope = st.session_state.get("graph_scope")
        entities = graph_manager.get_all_entities(shown_scope)
        relations = graph_manager.get_all_relations(shown_scope)
        if not entities:
            st.info("이 범위에는 아직 엔티티가 없습니다. 먼저 문서를 넣어보세요.")
        else:
            components.html(build_graph_html(entities, relations), height=670, scrolling=True)

    st.divider()
    st.subheader("Gephi용 GEXF 내보내기")
    st.caption("저장된 .gexf를 Gephi의 File → Open으로 열면 컬렉션·타입별로 색·필터가 적용됩니다.")
    if st.button("GEXF 내보내기", key="graph_export"):
        from graph_export import export_graph

        out_path = export_graph(scope)
        st.session_state["last_gexf"] = str(out_path)
        st.success(f"저장 완료: {out_path}")

    last_gexf = st.session_state.get("last_gexf")
    if last_gexf and Path(last_gexf).exists():
        st.download_button(
            "내려받기 (.gexf)",
            data=Path(last_gexf).read_bytes(),
            file_name=Path(last_gexf).name,
            mime="application/xml",
            key="graph_download",
        )


# ───────────────────────── 🗂️ 컬렉션 탭 ─────────────────────────


# 컬렉션에 부모(본부)를 지정한다 — 자기 자신/순환 지정은 거부(graphrag set-parent와 동일).
def _set_parent(child: str, parent: str) -> None:
    if child == parent:
        st.error("자기 자신을 부모로 지정할 수 없습니다.")
        return
    if parent in sqlite_manager.get_collection_descendants(child):
        st.error(f"순환이 생겨 거부합니다: '{parent}'은(는) 이미 '{child}'의 하위입니다.")
        return
    sqlite_manager.set_collection_parent(child, parent)
    st.success(f"계층 설정: '{child}' → 부모 '{parent}'")


# 컬렉션 유지보수 탭 — 병합, 계층(부모) 지정/해제, 문서 삭제, 컬렉션 통째 삭제.
def render_collections_tab() -> None:
    collections = _list_collections()
    if not collections:
        st.info("아직 컬렉션이 없습니다. 먼저 '문서 넣기'로 자료를 넣어보세요.")
        return

    st.subheader("엔티티 자동 병합 (merge)")
    st.caption("같은 컬렉션 안의 중복 엔티티를 표기 정규화+임베딩 유사도로 병합합니다(비용 0). 사업 간 병합은 하지 않습니다.")
    merge_scope = _pick_scope("merge")
    if st.button("병합 실행", key="merge_run"):
        from pipeline import entity_resolution

        with st.spinner("병합 중..."):
            entity_resolution.run(merge_scope)
        st.success("병합 작업 완료")

    st.divider()
    st.subheader("컬렉션 계층 — 본부(부모) 지정")
    hcol1, hcol2 = st.columns(2)
    child = hcol1.selectbox("자식 컬렉션", collections, key="hier_child")
    parent = hcol2.selectbox("부모(본부) 컬렉션", collections, key="hier_parent")
    hbtn1, hbtn2 = st.columns(2)
    if hbtn1.button("부모 지정 (set-parent)", key="set_parent_run"):
        _set_parent(child, parent)
    if hbtn2.button("부모 해제 (unset-parent)", key="unset_parent_run"):
        sqlite_manager.unset_collection_parent(child)
        st.success(f"계층 해제: '{child}'")

    st.divider()
    st.subheader("문서 삭제 (delete)")
    st.caption("문서의 벡터·관계·기록을 지우고, 관계가 끊겨 고립된 엔티티까지 정리합니다.")
    del_coll = st.selectbox("컬렉션", collections, key="del_doc_coll")
    del_name = st.text_input("삭제할 문서 파일명(예: 메모.md)", key="del_doc_name")
    if st.button("문서 삭제", disabled=not del_name.strip(), key="del_doc_run"):
        from db import document_store

        name = Path(del_name.strip()).name
        ok = document_store.delete_document(del_coll, name)
        if ok:
            removed = graph_manager.cleanup_isolated_entities([del_coll])
            st.success(f"[{del_coll}] {name} 삭제됨 · 고립 엔티티 {removed}개 정리")
        else:
            st.warning(f"[{del_coll}] {name} — 문서를 찾을 수 없음")

    st.divider()
    st.subheader("컬렉션 통째로 삭제 (delete-collection)")
    target = st.selectbox("삭제할 컬렉션", collections, key="del_coll_target")
    confirm = st.checkbox(
        f"'{target}'의 문서·엔티티·관계·청크를 모두 삭제합니다. 되돌릴 수 없습니다.",
        key="del_coll_confirm",
    )
    if st.button("컬렉션 통째로 삭제", disabled=not confirm, key="del_coll_run"):
        from db import document_store

        count = document_store.delete_collection(target)
        st.success(f"컬렉션 '{target}' 삭제 완료 (문서 {count}개 + 엔티티/관계/청크)")


# ───────────────────────── 🔗 브릿지 탭 ─────────────────────────


# 컬렉션을 넘는 SAME_AS 브릿지 관리 탭 — 목록/후보 제안/직접 연결·해제(graphrag bridge).
def render_bridge_tab() -> None:
    st.caption("컬렉션(사업)이 달라 격리된 '같은 대상'을 병합 없이 연결합니다(SAME_AS 브릿지).")

    st.subheader("현재 브릿지 목록 (list)")
    bridges = graph_manager.list_all_bridges()
    if bridges:
        for b in bridges:
            st.write(f"- [{b['collection_a']}] {b['name_a']} ↔ [{b['collection_b']}] {b['name_b']}")
    else:
        st.info("아직 브릿지가 없습니다.")

    st.divider()
    st.subheader("브릿지 후보 제안 (suggest)")
    threshold = st.slider(
        "유사도 임계값", 0.5, 1.0, float(settings.bridge_similarity_threshold), 0.01, key="bridge_thr"
    )
    if st.button("후보 찾기", key="bridge_suggest"):
        from pipeline import bridge

        with st.spinner("후보 탐색 중..."):
            candidates = bridge.find_bridge_candidates(threshold)
        if not candidates:
            st.info("브릿지 후보가 없습니다.")
        else:
            for coll_a, name_a, coll_b, name_b, score in sorted(candidates, key=lambda c: -c[4]):
                st.write(f"- [{coll_a}] {name_a} ↔ [{coll_b}] {name_b} · 유사도 {score * 100:.1f}%")

    st.divider()
    st.subheader("브릿지 직접 연결 / 해제 (add / remove)")
    collections = _list_collections()
    if not collections:
        st.info("컬렉션이 필요합니다.")
        return
    bcol1, bcol2 = st.columns(2)
    coll_a = bcol1.selectbox("컬렉션 A", collections, key="bridge_coll_a")
    name_a = bcol1.text_input("이름 A", key="bridge_name_a")
    coll_b = bcol2.selectbox("컬렉션 B", collections, key="bridge_coll_b")
    name_b = bcol2.text_input("이름 B", key="bridge_name_b")
    has_names = bool(name_a.strip() and name_b.strip())
    bbtn1, bbtn2 = st.columns(2)
    if bbtn1.button("연결 (add)", disabled=not has_names, key="bridge_add"):
        ok = graph_manager.add_bridge(coll_a, name_a.strip(), coll_b, name_b.strip())
        if ok:
            st.success(f"브릿지 연결됨: [{coll_a}] {name_a} ↔ [{coll_b}] {name_b}")
        else:
            st.error("실패 — 엔티티가 없거나 동일 대상입니다.")
    if bbtn2.button("해제 (remove)", disabled=not has_names, key="bridge_remove"):
        graph_manager.remove_bridge(coll_a, name_a.strip(), coll_b, name_b.strip())
        st.success(f"브릿지 해제: [{coll_a}] {name_a} ↔ [{coll_b}] {name_b}")


# ───────────────────────── 🛠️ 유지보수 탭 ─────────────────────────


# DB 3종 스키마를 초기화한다. reset=True면 기존 graphrag_dbs/를 통째로 지운다(graphrag init).
def _init_db(reset: bool) -> None:
    if reset and settings.db_dir.exists():
        graph_manager.close_connection()
        shutil.rmtree(settings.db_dir)
    sqlite_manager.init_schema()
    vector_manager.init_schema()
    graph_manager.init_schema()
    st.success("DB 초기화 완료" + (" (기존 데이터 리셋됨)" if reset else ""))


# 유지보수 탭 — DB 초기화/리셋, 백업, 복원.
def render_maintenance_tab() -> None:
    st.subheader("DB 초기화 (init)")
    st.caption("스키마만 생성합니다(이미 있으면 그대로). --reset은 기존 DB를 전부 지웁니다.")
    if st.button("스키마 생성/확인 (init)", key="init_plain"):
        _init_db(reset=False)
    reset_ok = st.checkbox(
        "기존 DB를 전부 지우고 새로 시작합니다. 되돌릴 수 없습니다.", key="init_reset_ok"
    )
    if st.button("초기화 + 리셋 (init --reset)", disabled=not reset_ok, key="init_reset_run"):
        _init_db(reset=True)

    st.divider()
    st.subheader("백업 (backup)")
    st.caption("graphrag_dbs/ 전체를 zip으로 backups/에 저장합니다(최신 10개 자동 보관).")
    if st.button("지금 백업", key="backup_run"):
        import backup_db

        with st.spinner("백업 중..."):
            out_path = backup_db.create_backup()
        st.success(f"백업 완료: {out_path}")

    st.divider()
    st.subheader("복원 (restore)")
    st.caption("원자적 복원 — 먼저 임시폴더에 풀어보고 성공했을 때만 교체하므로, 실패해도 기존 DB는 보존됩니다.")
    backup_dir = settings.project_root / "backups"
    zips = sorted(backup_dir.glob("graphrag_dbs_*.zip"), reverse=True) if backup_dir.exists() else []
    if not zips:
        st.info("backups/ 폴더에 백업이 없습니다.")
        return
    pick = st.selectbox("복원할 백업", [z.name for z in zips], key="restore_pick")
    restore_ok = st.checkbox(
        "현재 DB를 이 백업으로 덮어씁니다.", key="restore_confirm"
    )
    if st.button("복원 실행", disabled=not restore_ok, key="restore_run"):
        import restore_db

        with st.spinner("복원 중..."):
            restore_db.restore_backup(backup_dir / pick)
        st.success("복원 완료")


# ───────────────────────── 탭 배치 ─────────────────────────

tab_status, tab_ingest, tab_query, tab_graph, tab_collections, tab_bridge, tab_maint = st.tabs(
    ["📊 현황", "📥 문서 넣기", "💬 질문", "🕸️ 그래프", "🗂️ 컬렉션", "🔗 브릿지", "🛠️ 유지보수"]
)

with tab_status:
    render_status_tab()
with tab_ingest:
    render_ingest_tab()
with tab_query:
    render_query_tab()
with tab_graph:
    render_graph_tab()
with tab_collections:
    render_collections_tab()
with tab_bridge:
    render_bridge_tab()
with tab_maint:
    render_maintenance_tab()
