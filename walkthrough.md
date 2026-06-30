# 검증서 — 가성비 품질·정합성 보강

## 1. 무엇이 바뀌었나 (기능 체감)
- **추출 품질 ↑:** 문서를 자를 때 문장/문단 경계를 우선해, 관계가 문장 한가운데서 끊기지 않는다.
- **관계 이름 정돈:** `works at`·`Works-At` 같은 표기 차이를 `WORKS_AT`로 통일하고, 자주 쓰는 관계를 프롬프트에 예시로 권장해 같은 관계가 제각각 만들어지는 일을 줄인다.
- **현황 숫자 정합:** `status --collection A`의 청크 수가 이제 해당 사업만 센다(이전엔 전체를 셌음). 고아 데이터가 있으면 현황에 건수가 뜬다.
- **자동 청소:** `ingest`가 끝나면 처리 중 끊긴 고아 데이터를 자동으로 한 번 정리한다.
- **사업 격벽 일관성:** 병합 금지 목록이 사업(컬렉션)별로 격리된다(한 사업 규칙이 다른 사업으로 새지 않음).
- **백업 안전성:** 백업/복원 직전 ChromaDB도 닫아, 쓰다 만 상태로 복사되거나 Windows 파일 락이 걸리는 것을 막는다.
- **한도 카운터 정확도:** 일일 사용량 집계 날짜를 Gemini 실제 리셋 기준(태평양시간)에 맞춘다.

## 2. 변경 파일
- 코드: `db/document_store.py`(청킹), `db/vector_manager.py`(범위 count·close), `db/sqlite_manager.py`(블랙리스트 스키마/마이그레이션·태평양 날짜), `schemas.py`(predicate 정규화), `pipeline/ingest.py`(시드 관계·예시 프롬프트), `pipeline/entity_resolution.py`(블랙리스트 컬렉션 전달), `graphrag_cli.py`(status·ingest), `backup_db.py`·`restore_db.py`(Chroma close), `requirements.txt`(tzdata 고정).
- 테스트: `test_sqlite_manager.py`, `test_entity_resolution.py`, `test_schemas.py`, `test_document_store.py`, `test_vector_manager.py`, `test_pipeline_ingest.py`.

## 3. 검증 결과
- `pytest` 전체 **116 passed**.
- 스모크: `graphrag init`(블랙리스트 자동 마이그레이션 포함) → 정상, `graphrag status --all` → 정상 출력.
- 실DB 확인: `merge_blacklist` 컬럼 = `[collection, node_a, node_b, reason]`, 태평양 날짜(2026-06-28)가 로컬(2026-06-29)과 다르게 산출됨(수정 유효), `vector_manager.close()` 무오류.

## 4. 알려진 한계 / 비고
- 포매터(ruff/black)가 환경에 미설치되어 자동 정렬은 생략(코드는 주변 스타일을 따름).
- predicate는 '닫힌 강제'가 아니라 '시드+힌트(열린)' 정책 — 새로운 관계도 허용하되 형식만 통일한다.
- 고아 데이터 탐지는 전역(소속 불명)이라 컬렉션 범위와 무관하게 표시/정리된다.

## 5. 컬렉션 유기적 고도화 ①②③ (격벽 유지 + 옵트인 연결) — 구현 완료

### 기능 체감
- **① 공유 엔티티 브릿지:** 서로 다른 사업에 흩어진 같은 대상(예: 같은 거래처)을 **병합 없이** 연결한다. 각 사업 안에서는 그대로 격리돼 보이고, 종합할 때만 다리를 건넌다.
- **② 교차 인사이트:** `--all`(또는 두 사업 범위) 질문에서 브릿지로 이어진 대상이 **"N개 사업에 걸쳐 있습니다"**라고 표시되고, 양쪽 사업에서의 관계가 함께 답변 근거가 된다. 단순 동명이인은 섞지 않는다(명시적 브릿지만 사용).
- **③ 컬렉션 계층:** 사업을 '본부' 아래 묶고, `status/query --collection 본부`가 자손 사업까지 자동 포함한다. `collections`는 트리로 표시된다.

### 새 명령
- `graphrag bridge add|remove|list|suggest [--from 사업:이름 --to 사업:이름] [--threshold]`
- `graphrag set-parent <자식> <부모>`, `graphrag unset-parent <자식>`

### 변경/추가 파일
- 코드: `config.py`(브릿지 임계값), `db/graph_manager.py`(SAME_AS CRUD), `db/sqlite_manager.py`(collection_meta·계층 함수), `query.py`(교차 인사이트), `graphrag_cli.py`(명령·범위 펼침·트리), 신규 `pipeline/bridge.py`(후보 제안).
- 테스트: `test_graph_manager.py`, 신규 `test_bridge.py`, `test_query.py`, `test_sqlite_manager.py`, `test_graphrag_cli.py`.

### 검증
- `pytest` 전체 **128 passed**.
- 스모크: `init`(SAME_AS·collection_meta 스키마 추가) → 정상, `bridge add`→`bridge list` 표시, `set-parent`→순환 지정 **거부** 동작 확인, `unset-parent` 정리.

### 설계 결정 / 한계
- 브릿지 = Kuzu의 별도 rel 타입(기존 RELATION 질의 무영향), 계층 = SQLite 메타, 교차 인사이트 = 순수 질의 로직(책임 분리).
- 걸침 판정은 **명시적 브릿지만** 사용(동명이인 오인 방지). 스코프 밖 사업은 절대 끌어오지 않음.
- 컬렉션 내 병합으로 사라지는 노드가 브릿지를 갖고 있었다면 그 브릿지는 유실(드묾, 추후 보강 대상).

## 6. 잠수함 문서 추출 품질 개선 (Tier 1~3) — 구현·검증 완료 / 재인덱싱 보류

### 배경
- `잠수함 기술개발` 컬렉션 1건 진단: 엔티티 989/관계 2183이나 ①핵심 주제 "연료전지" 4분할 ②`merge` 미실행+설명문 오염으로 병합 불가(실측 '연료전지 시스템'~'연료전지시스템' 0.83) ③잡티 엔티티 ~151개(특허번호·코드·주소·긴제목) ④과잉 허브·`RELATED_TO` 13%·원본 표 노이즈 51%.

### 기능 체감
- **추출이 더 깔끔해진다(다음 인덱싱부터):** 특허번호·과제코드·주소·긴 제목이 엔티티로 안 잡히고, 의미 없는 `RELATED_TO`와 장비-주제 오연결이 줄어든다.
- **같은 대상이 한 노드로 모인다:** "연료전지 시스템"/"연료전지시스템" 같은 표기 변형이 자동 병합된다. `ingest` 종료 시 자동 실행(끄려면 `--no-merge`).
- **인덱싱 비용 절반:** 변환 문서의 빈 표 격자를 걷어내 청크 수가 준다(잠수함 문서 378→191).

### 변경 파일
- `db/document_store.py`: `clean_markdown()` 신규. `estimate_request_count`가 정리본 기준.
- `pipeline/ingest.py`: `process_file`가 청킹 직전 `clean_markdown`(해시·원본은 raw 유지). 프롬프트에 품질 규칙 추가.
- `pipeline/entity_resolution.py`: `_normalize_name`/`find_normalized_duplicates`(무료 정규화 병합), 임베딩 입력 이름만, `run`이 정규화→임베딩 순.
- `graphrag_cli.py`: `ingest` 후 `_run_auto_merge` 자동 호출 + `--no-merge`.
- 테스트: `test_document_store.py`(+3), `test_entity_resolution.py`(+4).

### 검증
- `pytest` 전체 **135 passed**(기존 128 + 신규 7).
- 무료 병합 실증(비용 0): 엔티티 989→940, 관계 2183→2132, 54쌍 병합. `연료전지 시스템` 98→148, `숭실대학교` 35→73. 위험 부분집합·유사도 미달쌍은 보존.
- 표 정리 실증: `ingest --dry-run` 378→191.

### 잔여
- 전체 재인덱싱은 오늘 한도(380/500 사용) 초과로 **보류** → 리셋 후 1회 실행(`HANDOFF-sub.md`).
