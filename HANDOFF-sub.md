# HANDOFF-sub.md (실무 변경 로그 / 다음 작업)

> AI 주도권 문서. 세션별 세부 변경 이력·현재 상태·미해결 문제·다음 단계를 기록한다.

## 📅 2026-06-30 세션 — 잠수함 문서 추출 품질 개선 (Tier 1~3)

### 진단 (실DB 점검)
- `잠수함 기술개발` 1건: 엔티티 989/관계 2183. 양은 충분, 질이 희석.
- 근본 원인: ①프롬프트가 식별자/긴제목/약한관계 미차단 ②`merge` 미실행(alias 0/989) + 설명문 오염으로 병합 불가(이름+설명 0.83 vs 이름만 0.98) ③원본 표 노이즈 51%로 청크 예산 낭비.

### 변경 (✅ 구현·검증 완료 / ⏳ 커밋 대기)
- **Tier 1 프롬프트(`pipeline/ingest.py`):** 순수 식별자(특허번호·코드·주소)·긴제목 엔티티 금지, 짧은 표준명, `RELATED_TO` 억제, 장비-주제 오연결 방지 규칙 추가.
- **Tier 2 병합(`pipeline/entity_resolution.py`):** `_normalize_name`/`find_normalized_duplicates`(공백·기호만 다른 표기 무료 병합), 임베딩 입력 이름만(설명 제외), `run`이 정규화→임베딩 순. `graphrag_cli.py`에 `ingest` 후 자동 병합 + `--no-merge`.
- **Tier 3 표 전처리(`db/document_store.py`):** `clean_markdown()` 신규(빈 표셀/구분선 제거), `process_file`가 청킹 직전 적용(해시·원본은 raw 유지), `estimate_request_count`도 정리본 기준.
- 테스트: `test_document_store.py`(+3), `test_entity_resolution.py`(+4).

### 현재 상태
- `pytest` 전체 **135 passed**.
- 무료 병합 1회 실행(비용 0): 엔티티 989→**940**, 관계 2183→**2132**, 54쌍 병합. `연료전지 시스템` 98→148(파편 흡수), `숭실대학교` 35→73.
- 표 정리 실증: `ingest --dry-run` 378→**191**청크.
- 이번 세션 변경분 8개 파일 **uncommitted**. (M2는 `6c47b84`로 이미 커밋됨 — 직전 메모의 "M2 미커밋"은 오기였음.)
- `HANDOFF-main.md`/`HANDOFF-sub.md`는 git **untracked**(한 번도 add 안 됨). `.tmp.drive*/`도 untracked.

### ⚠️ 알려진 문제
1. 이름만 임베딩이 특허번호 같은 ID성 문자열을 과병합(예: `10-2190943`←`10-2190941`). Tier 1이 재인덱싱 시 ID를 애초에 추출 안 해 자연 해소됨.
2. `연료전지`(성분)와 `연료전지 시스템`(시스템)은 의도적으로 분리 유지.

### ▶️ 다음 단계 (Next Steps)
1. **★ 한도 리셋(태평양시간 자정) 후 전체 재인덱싱:**
   `graphrag delete-collection "잠수함 기술개발"` → `graphrag ingest "processed/잠수함 기술개발/잠수함 사업계획서.md" --collection "잠수함 기술개발"` (자동 병합 포함, 예상 191호출).
   재인덱싱 후 `db_status.py`로 잡티(특허번호 OTHER)·고립·연료전지 단일허브화 재확인.
2. 커밋(add 목록): `db/document_store.py pipeline/ingest.py pipeline/entity_resolution.py graphrag_cli.py tests/test_document_store.py tests/test_entity_resolution.py implementation_plan.md walkthrough.md`.

## 📅 2026-06-29 세션

### 트랙 1 — 가성비 품질·정합성 보강 (✅ 커밋 완료: `d3b4e51`)
1. 문장/문단 경계 우선 청킹(`document_store.chunk_text`, 자체 구현·무의존).
2. predicate 형식 정규화(대문자 스네이크) + 시드 관계·few-shot 프롬프트(`schemas.py`, `pipeline/ingest.py`).
3. `status` 벡터 청크 수 컬렉션 범위 반영(`vector_manager.count_chunks(collections)`).
4. `ingest` 후 고아 데이터 자동 정리 + `status` 고아 건수 경고(`graphrag_cli.py`).
5. 병합 블랙리스트 컬렉션 격벽 + 자동 마이그레이션(`sqlite_manager`).
6. 백업/복원 전 ChromaDB 닫기(`vector_manager.close()`, `backup_db`, `restore_db`).
7. 일일 사용량 날짜 = 태평양시간(`sqlite_manager`, `requirements.txt`에 `tzdata` 고정).

### 트랙 2 — 컬렉션 유기적 고도화 ①②③ (✅ 구현·검증 완료 / ⏳ 커밋 대기)
- ① SAME_AS 브릿지: Kuzu 별도 rel 타입, `add/remove/get/list/is_bridged`(`graph_manager`), 후보 제안 `pipeline/bridge.py`, 설정 `config.bridge_similarity_threshold=0.90`.
- ② 교차 인사이트: `query._gather_graph_context`가 브릿지로 이어진 대상의 '걸침'과 양쪽 관계 표면화(명시 브릿지만, 스코프 존중).
- ③ 컬렉션 계층: `sqlite_manager.collection_meta` + 계층 함수, `graphrag_cli` 범위 펼침/트리/`set-parent`·`unset-parent`.
- CLI 신규: `graphrag bridge add|remove|list|suggest`, `set-parent`, `unset-parent`.

### 현재 상태
- `pytest` 전체 **128 passed**.
- 트랙 2 변경분 **uncommitted**. 커밋 add 목록·문구는 `walkthrough.md` 및 직전 대화 참조.

## ⚠️ 알려진 문제 (Known Issues)
1. **`d3b4e51`이 `config.py`(`llm_daily_limit`) 누락** — 이미 커밋된 `graphrag_cli.py`가 이 값에 의존. 다음 커밋에 `config.py`·`query.py`·`USAGE.md` 포함하면 완결됨(작업 트리는 정상).
2. 컬렉션 내 병합(`merge_entity_into`)으로 사라지는 노드가 SAME_AS 브릿지를 갖고 있었다면 그 브릿지는 유실(드묾).
3. 포매터(ruff/black) 미설치 → 자동 정렬 생략(코드는 주변 스타일 준수).
4. `.tmp.driveupload/`(구글 드라이브 임시 폴더)가 untracked로 남아 있음.

## ▶️ 다음 단계 (Next Steps)
1. 트랙 2 커밋(아래 add 목록): `pipeline/bridge.py tests/test_bridge.py config.py db/graph_manager.py db/sqlite_manager.py graphrag_cli.py query.py USAGE.md implementation_plan.md walkthrough.md tests/test_graph_manager.py tests/test_graphrag_cli.py tests/test_query.py tests/test_sqlite_manager.py`.
2. `.gitignore`에 `.tmp.driveupload/` 추가.
3. (선택) `ruff` 설치 후 `ruff format` 일괄 적용.
4. (선택) 브릿지 유실 보강 — `merge_entity_into`가 SAME_AS도 keep 노드로 이전하도록.
5. (선택) `bridge suggest` 결과를 한 번에 확정하는 인터랙션(현재는 제안 → 수동 add).
