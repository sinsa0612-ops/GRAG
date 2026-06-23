# graphrag 통합 CLI — 기존 기능(초기화/추출/질문/그래프/현황/병합/백업)을 하나의 명령으로 묶는 얇은 디스패처.
# 각 서브커맨드는 기존 모듈 함수를 그대로 호출한다. 컬렉션(사업) 범위는 --collection / --all 로 지정한다.
import argparse
import logging
import shutil
from pathlib import Path

from config import settings


# 조회 계열(query/graph/status/merge)에서 대상 컬렉션 범위를 정한다.
# --all 또는 아무것도 주지 않으면 None(전체 컬렉션 종합), --collection A,B면 그 목록으로 좁힌다.
def _collections_from_args(args) -> list[str] | None:
    if getattr(args, "all", False):
        return None
    raw = getattr(args, "collection", None)
    if raw:
        return [c.strip() for c in raw.split(",") if c.strip()]
    return None


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


# 파일들(또는 inbox/ 전체)을 지정한 컬렉션으로 추출·저장한다.
# 파일을 직접 지정한 경우에도 inbox와 똑같이, 처리 후 원본을 컬렉션별 분류 폴더로 옮긴다
# (성공 → processed/<컬렉션>/, 실패 → failed/<컬렉션>/).
def cmd_ingest(args) -> None:
    from pipeline import ingest

    import process_inbox

    collection = args.collection or settings.default_collection
    if args.inbox:
        process_inbox.process_inbox(collection)
        return
    if not args.files:
        print("처리할 파일을 지정하거나 --inbox 를 쓰세요.")
        return
    for f in args.files:
        path = Path(f)
        if not path.exists():
            print(f"파일을 찾을 수 없습니다: {f}")
            continue
        try:
            changed = ingest.process_file(path, collection)
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


# 문서를 컬렉션에서 삭제한다(벡터 청크 + 관계 + 문서 기록). 그로 인해 고립된 엔티티도 정리한다.
# 잘못된 컬렉션으로 추출했을 때 되돌리는 용도. 단, 다른 엔티티와 연결된 채 남은 노드는 자동 삭제되지 않는다.
def cmd_delete(args) -> None:
    from db import document_store, graph_manager

    collection = args.collection or settings.default_collection
    for f in args.files:
        name = Path(f).name
        ok = document_store.delete_document(collection, name)
        print(f"[{collection}] {name}: {'삭제됨' if ok else '문서를 찾을 수 없음'}")
    removed = graph_manager.cleanup_isolated_entities([collection])
    print(f"고립된 엔티티 {removed}개 정리됨")


# 한 컬렉션(사업)을 통째로 삭제한다(문서/벡터/엔티티/관계 전부). 잘못 넣은 사업을 깔끔히 비울 때 쓴다.
def cmd_delete_collection(args) -> None:
    from db import document_store

    count = document_store.delete_collection(args.collection)
    print(f"컬렉션 '{args.collection}' 삭제 완료 (문서 {count}개 + 엔티티/관계/청크)")


# 추출된 그래프+벡터 정보만 근거로 질문에 답한다.
def cmd_query(args) -> None:
    from query import answer_question

    print(answer_question(args.question, collections=_collections_from_args(args)))


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
    from db import graph_manager, sqlite_manager, vector_manager

    collections = _collections_from_args(args)
    scope = "전체" if collections is None else ", ".join(collections)
    print(f"=== 현황 (범위: {scope}) ===")
    print(f"문서 수: {sqlite_manager.count_documents(collections)}")
    print(f"벡터 청크 수: {vector_manager.count_chunks()}")
    print(f"엔티티 수: {graph_manager.count_entities(collections)}")
    print(f"관계 수: {graph_manager.count_relations(collections)}")
    print(f"엔티티 타입: {', '.join(graph_manager.get_known_types(collections)) or '(없음)'}")
    print(f"관계 이름: {', '.join(graph_manager.get_known_predicates(collections)) or '(없음)'}")


# 존재하는 모든 컬렉션과 각 문서/엔티티 수를 나열한다.
def cmd_collections(args) -> None:
    from db import graph_manager, sqlite_manager

    doc_counts = sqlite_manager.get_collection_doc_counts()
    all_collections = sorted(set(doc_counts) | set(graph_manager.get_all_collections()))
    if not all_collections:
        print("(아직 컬렉션이 없습니다)")
        return
    for collection in all_collections:
        entity_count = graph_manager.count_entities([collection])
        print(f"- {collection}: 문서 {doc_counts.get(collection, 0)}개, 엔티티 {entity_count}개")


# 엔티티 자동 병합(컬렉션 내)을 실행한다.
def cmd_merge(args) -> None:
    from pipeline import entity_resolution

    entity_resolution.run(_collections_from_args(args))
    print("병합 작업 완료")


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
    p_ingest.set_defaults(func=cmd_ingest)

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

    sub.add_parser("backup", help="DB 백업").set_defaults(func=cmd_backup)
    p_restore = sub.add_parser("restore", help="백업 복원")
    p_restore.add_argument("archive", help="백업 zip 경로")
    p_restore.set_defaults(func=cmd_restore)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
