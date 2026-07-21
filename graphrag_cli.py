# graphrag 통합 CLI — 기존 기능(초기화/추출/질문/그래프/현황/병합/백업)을 하나의 명령으로 묶는 얇은 디스패처.
# 각 서브커맨드는 기존 모듈 함수를 그대로 호출한다. 컬렉션(사업) 범위는 --collection / --all 로 지정한다.
import argparse
import logging
import math
import shutil
from pathlib import Path

from config import settings


# 조회 계열(query/graph/status/merge)에서 대상 컬렉션 범위를 정한다.
# --all 또는 아무것도 주지 않으면 None(전체 컬렉션 종합), --collection A,B면 그 목록으로 좁힌다.
# 계층이 지정돼 있으면 부모 이름은 자손 컬렉션까지 자동으로 펼친다(본부 단위 조회).
def _collections_from_args(args) -> list[str] | None:
    if getattr(args, "all", False):
        return None
    raw = getattr(args, "collection", None)
    if not raw:
        return None

    from db import sqlite_manager

    names = [c.strip() for c in raw.split(",") if c.strip()]
    expanded: list[str] = []
    seen: set[str] = set()
    for name in names:
        for descendant in sqlite_manager.get_collection_descendants(name):
            if descendant not in seen:
                seen.add(descendant)
                expanded.append(descendant)
    return expanded or None


# DB 3종 스키마를 초기화한다. --reset이면 기존 graphrag_dbs/를 통째로 지우고 새로 만든다.
def cmd_init(args) -> None:
    from db import graph_manager, sqlite_manager, vector_manager

    if args.reset and settings.db_dir.exists():
        graph_manager.close_connection()
        shutil.rmtree(settings.db_dir)
    sqlite_manager.init_schema()
    vector_manager.init_schema()
    graph_manager.init_schema()
    print("DB 초기화 완료" + (" (기존 데이터 리셋됨)" if args.reset else ""))


# 처리 도중 끊긴 고아 데이터(어느 문서에도 속하지 않는 청크/관계)를 자동 정리하고, 있으면 알려준다.
def _report_orphan_cleanup() -> None:
    from db import document_store

    removed = document_store.cleanup_orphaned_data()
    if removed:
        print(f"고아 데이터 {removed}건 자동 정리됨")


# ingest 직후 해당 컬렉션의 중복 엔티티를 자동 병합한다(표기 정규화 + 임베딩, 임베딩은 로컬이라 추가 비용 0).
# 같은 문서를 추출하다 갈라진 표기 파편을 그 자리에서 정리해, 그래프가 파편화된 채 쌓이지 않게 한다.
# --no-merge로 끌 수 있다.
def _run_auto_merge(collection: str, args) -> None:
    if getattr(args, "no_merge", False):
        return
    from pipeline import entity_resolution

    print("엔티티 자동 병합 중...")
    entity_resolution.run([collection])


# 한도 초과로 차단됐을 때, 문서를 몇 개로 쪼개야 하는지 안내한다.
# multiplier는 gleaning으로 청크당 호출이 몇 배가 되는지(1=gleaning 끔)로, 요청 수 환산에 반영한다.
def _print_split_guidance(estimates: list[tuple], remaining: int, limit: int, multiplier: int = 1) -> None:
    step = max(1, settings.chunk_size - settings.chunk_overlap)
    for path, n in estimates:
        eff = n * multiplier
        if eff > limit:
            pieces = math.ceil(eff / limit)
            max_chunks = max(1, limit // multiplier)
            approx_chars = max_chunks * step
            cost = f"{n}청크" if multiplier == 1 else f"{n}청크×{multiplier}={eff}요청"
            print(
                f"  · {path.name}({cost})는 하루 한도({limit})를 단독으로 넘습니다 "
                f"→ 약 {pieces}개로 나눠 각각 넣으세요 (조각당 ≤{max_chunks}청크 ≈ ≤약 {approx_chars:,}자)."
            )
            print(f"    예: {path.stem}-1{path.suffix}, {path.stem}-2{path.suffix} ...")
    if remaining > 0:
        print(f"  · 오늘 남은 한도는 {remaining}요청입니다. 합계가 이보다 작아지게 나누거나 내일 이어서 하세요.")
    else:
        print("  · 오늘 한도를 이미 다 썼습니다. 내일(태평양시간 자정 리셋) 다시 시도하세요.")


# 파일들(또는 inbox/ 전체)을 지정한 컬렉션으로 추출·저장한다.
# 처리 전에 예상 요청 수(=청크 수)와 오늘 사용량을 비교해, 하루 한도를 넘기면 차단하고 분할을 안내한다.
# 파일을 직접 지정한 경우에도 inbox와 똑같이, 처리 후 원본을 컬렉션별 분류 폴더로 옮긴다.
def cmd_ingest(args) -> None:
    from db import document_store, sqlite_manager
    from pipeline import ingest

    import process_inbox

    collection = args.collection or settings.default_collection
    # --backend 미지정이면 설정 기본값(ingest_backend, 기본 ollama = 완전 로컬). 명시하면 그걸 우선한다.
    backend = getattr(args, "backend", None) or settings.ingest_backend
    # ollama(로컬 무료)·claude_cli/codex_cli(구독)는 Gemini 일일 한도(RPD)와 무관 → 한도 가드/사용량 기록을 건너뛴다.
    _is_gemini = backend in (None, "gemini")
    if not _is_gemini:
        print(f"추출 백엔드: {backend} (로컬/구독 — Gemini 하루 한도 미적용)")

    # 1) 처리 대상 파일 목록 결정
    if args.inbox:
        settings.inbox_dir.mkdir(parents=True, exist_ok=True)
        targets = sorted(p for p in settings.inbox_dir.iterdir() if p.is_file())
    else:
        if not args.files:
            print("처리할 파일을 지정하거나 --inbox 를 쓰세요.")
            return
        targets = []
        for f in args.files:
            path = Path(f)
            if not path.exists():
                print(f"파일을 찾을 수 없습니다: {f}")
                continue
            targets.append(path)
    if not targets:
        print("처리할 파일이 없습니다.")
        return

    # 2) 예상 요청 수(=청크 수)와 오늘 사용량 비교
    estimates = [
        (path, document_store.estimate_request_count(path.read_text(encoding="utf-8")))
        for path in targets
    ]
    # gleaning이 켜지면 청크당 최대 (1+glean)회 호출된다 → 예상/한도 계산에 곱해 반영한다.
    glean = getattr(args, "glean", 0) or 0
    multiplier = 1 + max(0, glean)
    total_est = sum(n for _, n in estimates) * multiplier
    used = sqlite_manager.get_api_usage_today()
    limit = settings.llm_daily_limit
    remaining = limit - used

    print(f"오늘 사용: {used}/{limit} (남은 한도 {max(0, remaining)})")
    if glean:
        print(f"gleaning {glean}라운드 → 청크당 최대 {multiplier}회 호출")
    for path, n in estimates:
        detail = f"{n} 요청" if multiplier == 1 else f"{n}청크 × {multiplier} = {n * multiplier} 요청"
        print(f"  - {path.name}: 예상 {detail}")
    print(f"이번 작업 합계: {total_est} 요청 → 처리 후 {used + total_est}/{limit}")

    if args.dry_run:
        print("(--dry-run: 실제 처리는 하지 않았습니다)")
        return

    # 3) 한도 가드 (Gemini만 — ollama/CLI는 로컬/구독이라 RPD 무관)
    if _is_gemini and used + total_est > limit and not args.force:
        print(f"\n⛔ 한도 초과 예상: {used} + {total_est} = {used + total_est} > {limit}  — 처리를 중단합니다.")
        _print_split_guidance(estimates, max(0, remaining), limit, multiplier)
        print("그래도 강행하려면 --force 를 붙이세요.")
        return

    # 4) 실제 처리
    if args.inbox:
        process_inbox.process_inbox(collection, glean_rounds=glean)
        _report_orphan_cleanup()
        _run_auto_merge(collection, args)
        return
    for path, _ in estimates:
        try:
            changed = ingest.process_file(path, collection, glean_rounds=glean, backend=backend)
        except Exception as exc:
            failed_dir = settings.failed_dir / collection
            failed_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), process_inbox._unique_destination(failed_dir, path.name))
            print(f"[{collection}] {path.name}: 처리 실패 — failed/{collection}/로 이동 ({exc})")
            continue
        processed_dir = settings.processed_dir / collection
        processed_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), process_inbox._unique_destination(processed_dir, path.name))
        status = "처리 완료" if changed else "변경 없음(이미 처리됨)"
        print(f"[{collection}] {path.name}: {status} — processed/{collection}/로 이동")
    _report_orphan_cleanup()
    _run_auto_merge(collection, args)


# 오늘 LLM 요청 사용량과 남은 한도를 출력한다.
def cmd_usage(args) -> None:
    from db import sqlite_manager

    used = sqlite_manager.get_api_usage_today()
    limit = settings.llm_daily_limit
    print(f"오늘 LLM 요청: {used}/{limit} (남은 한도 {max(0, limit - used)})")


# 문서를 컬렉션에서 삭제한다(벡터 청크 + 관계 + 문서 기록). 그로 인해 고립된 엔티티도 정리한다.
# 잘못된 컬렉션으로 추출했을 때 되돌리는 용도. 단, 다른 엔티티와 연결된 채 남은 노드는 자동 삭제되지 않는다.
def cmd_delete(args) -> None:
    from db import document_store, graph_manager, sqlite_manager

    collection = args.collection or settings.default_collection
    for f in args.files:
        name = Path(f).name
        ok = document_store.delete_document(collection, name)
        print(f"[{collection}] {name}: {'삭제됨' if ok else '문서를 찾을 수 없음'}")
    removed = graph_manager.cleanup_isolated_entities([collection])
    # [M2] 고립 엔티티 정리는 그래프 변이(엔티티 삭제)다 — delete_document가 이미 dirty를 세우지만,
    # 문서 삭제가 없었어도(no-op) cleanup이 기존 고립 엔티티를 지우면 커뮤니티 멤버십이 바뀌므로 여기서도 표시한다.
    if removed:
        sqlite_manager.mark_communities_dirty(collection)
    print(f"고립된 엔티티 {removed}개 정리됨")


# 한 컬렉션(사업)을 통째로 삭제한다(문서/벡터/엔티티/관계 전부). 잘못 넣은 사업을 깔끔히 비울 때 쓴다.
def cmd_delete_collection(args) -> None:
    from db import document_store

    count = document_store.delete_collection(args.collection)
    print(f"컬렉션 '{args.collection}' 삭제 완료 (문서 {count}개 + 엔티티/관계/청크)")


# [M4] 글로벌(map-reduce) 질의를 실행한다. 스코프 컬렉션 중 하나라도 커뮤니티가 stale(재빌드 필요 —
# 한 번도 안 빌드됐거나 마지막 빌드 이후 그래프가 바뀐 경우 모두 포함, is_communities_dirty가 두 경우를
# 함께 판정한다)이면 재빌드 안내를 먼저 보여준 뒤, 사용자가 질문에 대한 답 없이 끝나지 않도록 로컬
# 검색으로 즉시 폴백한다([ASSUMPTION] 안내만 하고 종료하는 대신 자동 로컬 폴백을 택함 — m4-result.md 근거).
def _cmd_query_global(args) -> None:
    from db import graph_manager, sqlite_manager
    from query import answer_question, answer_question_global

    collections = _collections_from_args(args)
    target = collections if collections is not None else sorted(
        set(sqlite_manager.get_collection_doc_counts()) | set(graph_manager.get_all_collections())
    )
    stale = [c for c in target if sqlite_manager.is_communities_dirty(c)]
    if stale:
        print(
            f"⚠️ 커뮤니티가 없거나 오래됨(재빌드 필요): {', '.join(stale)} "
            f"— 먼저 `graphrag communities build --collection <이름>`을 실행하세요."
        )
        print("(우선 로컬 검색으로 답합니다)")
        print(answer_question(args.question, collections=collections))
        return
    print(answer_question_global(args.question, collections=collections, level=args.level))


# 추출된 그래프+벡터 정보만 근거로 질문에 답한다. --mode global이면 커뮤니티 리포트(M3) 위에서
# map-reduce로 종합 답변한다(M4, _cmd_query_global). 기본(local)은 기존과 완전히 동일한 코드 경로다
# (hot-path 불변 — mode 분기가 없던 시절과 바이트 동일하게 answer_question을 호출한다).
def cmd_query(args) -> None:
    if getattr(args, "mode", "local") == "global":
        _cmd_query_global(args)
        return

    from query import answer_question

    print(answer_question(
        args.question, collections=_collections_from_args(args),
        backend=getattr(args, "backend", None),
    ))


# 그래프를 Gephi용 GEXF로 내보낸다. -o 로 경로를 지정하면 그곳에, 아니면 exports/에 저장한다.
def cmd_graph(args) -> None:
    from db import graph_manager
    from graph_export import build_gexf, export_graph

    collections = _collections_from_args(args)
    if args.output:
        entities = graph_manager.get_all_entities(collections)
        relations = graph_manager.get_all_relations(collections)
        Path(args.output).write_text(build_gexf(entities, relations), encoding="utf-8")
        print(f"GEXF 저장: {args.output} (엔티티 {len(entities)}개, 관계 {len(relations)}개)")
    else:
        print(f"GEXF 저장: {export_graph(collections)}")


# 지정한 컬렉션 범위의 현황(문서/엔티티/관계 수, 사용 타입·관계)을 출력한다.
def cmd_status(args) -> None:
    from db import document_store, graph_manager, sqlite_manager, vector_manager

    collections = _collections_from_args(args)
    scope = "전체" if collections is None else ", ".join(collections)
    print(f"=== 현황 (범위: {scope}) ===")
    print(f"문서 수: {sqlite_manager.count_documents(collections)}")
    print(f"벡터 청크 수: {vector_manager.count_chunks(collections)}")
    print(f"엔티티 수: {graph_manager.count_entities(collections)}")
    print(f"관계 수: {graph_manager.count_relations(collections)}")
    print(f"엔티티 타입: {', '.join(graph_manager.get_known_types(collections)) or '(없음)'}")
    print(f"관계 이름: {', '.join(graph_manager.get_known_predicates(collections)) or '(없음)'}")
    # 고아 데이터는 컬렉션 경계 없이 전역으로만 탐지된다(추적이 끊긴 데이터라 소속을 알 수 없음).
    orphan_count = len(document_store.find_orphaned_source_ids())
    if orphan_count:
        print(f"⚠️ 고아 데이터: {orphan_count}건 (전역) — ingest 시 자동 정리되거나 cleanup_db로 정리됩니다")


# 존재하는 모든 컬렉션과 각 문서/엔티티 수를 나열한다. 부모-자식 계층이 있으면 트리(들여쓰기)로 보여준다.
def cmd_collections(args) -> None:
    from db import graph_manager, sqlite_manager

    doc_counts = sqlite_manager.get_collection_doc_counts()
    all_collections = sorted(set(doc_counts) | set(graph_manager.get_all_collections()))
    if not all_collections:
        print("(아직 컬렉션이 없습니다)")
        return

    parent_of = sqlite_manager.list_collection_hierarchy()  # {자식: 부모}
    children_of: dict[str, list[str]] = {}
    for child, parent in parent_of.items():
        children_of.setdefault(parent, []).append(child)

    # 한 컬렉션과 그 자손을 들여쓰기로 출력한다.
    def _print_node(collection: str, depth: int) -> None:
        entity_count = graph_manager.count_entities([collection])
        indent = "  " * depth
        print(f"{indent}- {collection}: 문서 {doc_counts.get(collection, 0)}개, 엔티티 {entity_count}개")
        for child in sorted(children_of.get(collection, [])):
            _print_node(child, depth + 1)

    # 루트 = 부모가 없거나, 부모가 실제 컬렉션 목록에 없는 것(고아 부모 방지).
    roots = [
        c for c in all_collections
        if parent_of.get(c) is None or parent_of.get(c) not in all_collections
    ]
    for root in roots:
        _print_node(root, 0)


# 엔티티 자동 병합(컬렉션 내)을 실행한다.
def cmd_merge(args) -> None:
    from pipeline import entity_resolution

    entity_resolution.run(_collections_from_args(args))
    print("병합 작업 완료")


# 설명 후보가 min_candidates개 이상 쌓인 엔티티만 로컬 LLM(기본 Ollama)으로 통합 요약한다(옵트인 배치, M1.5).
def cmd_summarize_descriptions(args) -> None:
    from pipeline import desc_summarizer

    updated = desc_summarizer.summarize_descriptions(args.collection, min_candidates=args.min_candidates)
    print(f"[{args.collection}] 설명 통합 완료: {updated}개 엔티티")


# 컬렉션별 커뮤니티/리포트 현황(개수·재빌드 필요 여부)을 출력한다(communities status, M3).
# collections가 None이면 문서/그래프 어느 쪽에든 존재가 확인된 모든 컬렉션을 대상으로 한다.
def _print_communities_status(collections: list[str] | None) -> None:
    from db import graph_manager, sqlite_manager

    if collections is None:
        collections = sorted(
            set(sqlite_manager.get_collection_doc_counts()) | set(graph_manager.get_all_collections())
        )
    if not collections:
        print("(아직 컬렉션이 없습니다)")
        return
    for collection in collections:
        n_communities = len(sqlite_manager.get_communities(collection))
        n_reports = len(sqlite_manager.get_community_reports(collection))
        stale = " ⚠️ stale(재빌드 필요)" if sqlite_manager.is_communities_dirty(collection) else ""
        print(f"- {collection}: 커뮤니티 {n_communities}개, 리포트 {n_reports}개{stale}")


# 커뮤니티 탐지(Leiden, 컬렉션별) + 리포트 생성(M3)을 실행해 SQLite에 저장하거나, 현황을 출력한다.
# build: 탐지(순수 CPU)는 항상 실행하고, 그 위에 기본으로 리포트(LLM 배치)까지 생성한다(--no-reports로
# 건너뛸 수 있음). 탐지가 끝까지 완주했을 때만 dirty를 해제한다(크래시 안전 — 리포트 생성 단계의 개별
# 실패는 community_reporter가 해당 커뮤니티만 건너뛰므로 dirty 해제 여부에 영향을 주지 않는다).
# status: 컬렉션별 커뮤니티/리포트 개수와 stale 여부를 보여준다(--collection 생략 시 전체).
def cmd_communities(args) -> None:
    from db import sqlite_manager
    from pipeline import community_detector

    if args.action == "status":
        _print_communities_status(_collections_from_args(args))
        return

    if args.action == "build":
        if not args.collection:
            print("build는 --collection이 필요합니다.")
            return
        collection = args.collection
        print(f"[{collection}] 커뮤니티 탐지 중(Leiden, 순수 CPU)...")
        communities, graph_signature = community_detector.detect_communities(collection)
        sqlite_manager.replace_communities(collection, communities)
        sqlite_manager.clear_communities_dirty(collection, graph_signature)
        if not communities:
            print(f"[{collection}] 엔티티가 없어 커뮤니티가 생성되지 않았습니다.")
            return
        levels = sorted({c["level"] for c in communities})
        print(f"[{collection}] 커뮤니티 {len(communities)}개 저장 완료 (레벨 {levels})")

        if not args.no_reports:
            from pipeline import community_reporter

            print(f"[{collection}] 커뮤니티 리포트 생성 중(bottom-up, 레벨별 백엔드 라우팅 — 시간이 걸릴 수 있습니다)...")
            n_reports = community_reporter.generate_reports(collection)
            print(f"[{collection}] 리포트 {n_reports}개 생성 완료")


# "컬렉션:이름" 형식 인자를 (컬렉션, 이름)으로 가른다. 형식이 틀리면 None.
def _parse_ref(ref: str) -> tuple[str, str] | None:
    if not ref or ":" not in ref:
        return None
    collection, name = ref.split(":", 1)
    collection, name = collection.strip(), name.strip()
    return (collection, name) if collection and name else None


# 컬렉션을 넘는 SAME_AS 브릿지를 관리한다(add/remove/list/suggest). 같은 대상을 사업 간에 병합 없이 연결한다.
def cmd_bridge(args) -> None:
    from db import graph_manager

    if args.action == "list":
        bridges = graph_manager.list_all_bridges()
        if not bridges:
            print("(아직 브릿지가 없습니다)")
            return
        for b in bridges:
            print(f"- [{b['collection_a']}] {b['name_a']} ↔ [{b['collection_b']}] {b['name_b']}")
        return

    if args.action == "suggest":
        from pipeline import bridge

        candidates = bridge.find_bridge_candidates(args.threshold)
        if not candidates:
            print("(브릿지 후보가 없습니다)")
            return
        print("컬렉션 간 브릿지 후보(유사도 높은 순):")
        for coll_a, name_a, coll_b, name_b, score in sorted(candidates, key=lambda c: -c[4]):
            print(f"- [{coll_a}] {name_a} ↔ [{coll_b}] {name_b}  (유사도 {score * 100:.1f}%)")
        print("\n연결하려면: graphrag bridge add --from 컬렉션:이름 --to 컬렉션:이름")
        return

    # add / remove 는 --from, --to 가 필요하다.
    ref_a = _parse_ref(args.from_ref)
    ref_b = _parse_ref(args.to_ref)
    if not ref_a or not ref_b:
        print("--from/--to 는 '컬렉션:이름' 형식이어야 합니다 (예: --from 사업A:김변호사 --to 사업B:김변호사).")
        return

    if args.action == "add":
        ok = graph_manager.add_bridge(ref_a[0], ref_a[1], ref_b[0], ref_b[1])
        result = "연결됨" if ok else "실패(엔티티 없음 또는 동일 대상)"
        print(f"브릿지 {result}: [{ref_a[0]}] {ref_a[1]} ↔ [{ref_b[0]}] {ref_b[1]}")
    else:  # remove
        graph_manager.remove_bridge(ref_a[0], ref_a[1], ref_b[0], ref_b[1])
        print(f"브릿지 해제: [{ref_a[0]}] {ref_a[1]} ↔ [{ref_b[0]}] {ref_b[1]}")


# 컬렉션에 부모(본부)를 지정한다. 순환이 생기는 지정은 거부한다.
def cmd_set_parent(args) -> None:
    from db import sqlite_manager

    if args.child == args.parent:
        print("자기 자신을 부모로 지정할 수 없습니다.")
        return
    # 부모가 이미 자식의 자손이면, 이 지정은 순환을 만든다 → 거부.
    if args.parent in sqlite_manager.get_collection_descendants(args.child):
        print(f"순환이 생겨 거부합니다: '{args.parent}'은(는) 이미 '{args.child}'의 하위입니다.")
        return
    sqlite_manager.set_collection_parent(args.child, args.parent)
    print(f"계층 설정: '{args.child}' → 부모 '{args.parent}'")


# 컬렉션의 부모 지정을 해제한다.
def cmd_unset_parent(args) -> None:
    from db import sqlite_manager

    sqlite_manager.unset_collection_parent(args.child)
    print(f"계층 해제: '{args.child}'")


# 두 컬렉션의 답변 품질을 비교한다(질문 자동생성 + LLM 페어와이즈 심판).
# 추출 '수'가 아니라 실제 Q&A로 어느 쪽 그래프가 더 나은 답을 내는지 상대 비교한다.
def cmd_eval(args) -> None:
    import evaluate
    from db import graph_manager

    # 질문 생성용 발췌: --source가 있으면 원문 앞/중간/끝을 뽑고, 없으면 A 컬렉션의 엔티티로 구성한다.
    if args.source:
        text = Path(args.source).read_text(encoding="utf-8")
        mid = len(text) // 2
        sample = f"{text[:2500]}\n…\n{text[mid:mid + 2500]}\n…\n{text[-2500:]}"
    else:
        entities = graph_manager.get_all_entities([args.a])[:40]
        sample = "\n".join(
            f"- {e['name']} ({e.get('type')}): {e.get('description', '')}" for e in entities
        ) or "(내용 없음)"

    questions = evaluate.generate_questions(sample, n=args.questions, model=args.model)
    if not questions:
        print("질문 생성에 실패했습니다.")
        return
    print(f"질문 {len(questions)}개 생성:")
    for question in questions:
        print(f"  - {question}")

    print(f"\n[{args.a}] vs [{args.b}] 답변·심판 중... (질문당 답변 2회 + 심판 2회)")
    report = evaluate.compare_collections(args.a, args.b, questions, judge_model=args.model)
    wins = report["wins"]
    print(f"\n=== 결과: A=[{args.a}]  B=[{args.b}] ===")
    print(f"A 승 {wins['A']} · B 승 {wins['B']} · 무 {wins['tie']}  (총 {len(questions)})")
    for result in report["results"]:
        print(f"  [{result['winner']}] {result['question']}")
        print(f"       └ {result['reason']}")


# DB 전체를 백업한다.
def cmd_backup(args) -> None:
    import backup_db

    print(f"백업 완료: {backup_db.create_backup()}")


# 지정한 백업 zip으로 복원한다.
def cmd_restore(args) -> None:
    import restore_db

    restore_db.restore_backup(Path(args.archive))
    print("복원 완료")


# 서브커맨드 파서를 구성하고 인자를 디스패치한다.
def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(prog="graphrag", description="개인용 GraphRAG CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="DB 초기화")
    p_init.add_argument("--reset", action="store_true", help="기존 DB를 지우고 새로 만든다")
    p_init.set_defaults(func=cmd_init)

    p_ingest = sub.add_parser("ingest", help="문서 추출+그래프 생성")
    p_ingest.add_argument("files", nargs="*", help="처리할 파일 경로들")
    p_ingest.add_argument("--inbox", action="store_true", help="inbox/ 폴더를 일괄 처리")
    p_ingest.add_argument("--collection", help="대상 컬렉션(사업) 이름 (기본: default)")
    p_ingest.add_argument("--dry-run", action="store_true", help="처리 없이 예상 요청 수만 표시")
    p_ingest.add_argument("--force", action="store_true", help="하루 한도 초과 예상이어도 강행")
    p_ingest.add_argument("--no-merge", action="store_true", help="처리 후 엔티티 자동 병합을 건너뜀")
    p_ingest.add_argument(
        "--glean", type=int, default=0, metavar="N",
        help="청크당 gleaning(놓친 것 추가 추출) 라운드 수. 요청 수가 최대 (1+N)배로 늘어난다. 기본 0=끔",
    )
    p_ingest.add_argument(
        "--backend", choices=["gemini", "ollama", "claude_cli", "codex_cli"], default=None,
        help="추출 LLM 백엔드(기본: Gemini). ollama=로컬 무료(RPD 한도 미적용), claude_cli/codex_cli=구독.",
    )
    p_ingest.set_defaults(func=cmd_ingest)

    sub.add_parser("usage", help="오늘 LLM 요청 사용량/남은 한도").set_defaults(func=cmd_usage)

    p_delete = sub.add_parser("delete", help="문서 삭제(벡터/관계/기록 + 고립 엔티티 정리)")
    p_delete.add_argument("files", nargs="+", help="삭제할 파일명")
    p_delete.add_argument("--collection", help="대상 컬렉션 (기본: default)")
    p_delete.set_defaults(func=cmd_delete)

    p_delcol = sub.add_parser("delete-collection", help="컬렉션을 통째로 삭제(엔티티 포함)")
    p_delcol.add_argument("collection", help="삭제할 컬렉션 이름")
    p_delcol.set_defaults(func=cmd_delete_collection)

    p_query = sub.add_parser("query", help="질문하기")
    p_query.add_argument("question", help="질문 내용")
    p_query.add_argument("--collection", help="범위 컬렉션(쉼표로 여러 개)")
    p_query.add_argument("--all", action="store_true", help="전체 컬렉션 종합(행정 종합)")
    p_query.add_argument(
        "--mode", choices=["local", "global"], default="local",
        help="local(기본)=그래프+벡터 직접 검색, global=커뮤니티 리포트 위 map-reduce 종합 검색(M4, communities build 선행 필요)",
    )
    p_query.add_argument(
        "--level", type=int, help="글로벌 검색 전용: 조회할 커뮤니티 레벨(0=최상위, 미지정 시 설정 기본값)",
    )
    p_query.add_argument(
        "--backend", choices=["gemini", "ollama", "claude_cli", "codex_cli"], default=None,
        help="로컬 검색 답변 합성 백엔드(미지정 시 설정 기본=ollama 무과금). gemini는 키·RPD 한도 필요.",
    )
    p_query.set_defaults(func=cmd_query)

    p_graph = sub.add_parser("graph", help="Gephi용 GEXF 내보내기")
    p_graph.add_argument("--collection", help="범위 컬렉션(쉼표로 여러 개)")
    p_graph.add_argument("--all", action="store_true", help="전체 컬렉션")
    p_graph.add_argument("-o", "--output", help="저장할 파일 경로")
    p_graph.set_defaults(func=cmd_graph)

    p_status = sub.add_parser("status", help="DB 현황")
    p_status.add_argument("--collection", help="범위 컬렉션(쉼표로 여러 개)")
    p_status.add_argument("--all", action="store_true", help="전체 컬렉션")
    p_status.set_defaults(func=cmd_status)

    sub.add_parser("collections", help="컬렉션 목록").set_defaults(func=cmd_collections)

    p_merge = sub.add_parser("merge", help="엔티티 자동 병합(컬렉션 내)")
    p_merge.add_argument("--collection", help="범위 컬렉션(쉼표로 여러 개)")
    p_merge.add_argument("--all", action="store_true", help="전체 컬렉션")
    p_merge.set_defaults(func=cmd_merge)

    p_summarize = sub.add_parser(
        "summarize-descriptions", help="설명 후보를 로컬 LLM(Ollama)으로 통합 요약(옵트인 배치)"
    )
    p_summarize.add_argument("--collection", required=True, help="대상 컬렉션(사업) 이름")
    p_summarize.add_argument(
        "--min-candidates", type=int, dest="min_candidates",
        help="통합 요약을 트리거할 최소 후보 수(기본: 설정값)",
    )
    p_summarize.set_defaults(func=cmd_summarize_descriptions)

    p_communities = sub.add_parser(
        "communities", help="커뮤니티 탐지(Leiden) + 리포트 생성(M3) — 글로벌 검색은 이후 마일스톤"
    )
    p_communities.add_argument("action", choices=["build", "status"], help="동작")
    p_communities.add_argument(
        "--collection", help="대상 컬렉션(사업) 이름 (build는 필수, status는 생략 시 전체·쉼표로 여러 개)"
    )
    p_communities.add_argument(
        "--no-reports", action="store_true", help="build 시 리포트(LLM 배치) 생성을 건너뛰고 탐지만 수행"
    )
    p_communities.set_defaults(func=cmd_communities)

    p_bridge = sub.add_parser("bridge", help="컬렉션 간 같은 대상 연결(SAME_AS 브릿지)")
    p_bridge.add_argument("action", choices=["add", "remove", "list", "suggest"], help="동작")
    p_bridge.add_argument("--from", dest="from_ref", help="컬렉션:이름 (add/remove)")
    p_bridge.add_argument("--to", dest="to_ref", help="컬렉션:이름 (add/remove)")
    p_bridge.add_argument("--threshold", type=float, help="suggest 유사도 임계값(0~1)")
    p_bridge.set_defaults(func=cmd_bridge)

    p_setparent = sub.add_parser("set-parent", help="컬렉션에 부모(본부) 지정")
    p_setparent.add_argument("child", help="자식 컬렉션 이름")
    p_setparent.add_argument("parent", help="부모(본부) 컬렉션 이름")
    p_setparent.set_defaults(func=cmd_set_parent)

    p_unsetparent = sub.add_parser("unset-parent", help="컬렉션 부모 지정 해제")
    p_unsetparent.add_argument("child", help="자식 컬렉션 이름")
    p_unsetparent.set_defaults(func=cmd_unset_parent)

    p_eval = sub.add_parser("eval", help="두 컬렉션의 답변 품질 비교(질문 자동생성 + LLM 심판)")
    p_eval.add_argument("--a", required=True, help="비교 컬렉션 A")
    p_eval.add_argument("--b", required=True, help="비교 컬렉션 B")
    p_eval.add_argument("--questions", type=int, default=8, help="생성할 질문 수(기본 8)")
    p_eval.add_argument("--source", help="질문 생성용 원문 파일 경로(없으면 A의 엔티티로 생성)")
    p_eval.add_argument("--model", help="질문 생성·심판에 쓸 모델(기본: 설정 기본 모델)")
    p_eval.set_defaults(func=cmd_eval)

    sub.add_parser("backup", help="DB 백업").set_defaults(func=cmd_backup)
    p_restore = sub.add_parser("restore", help="백업 복원")
    p_restore.add_argument("archive", help="백업 zip 경로")
    p_restore.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
