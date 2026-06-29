# 구현 계획서 — 컬렉션 유기적 고도화 ①②③ (격벽 유지 + 옵트인 연결)

## 1. 목적 / 배경
- 사업(컬렉션) 격벽은 유지하되, 필요할 때만 '문'을 내어 종합 통찰이 가능하게 한다.
- 모두 **추가형**(기존 데이터/질의/격벽 무변경)이며 **옵트인**(명시 연결)이라 무관한 사업이 자동으로 엮이지 않는다.

## 2. 구조 사전 이해 보고서 (3줄)
1. 엔티티 정체성 `(collection,name)` 격벽은 그대로 두고, 그 위에 ①컬렉션을 넘는 유일 연결선 SAME_AS ②종합 시 걸침을 표면화하는 질의 ③컬렉션 부모-자식 메타를 추가한다.
2. 새 어휘는 DAL(`db/*`)로만 접근, 기존 `RELATION` 질의·백업 경로에 영향 없음(별도 rel 타입/별도 테이블).
3. 책임 분리: 브릿지=Kuzu, 계층=SQLite, 교차 인사이트=query 로직.

## 3. 설계

### ① 공유 엔티티 SAME_AS 브릿지 (db/graph_manager.py, pipeline/bridge.py, CLI)
- Kuzu에 `CREATE REL TABLE SAME_AS (FROM Entity TO Entity)` 추가(속성 없음). RELATION과 다른 타입이라 기존 질의에 안 걸린다.
- 무방향 중복 방지: 두 엔티티 id를 정렬해 `lo -[:SAME_AS]-> hi` 한 방향으로만 저장(조회는 양방향 합집합).
- 함수: `add_bridge / remove_bridge / get_bridges / list_all_bridges / is_bridged`. 양쪽 엔티티가 실제 존재할 때만 연결.
- 제안: `pipeline/bridge.py.find_bridge_candidates(threshold)` — **서로 다른 컬렉션** 엔티티를 임베딩 유사도로 비교해 후보만 반환(파괴적 병합 아님, 실제 연결은 사용자 결정). 이미 브릿지된 쌍은 제외.
- 설정: `config.bridge_similarity_threshold = 0.90`(교차 동일 대상은 약간 느슨하게).

### ② 교차 인사이트 질의 (query.py)
- `_gather_graph_context`에서 매칭 엔티티마다 `get_bridges(범위 내)`를 따라가, 같은 대상이 2개 이상 사업에 걸치면 "※ N개 사업에 걸쳐 있음" 표시 + 브릿지된 쌍의 관계도 컨텍스트에 포함.
- **걸침 판정은 '브릿지(명시)'만 사용** — 단순 동명이인을 같은 사람으로 오인하지 않게(기존 동명이인 방어 원칙 유지).
- 스코프 존중: `--all`이면 전 컬렉션 횡단, `--collection A,B`면 그 범위 내 걸침만. 스코프 밖은 절대 끌어오지 않음.

### ③ 컬렉션 계층 (db/sqlite_manager.py, CLI)
- `collection_meta(collection TEXT PK, parent TEXT)` 추가.
- 함수: `set_collection_parent / unset_collection_parent / get_collection_parent / get_collection_children / get_collection_descendants(자기+하위, 순환 보호) / list_collection_hierarchy`.
- CLI `_collections_from_args`가 `--collection 본부`를 자손까지 펼쳐 범위 결정. 순환 생성은 거부.

### CLI 추가
- `graphrag bridge add|remove|list|suggest [--from C:N --to C:N] [--collection] [--threshold]`
- `graphrag set-parent <child> <parent>`, `graphrag unset-parent <child>`
- `graphrag collections`를 트리(들여쓰기)로 표시하도록 보강.

## 4. 영향 파일
- 코드: `config.py`, `db/graph_manager.py`, `db/sqlite_manager.py`, `query.py`, `graphrag_cli.py`, 신규 `pipeline/bridge.py`.
- 테스트: `test_graph_manager.py`(브릿지), 신규 `test_bridge.py`(후보), `test_query.py`(교차 인사이트), `test_sqlite_manager.py`(계층), `test_graphrag_cli.py`(스코프 확장).

## 5. 검증
- `pytest` 전체 그린 + 브릿지/계층/교차 인사이트 신규 케이스.
- 스모크: bridge add→query --all에서 걸침 표시, set-parent→status --collection 본부가 자손 포함.

## 6. 알려진 한계(허용)
- 컬렉션 내 병합(merge_entity_into)으로 사라지는 노드가 브릿지를 갖고 있었다면 그 브릿지는 유실(드묾) — 추후 보강 대상.
- 브릿지 후보 탐색은 전 엔티티 O(N²) 비교(개인 규모 허용).
