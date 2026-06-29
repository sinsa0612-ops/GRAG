# 구현 계획서 — 가성비 품질·정합성 보강 + 컬렉션 유기적 고도화(제안)

## 1. 목적 / 배경
- 추출 품질을 "가성비"(무료 모델·청크당 1회 유지) 범위 안에서 끌어올리고, 코드 리뷰에서 드러난 DB 정합성 틈을 막는다.
- 컬렉션 격벽은 유지하되, 사업끼리 더 유기적으로 연결할 수 있는 방안을 별도 트랙으로 제안한다(이번엔 구현 아님).

## 2. 구조 사전 이해 보고서 (3줄 요약)
1. **근본 원인:** 글자수 단순 분할(문맥 절단), 자유 predicate(어휘 파편화), 컬렉션 필터 누락(status 청크 수·병합 블랙리스트)이 품질·정합성을 갉아먹는다.
2. **연계 흐름/규칙:** 모든 변경은 DAL(`db/*`)·어댑터 격리·컬렉션 격벽 원칙 안에서, 기존 인터페이스의 폭발 반경을 최소화해 적용한다.
3. **정석 해결:** 분할기는 문장/문단 경계 인식으로, predicate는 시드 목록+형식 정규화로, 집계·블랙리스트는 컬렉션 인자를 일관 적용한다.

## 3. 변경 항목별 설계

### A. 문장 경계 청킹 (가성비 핵심, 추출 품질 ↑)
- `document_store.chunk_text`를 글자수 단순 절단 → **경계 우선 분할**로 교체.
- 구분자 우선순위(문단 `\n\n` → 줄 `\n` → 문장부호 `. ! ? 。` → 공백)로 자르고, chunk_size/overlap는 그대로 존중.
- 구분자가 전혀 없는 텍스트는 기존처럼 글자수로 폴백 → 기존 의미 유지.
- **결정 필요(질문 1):** 자체 구현(무의존, KISS) vs `langchain-text-splitters`(가벼운 단독 패키지) 도입.

### B. predicate 어휘 안정화 (가성비 핵심)
- (1) `schemas.ExtractedRelation`에서 predicate를 **대문자 스네이크케이스로 정규화**(예: `works at`→`WORKS_AT`).
- (2) 프롬프트에 **시드 추천 predicate 목록 + few-shot 예시 1개** 삽입(이른 문서의 파편화 방지).
- **결정 필요(질문 2):** EntityType처럼 **닫힌 목록 강제**(미존재→RELATED_TO) vs **시드+힌트(열린)** 유지.

### C. status 청크 수 컬렉션 필터 (정합성 버그)
- `vector_manager.count_chunks(collections=None)`로 시그니처 확장(Chroma metadata 필터).
- `graphrag_cli.cmd_status`가 범위를 그대로 전달 → 다른 집계와 숫자 일치.

### D. 고아 데이터 가시화 (정합성)
- **결정 필요(질문 3):** `status`에 고아 건수만 경고 표시 vs `ingest` 종료 후 자동 청소 vs 둘 다.

### E. 병합 블랙리스트 컬렉션 격벽 (스키마 변경)
- `merge_blacklist`에 `collection` 추가, PK `(collection, node_a, node_b)`로 재구성.
- SQLite는 PK 변경 불가 → 신규 테이블 생성·기존 데이터를 `default` 컬렉션으로 이관·교체(자동 마이그레이션).
- `is_merge_blacklisted`/`add`/`remove`/`list`에 collection 인자 추가, `entity_resolution`이 컬렉션 전달.
- ⚠️ 유일한 DB 스키마 변경 → 확답 후 진행.

### F. 백업 시 ChromaDB 잠재우기 (잠재 정합성)
- `vector_manager.close()` 추가(클라이언트 해제), `backup_db`/`restore_db`가 교체 직전 호출.

### G. 일일 사용량 날짜 기준 정합 (소소)
- `api_usage`의 날짜 키를 로컬 → **태평양시간 날짜**(Gemini 실제 리셋 기준)로 맞춤(`zoneinfo`, 표준 라이브러리).

## 4. 영향 파일
- 코드: `db/document_store.py`, `db/vector_manager.py`, `db/sqlite_manager.py`, `schemas.py`, `pipeline/ingest.py`, `pipeline/entity_resolution.py`, `graphrag_cli.py`, `backup_db.py`, `restore_db.py`.
- 테스트: `test_document_store.py`(청킹), `test_sqlite_manager.py`(블랙리스트), `test_pipeline_ingest.py`(predicate/프롬프트), `test_vector_manager.py`(count), 신규 케이스 추가.

## 5. 검증
- `ruff format` 후 `pytest` 전체 그린.
- 청킹·predicate 정규화·블랙리스트 마이그레이션은 인메모리 격리 테스트로 회귀 방지.

## 6. 컬렉션 유기적 고도화 (별도 트랙 — 이번엔 제안만)
- **공유 엔티티 레이어:** 여러 사업에 공통 등장하는 인물/거래처를 병합 없이 `same_as` 브릿지로 연결(격벽 유지 + 종합 시 횡단).
- **교차 인사이트 질의:** `--all` 종합 시 "한 인물이 여러 사업에 걸쳐 있음"을 자동 표면화.
- **컬렉션 계층:** 상위 '행정' 아래 사업들을 트리로 묶어 부분/전체 범위를 선택.
- 모두 옵트인(명시적 연결)으로 설계해 무관한 사업의 자동 오염을 원천 차단.
