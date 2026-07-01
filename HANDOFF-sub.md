# HANDOFF-sub.md (실무 변경 로그 / 다음 작업)

> AI 주도권 문서. 세션별 세부 변경 이력·현재 상태·미해결 문제·다음 단계를 기록한다.

## 📅 2026-07-01 세션 — 모델 토글(Gemma) + gleaning 개발 + A/B

### 배경/목표
- MS GraphRAG의 gleaning(청크 다회독으로 recall↑)을 우리 RPD 안에서 가능케 하려고, 무료 Gemma(RPD 1.5k)로 바꿀 수 있는지 검토 → 모델 토글 구현 후 flash vs gemma A/B → gleaning 개발.

### 변경 (✅ 구현·검증 완료 / ⏳ 커밋 대기)
- **모델 토글:** `adapters/llm_adapter.generate(..., model=None)` + `_supports_structured_output`(Gemma는 스키마 off)·`_request_interval`(Gemma 4.5초). `config.gemma_request_interval_sec=4.5`. `pipeline/ingest.extract_chunk/process_file`에 `model` 인자. 기본값=기존 동작.
- **gleaning:** `config.glean_rounds=0`(기본 끔). `ingest.glean_chunk(chunk, base, rounds, model)` — 라운드마다 '이미 찾은 엔티티' 주고 놓친 것만 요청, dedupe, 새 게 없으면 조기 종료, 추가 호출 수 반환. `process_file`이 `rounds>0`일 때 호출하고 추가 사용량 기록. `process_inbox`·`graphrag_cli --glean N`(예상/가드 (1+N)배 반영, 분할안내 배수화)·`app.py`(gleaning 라운드 입력) 배선. 추출 프롬프트에 "순수 JSON만" 지시 추가(무스키마 신뢰도↑, 두 모델 공통).
- 테스트 `tests/test_pipeline_ingest.py` +3.

### A/B 결과 (크리스마스 캐럴; flash 91청크·gemma 60청크 부분)
- recall: gemma 청크당 엔티티 3.0→7.5, 관계 5.1→12.0(2.4~2.5배). 배경/지명/연도 디테일 다수.
- 품질 비슷(RELATED_TO 26.7%↔25.9%, 잡티 0). gemma는 `영국` 과잉 허브 경향.
- **속도: flash 6.7s/청크 vs gemma ~75~130s/청크**(무료 지속부하 throttling). 순수 API 왕복 실측 74.7s·121.3s → 느림은 API 자체 지연, **우리 호출 간격 설정은 정상**(사용자 문의 확인 완료).
- 결론: gemma=최대 recall 배치용 토글 유지, 일상 recall은 flash+gleaning 권장.

### 현재 상태
- `pytest` **138 passed**. `--glean 2 --dry-run` 배수·가드 정확.
- 테스트로 만든 `캐럴-flash`(91)/`캐럴-gemma`(60) 컬렉션이 실DB에 남음(GUI 그래프 비교용, 불필요 시 `delete-collection`).
- 오늘 사용량 364/500(A/B·프로브 소비). 스크래치패드 A/B 스크립트는 비커밋.

### ⚠️ 알려진 문제
- gemma 무료 등급은 API 지연(~75~130초/콜)이 커서 대형 문서엔 시간 비현실적. 우리 코드로는 못 줄임(간격은 하한, 병목은 API).
- gemma는 스키마 미지원이라 무스키마 JSON 파싱 실패 소폭(A/B 60청크 중 1건, ~1.7%).

### 🐛 gleaning 필드명 버그 (발견·수정)
- 증상: gemma glean 결과가 +0%로 보였으나, 실은 gleaning 응답의 필드명이 name→id/text/entity, source/target→subject/object로 어긋나 Pydantic 검증에서 전부 버려진 것(gemma는 무스키마라 프롬프트만 의존; flash는 구조화 출력이 필드명을 강제해 무사).
- 수정: ①`_GLEAN_PROMPT`에 필드명 예시 명시 ②`_parse_extraction`에 `_normalize_field_names`(변형 키→표준 키, response_schema는 불변) ③테스트 +1(실측 실패 재현). `pytest` ingest 20 passed(전체 139 passed).
- 재측정(전권 91청크·병렬 5워커·58분·182콜): gemma glean1 = 엔티티 **+43.6%** / 관계 **+46.9%**, 91/91 청크. flash glean1(+46.3%/+53.9%)과 유사 → **gleaning은 두 모델 모두 유효**. 앞선 "gemma는 gleaning 이득 0" 결론은 이 버그 탓이었고 **철회**.
- 잔여 관찰: gemma glean은 RELATED_TO 32.1%(flash glean1 20%보다 높아 관계가 덜 구체적) + 날짜성 잡티 1건. gemma는 recall 최고지만 느려서(58분/권) 최대 recall용, 일상은 flash+glean1 권장.

### ✅ 품질평가 하네스 구현 (BenchmarkQED-lite, MS 자료 R2)
- 신규 `evaluate.py`: `generate_questions`(AutoQ) + `judge_pairwise`(AutoE, 순서 2회로 위치편향 제거) + `compare_collections`(순차, DB 동시접근 회피). `graphrag eval --a A --b B [--source 파일] [--questions N] [--model M]`. 테스트 `tests/test_evaluate.py`(+6). AST OK.
- 라이브(6문항, flash): **`캐럴-flash`(단순) vs `캐럴-gemma-glean1`(풍부) = B 2승·4무·A 0승.** 풍부한 추출이 사실 질문서 이기고 한 번도 안 짐(추출 노력이 답변 품질로 일부 전환). 단 4무는 전부 "둘 다 정보 부족" → **다음 병목은 추출량이 아니라 retrieval(top_k 벡터)/답변 합성**일 수 있음(평가도구가 준 실질 발견).
- caveat: flash-lite 심판·6문항이라 방향성. 후속 옵션: 문항↑/더 나은 심판, `answer_question`에 model 인자(답변 모델 비교), 병렬화.

### 📎 검토 메모 (MS 자료)
- **R2 BenchmarkQED = 위에서 경량 구현 완료.**
- **GraphRAG Auto-Tuning**(프롬프트 최적화): 컬렉션(사업)당 페르소나+few-shot 예시 자동생성만 경량 채택 권장. **엔티티 타입 자동확장은 우리 고정 온톨로지와 충돌 → 보류**. MS `graphrag` 라이브러리 통짜 도입 X, ~100줄 자체 구현.
- **BenchmarkQED**(품질 검증): AutoQ(질문 자동생성)+AutoE(LLM-as-judge 페어와이즈)만 경량 자체 구현 권장(OpenAI 전제·무거움이라 pip 지양). 모델/gleaning/프롬프트 A/B를 '수' 대신 '답변 품질'로 판정. **우선순위 상**.
- 둘 다 "추출 고도화 보류 트랙" → 마일스톤 결정 후 착수.

### ▶️ 다음 단계
1. 커밋: `config.py adapters/llm_adapter.py pipeline/ingest.py process_inbox.py graphrag_cli.py app.py USAGE.md tests/test_pipeline_ingest.py implementation_plan.md walkthrough.md HANDOFF-sub.md`.
2. (권장) `flash --glean 1` 실제 recall 리프트 소규모 실측(오늘 잔여 136요청 내) 후, gleaning 기본값/권장 라운드 확정.
3. (선택) gemma 과잉 허브(`영국` 등) 억제 프롬프트/병합 규칙.
4. 테스트 컬렉션 정리 여부 결정.

## 📅 2026-06-30 세션 (2) — USAGE.md 명령 통합 GUI 확장

### 목적
`USAGE.md`의 graphrag 명령 전체를 마우스로 실행하는 GUI 제공. 기존 `app.py`(업로드/그래프/현황 3탭)는 일부 명령만 다뤄, 질문·초기화·병합·삭제·백업/복원·컬렉션·계층·브릿지·사용량이 GUI에 없었다.

### 변경 (✅ 구현·검증 완료 / ⏳ 커밋 대기)
- **`app.py` 전면 확장(백엔드 무변경):** CLI(`graphrag_cli.py`)와 동일하게 `db/*`·`pipeline/*`·`query`·`graph_export`·`backup_db`/`restore_db`의 기존 함수를 그대로 호출하는 7개 탭으로 재구성.
  1. 📊 현황(`status`+`usage`+`collections` 트리, 범위 선택) 2. 📥 문서 넣기(`ingest`: 업로드/inbox, 예상 요청 수·**한도 가드**·분할 안내, `--force`/`--no-merge`, 처리 후 고아 정리·자동 병합) 3. 💬 질문(`query`, 전체/컬렉션 다중, 계층 펼침) 4. 🕸️ 그래프(시각화+GEXF 내보내기·다운로드) 5. 🗂️ 컬렉션(`merge`/`set-parent`·`unset-parent`/`delete`/`delete-collection`) 6. 🔗 브릿지(`list`/`suggest`/`add`/`remove`) 7. 🛠️ 유지보수(`init`/`init --reset`/`backup`/`restore`).
  - 되돌릴 수 없는 작업(`init --reset`·`delete-collection`·`restore`)은 확인 체크박스 후 버튼 활성화.
  - 조회 범위는 CLI `_collections_from_args`와 동일하게 부모→자손 펼침 재현.
- **`USAGE.md` §7** 갱신(3탭 설명 → 7탭 명령 매핑 표).
- **`implementation_plan.md`** 본 작업 계획으로 덮어씀(직전 Tier 1~3 계획은 이 문서·`7e95da9`에 보존).

### 검증
- `app.py` AST 파싱 OK.
- `streamlit run app.py`(헤드리스 8501) 기동 → 7개 탭 렌더, 현황 탭 실데이터 표시(문서 1/청크 378/엔티티 940 — 직전 세션 수치와 일치), 문서 넣기 탭 위젯 정상. **앱 코드 유래 예외 0건.**
  - 로그의 `transformers ... torchvision` 트레이스백 다수는 Streamlit 파일 감시기가 transformers 지연 모듈을 훑다 난 잡음(기능 무관, 기존 app.py도 동일).
- 백엔드 미변경이라 기존 `pytest`(135 passed) 영향 없음.

### ⚠️ 알려진 문제
- 새 의존성 없음(Streamlit 기존 사용). 첫 기동 시 `sentence_transformers`(torch/transformers) 임포트로 수십 초 지연 — 기존과 동일.

### ▶️ 다음 단계
1. 커밋: `app.py USAGE.md implementation_plan.md HANDOFF-sub.md` (+ 직전 세션에서 남았던 `HANDOFF-sub.md` 미커밋분 포함).
2. (직전 세션 이월) 한도 리셋 후 `잠수함 기술개발` 전체 재인덱싱 — 아래 6/30 세션(1) 참조.

## 📅 2026-06-30 세션 — 잠수함 문서 추출 품질 개선 (Tier 1~3)

### 진단 (실DB 점검)
- `잠수함 기술개발` 1건: 엔티티 989/관계 2183. 양은 충분, 질이 희석.
- 근본 원인: ①프롬프트가 식별자/긴제목/약한관계 미차단 ②`merge` 미실행(alias 0/989) + 설명문 오염으로 병합 불가(이름+설명 0.83 vs 이름만 0.98) ③원본 표 노이즈 51%로 청크 예산 낭비.

### 변경 (✅ 구현·검증·커밋 완료 — `7e95da9`)
- **Tier 1 프롬프트(`pipeline/ingest.py`):** 순수 식별자(특허번호·코드·주소)·긴제목 엔티티 금지, 짧은 표준명, `RELATED_TO` 억제, 장비-주제 오연결 방지 규칙 추가.
- **Tier 2 병합(`pipeline/entity_resolution.py`):** `_normalize_name`/`find_normalized_duplicates`(공백·기호만 다른 표기 무료 병합), 임베딩 입력 이름만(설명 제외), `run`이 정규화→임베딩 순. `graphrag_cli.py`에 `ingest` 후 자동 병합 + `--no-merge`.
- **Tier 3 표 전처리(`db/document_store.py`):** `clean_markdown()` 신규(빈 표셀/구분선 제거), `process_file`가 청킹 직전 적용(해시·원본은 raw 유지), `estimate_request_count`도 정리본 기준.
- 테스트: `test_document_store.py`(+3), `test_entity_resolution.py`(+4).

### 현재 상태
- `pytest` 전체 **135 passed**.
- 무료 병합 1회 실행(비용 0): 엔티티 989→**940**, 관계 2183→**2132**, 54쌍 병합. `연료전지 시스템` 98→148(파편 흡수), `숭실대학교` 35→73.
- 표 정리 실증: `ingest --dry-run` 378→**191**청크.
- 이번 세션 코드/문서 8개 파일 **커밋 완료**(`7e95da9`). (M2는 `6c47b84`로 이미 커밋됨 — 직전 메모의 "M2 미커밋"은 오기였음.)
- `.gitignore`(`.tmp.drive*/` 제외 추가) + `HANDOFF-main.md`·`HANDOFF-sub.md` **커밋 완료**(`6584aa6`). 이제 HANDOFF 문서도 git 추적됨.
- 본 세션 종료 마무리로 수정한 `HANDOFF-sub.md`만 미커밋 상태로 남음(다음에 같이 커밋).

### ⚠️ 알려진 문제
1. 이름만 임베딩이 특허번호 같은 ID성 문자열을 과병합(예: `10-2190943`←`10-2190941`). Tier 1이 재인덱싱 시 ID를 애초에 추출 안 해 자연 해소됨.
2. `연료전지`(성분)와 `연료전지 시스템`(시스템)은 의도적으로 분리 유지.

### ▶️ 다음 단계 (Next Steps)
1. **★ 한도 리셋(태평양시간 자정) 후 전체 재인덱싱:**
   `graphrag delete-collection "잠수함 기술개발"` → `graphrag ingest "processed/잠수함 기술개발/잠수함 사업계획서.md" --collection "잠수함 기술개발"` (자동 병합 포함, 예상 191호출).
   재인덱싱 후 `db_status.py`로 잡티(특허번호 OTHER)·고립·연료전지 단일허브화 재확인.
2. 이 세션 마무리로 수정한 `HANDOFF-sub.md` 커밋(`git add HANDOFF-sub.md` 후 커밋). 코드·gitignore·HANDOFF는 `7e95da9`/`6584aa6`로 이미 정리됨.
3. (선택) 재인덱싱 검증 후, 추출 품질 개선을 **M3 마일스톤**으로 `HANDOFF-main.md`에 승격할지 사용자 확정. (현재 HANDOFF-main 미반영 — §7-1에 따라 사용자 승인 후 기재.)

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
