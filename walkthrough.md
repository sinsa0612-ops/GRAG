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

## 5. 다음 단계 (미구현, 제안 트랙)
- 컬렉션 유기적 고도화: ① 공유 엔티티 `same_as` 브릿지 ② 교차 인사이트 질의 ③ 컬렉션 계층 — 격벽 유지하며 옵트인 연결. (implementation_plan.md 6장)
