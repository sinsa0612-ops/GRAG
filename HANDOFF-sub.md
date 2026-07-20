# HANDOFF-sub.md (실무 변경 로그 / 다음 작업)

> AI 주도권 문서. 세션별 세부 변경 이력·현재 상태·미해결 문제·다음 단계를 기록한다.

## 📅 2026-07-20 세션 — M1.5: 엔티티 설명 통합 요약(#6, 부분→정석)

### 배경
- M1(`5f841f6`)이 만든 LLM 백엔드 라우터(`generate(..., backend="ollama")`) 위에, canonical GraphRAG의
  "다중 언급 설명을 하나로 통합" 격차를 메운다. 지금까지는 같은 엔티티가 여러 문서에서 나오면
  `graph_manager.upsert_entity`의 `ON MATCH SET e.description = $description`이 마지막 값으로 덮어썼다.

### 변경 (✅ 구현·검증 완료 / ⏳ 커밋 대기 — 커밋은 오케스트레이터가 CEO 결재 후 별도 처리)
- **`db/sqlite_manager.py`:** `entity_desc_candidates(collection, entity_name, source_doc, description)` 신설
  (PK 3종 복합). CRUD 5종: `upsert_desc_candidate`/`get_desc_candidates`/`get_entities_with_min_candidates`/
  `delete_desc_candidates_by_source_doc`/`delete_desc_candidates_by_collection`. source_doc 키로 문서 단위
  캐스케이드 삭제가 정확히 짚히게 했다(유령 후보 방지).
- **`db/graph_manager.py`:** `update_entity_description(collection, name, description)` 신설 — description만
  바꾸고 type/aliases는 안 건드림(요약 배치 전용, upsert_entity와 달리 type 재전달 불필요).
- **`pipeline/ingest.py` (`store_extraction`):** 엔티티 upsert 직후, description이 비어있지 않으면
  `(collection, resolved_name, source_doc)` 키로 후보를 병행 적재. **hot-path 불변 확인:** description은
  지금처럼 즉시 채워짐(로컬 질의 영향 없음), 후보 적재는 순수 DB 쓰기라 인제스트 경로에 LLM 호출 추가 없음.
- **`pipeline/desc_summarizer.py` (신규):** `summarize_descriptions(collection, min_candidates=None,
  backend="ollama", model=None)` — 후보 `min_candidates`(기본 `config.desc_summary_min_candidates=2`)개
  이상인 엔티티만 도메인·언어 중립 프롬프트로 Ollama에 통합 요약 요청, 성공 시 `update_entity_description`으로
  교체. 후보 1개는 LLM 호출 없이 스킵(호출 절약). 요약 실패/빈 응답인 엔티티는 건너뛰고 기존 설명 유지(다른
  엔티티 처리는 안 막힘).
- **`db/document_store.py`:** `commit_document`(재처리 시 옛 source_id 정리)·`delete_document`·
  `delete_collection`·`cleanup_orphaned_data` 4곳에 설명 후보 캐스케이드 삭제 호출 추가(기존
  `delete_relations_by_source_doc` 패턴과 동일한 지점). `delete_collection` 캐스케이드는 작업지시서엔 명시
  안 됐지만, 다른 데이터(엔티티/벡터/문서)가 이미 컬렉션 통째 삭제되는 지점이라 후보만 안 지우면 이 기능이
  스스로 유령 데이터를 만드는 셈이라 판단해 포함([ASSUMPTION] 범위 판단, 1줄 추가).
- **`graphrag_cli.py`:** `summarize-descriptions --collection C [--min-candidates N]` 서브커맨드 신설
  (옵트인 배치, `p_merge`/`p_bridge` 사이에 배치).
- **`config.py`:** `desc_summary_min_candidates: int = 2` 추가(하드코딩 금지, 단일 출처).

### 검증
- `pytest -q` **193 passed**(기존 170 + 신규 23, 0 failed). 신규 테스트 분포:
  `test_sqlite_manager.py`+6, `test_graph_manager.py`+1, `test_pipeline_ingest.py`+4(hot-path 불변 명시
  assert 포함), `test_document_store.py`+4(캐스케이드), `test_desc_summarizer.py`+7(병합/스킵/설정 기본값/
  실패격리/실 Ollama skipif), `test_graphrag_cli.py`+1(CLI 배선).
- 기존 테스트 1건(`test_store_extraction_merges_known_alias_instead_of_creating_new_node`)이
  `store_extraction`의 새 sqlite 의존성 때문에 `sqlite_manager.init_schema()` 호출이 필요해져 1줄 추가(내
  변경이 만든 정당한 신규 의존성).
- **실 Ollama 통합 테스트가 로컬에서 실제로 통과함**(스킵 아님 — Ollama 기동 중, qwen3:14b 응답 확인).
- **CLI 엔드투엔드 스모크(운영 DB 미접근, `DB_DIR` 스크래치 격리):** 후보 2개("아침 회의 주재"/"오후 보고서
  작성") 수동 적재 → `graphrag summarize-descriptions --collection 스모크테스트` 실행 →
  `"아침에 회의를 주재한 홍길동은 오후에 보고서를 작성했다"`로 실제 통합됨을 확인. 스크래치 삭제 완료.

### ⚠️ 알려진 한계(범위 밖으로 남긴 것)
1. `entity_resolution.merge_entity_into`로 엔티티가 병합(drop)돼도 그 이름의 옛 설명 후보 행은 안 지워짐 —
   요약 대상 조회는 canonical 이름으로만 하므로 orphan 후보는 그냥 안 쓰이고 남을 뿐(정합성 버그는 아니고
   저장공간 낭비 수준). M1.5 범위 밖(작업지시서 "커뮤니티·글로벌·기존 Gemini 로직만 범위 밖" 명시, 이 항목은
   언급 안 됐으나 최소 변경 원칙상 손대지 않음).
2. `--min-candidates`를 CLI에서 안 주면 config 기본값(2) 사용 — 이 커맨드는 컬렉션당 1회성 배치라 매번 수동
   실행(자동 트리거 없음, M1.5 스펙대로 옵트인).

### ▶️ 다음 단계
1. **커밋 대기** — 오케스트레이터가 CEO 결재 후 진행. 변경 파일 목록은 이번 세션 diff 참조(`config.py
   db/document_store.py db/graph_manager.py db/sqlite_manager.py graphrag_cli.py pipeline/ingest.py
   pipeline/desc_summarizer.py tests/test_document_store.py tests/test_graph_manager.py
   tests/test_graphrag_cli.py tests/test_pipeline_ingest.py tests/test_sqlite_manager.py
   tests/test_desc_summarizer.py HANDOFF-sub.md`).
2. (선택) M2 착수 전, 실데이터 소규모 컬렉션으로 Ollama 통합요약 품질을 육안 확인(한국어 뉘앙스 — spec.md
   Risk 1과 별개로 설명요약 자체의 품질 체감).
3. M2(Leiden 계층 탐지)는 spec.md/addendum 순서대로 별도 세션.

## 📅 2026-07-02 세션 — retrieval 투자 1차: 답변 합성 충실성 재구성

### 배경
- 4자 비교 실답변 진단: 추출 풍부 조건에서 **그래프발 환각**(flash-g1 '난로' 오귀속, gemma-g1 '물탱크'/'악귀 군단'). 근본 원인 = `_ANSWER_PROMPT`가 그래프·본문을 대등하게 제시.

### 변경 (✅ 구현·pytest 완료 / ⏳ 전체 eval은 리셋 후)
- **`query.py`:** `_ANSWER_PROMPT` 재구성 — **본문(벡터)=1차 근거, 그래프=보조 힌트**(충돌 시 본문 우선, 그래프-only·본문 무근거 주장 단정 금지), 본문 블록을 앞으로. `_build_vector_context`(완전중복 dedup + `[본문 N]` 라벨) 신규. `answer_question`은 라벨본=프롬프트용, 원문 청크=그래프 매칭용. **query.py만**(스키마·DAL·인터페이스 무변경).
- **`tests/test_query.py` +3:** dedup·라벨 / 빈 입력 / 본문 우선 프레이밍. `pytest` **151 passed**.

### 검증 (✅ 완료 — 리셋 후 전체 eval, 커밋 53e9065)
- 충실성 전/후 전체 eval(리셋 후 깨끗한 quota, `verify_after_reset.py`): `캐럴-flash-g1` **신 4·구 0·무 4**, `캐럴-gemma-g1` **신 4·구 0·무 4** → **합 신 8승·구 0승·무 8, 완패 없음.** 표적 환각(`gemma-g1` Q6 '난로') 신답에서 제거 확인. → **충실성 재구성 확정.**
- 변경은 574f030 다음 커밋 **53e9065**로 이미 반영됨(사용자 승인). 본 검증 결과 문서화만 추가 커밋.

### ⚠️ 한도 정정 (중요)
- **flash-lite 실제 하드 한도 = 500/일** — API 429가 `limit: 500, model: gemini-3.1-flash-lite, FreeTier`로 명시. 사용자 "넉넉해"는 gemma(1500)나 다른 지표였던 듯. **flash 대량 eval은 500 벽에 걸린다**(하루 예산 배분 필요). gemma는 별도 1500. 리셋=태평양 자정.

### ✅ retrieval 트랙 종료 (검증 후 마무리 — 사용자 결정)
- 확정 2건: ①top_k+벡터→그래프 브릿지(574f030) ②충실성 재구성(53e9065·검증 8-0-8·5578d08). 둘 다 eval로 검증.
- **잠수함(표·희소 문서) 진단(10문항):** 기관명·연구책임자·선정방식·기간·주소·기술요약·종합·연구비(11,331,099천원)까지 **전부 정답·근거충실**. '약함 4/10'은 heuristic 오탐(짧은 정답·마커노이즈)이고 실제 gap 없음. 새 프롬프트대로 "[본문 3]에 따르면"처럼 근거 위치도 자연 인용.
- **결론:** 서사형(캐럴)·표/희소형(잠수함) 양쪽 답변 양호 → 관찰 gap 소멸. MMR·리랭킹 등 추가 미세개선은 **고칠 문제가 없는 저ROI**라 착수 안 함. **retrieval은 견고한 상태로 종료.**
- 실DB 캐럴 4조건(각 벡터 91)·잠수함 보존 = 향후 실험 자산.

### ▶️ 다음 단계 (피봇 — 사용자 레버 선택 대기)
- retrieval 종료. 남은 HANDOFF-main "다음 후보"/메모 레버:
  1. **프롬프트 자동튠**(컬렉션당 페르소나+few-shot, MS auto-tuning 경량판) — 단 4자 비교상 '추출 품질↑→답변↑' 전환이 약해 ROI 재고 필요. 고정 온톨로지 유지.
  2. **답변 모델 비교**(`answer_question`에 model 인자) — retrieval이 견고해진 지금, 더 강한 합성 모델이 답변을 개선하는지 측정(작은 변경).
- (권고) 두 retrieval 증분을 **M5 마일스톤**으로 HANDOFF-main 승격할지 사용자 확정(§7-1). 현재 미반영.

## 📅 2026-07-01 세션 (2) — retrieval/답변 합성 개선 (top_k + 벡터→그래프 브릿지)

### 배경/목표
- M4 eval에서 "다음 병목은 추출량이 아니라 retrieval/답변 합성"이라는 발견의 후속(HANDOFF-main "다음 후보"의 retrieval 트랙).
- 근본 원인: `query._gather_graph_context`가 **엔티티 이름이 질문 문자열에 그대로 있을 때만**(`name in question`) 그래프를 열어, 풍부한 추출이 답변에 도달하지 못함.

### 변경 (✅ 구현·검증 완료 / ⏳ 커밋 대기)
- **`config.py`:** `retrieval_top_k=12`(하드코딩 8 제거·설정 중앙화), `graph_context_max_entities=20` 신설.
- **`query.py`:** `_gather_graph_context(question, collections, extra_text="")` — 질문 직접 매칭 우선 + 본문 조각(extra_text) 매칭 보강(한 글자 이름 제외, 상한 20). `_gather_vector_context` 제거하고 `answer_question`이 `query_similar` 1회 호출로 (a)근거·(b)그래프 힌트 재사용. `top_k=None`이면 설정값. **스키마·인터페이스·Pydantic 무변경**(오케스트레이션만).
- **`tests/test_query.py` +3:** 본문→그래프 표면화 / 한 글자 이름 제외 / top_k 설정값 사용. 기존 5개는 `extra_text=""` 기본값으로 하위호환.

### 검증
- `pytest` 전체 **148 passed**(145+3).
- **브릿지 정량(`잠수함 기술개발`):** 이름을 안 대는 사실 질문에서 그래프 표면화 엔티티 **1→20(상한)**. 벡터가 그래프 도달을 열어준 직접 증거.
- **답변 A/B(scratchpad·비커밋, `잠수함` 8문항, 개선 전 vs 후 페어와이즈):** 개선전 1승·**개선후 4승**·무 3. "정보 없음"→실제답 전환: 공고번호 `제2026-71호`, 선정방식 `품목지정`. 회귀 없음(유일 A승은 총기간 병기 사소차, 무 2건은 순서편향 뒤바뀜). 사용량 194→260/500.

### ⚠️ 측정 중 발견 (중요 — 프로젝트 맥락 정정)
- **옛 캐럴 A/B 컬렉션들(`캐럴-flash`·`캐럴-gemma-glean1` 등)은 벡터 청크가 0개**였다(전체 벡터 378청크 전부 `잠수함 기술개발`; 이전 세션 추출 A/B가 `process_file`을 안 거치고 그래프 추출만 직접 호출 → 벡터·문서기록 단계 통째 건너뜀. source_doc이 `gemma_glean1_demo` 같은 가짜 라벨인 게 증거). → **M4 eval의 "retrieval 병목" 결론은 벡터가 빈 컬렉션에서 나와 부분 교란**돼 있었다. 격벽 자체는 정상이었다(관계 양끝 컬렉션 위반 0건). 정상 `ingest`는 벡터를 적재하므로 코드 버그 아님(던질 테스트 컬렉션 문제).

### 🔁 후속 — 캐럴 정상 재적재 + 4조건 공정 비교 (✅ 완료, scratchpad·비커밋)
- 사용자 지시로 옛 캐럴 4개 삭제 후, **정상 파이프라인(벡터+그래프+문서기록)** 으로 2×2 재적재. 공정성 위해 4조건 모두 동일 추출 로직(어휘힌트 없음)으로 통일, model·glean만 변수. flash=순차, **gemma=스레드 12병렬**.
- **gemma 병렬 규명(사용자 "1 RPM" 문의):** 병렬은 정상 작동한다. 동시성 프로브(6콜)에서 전부 동시 출발·**5.1x**. 실적재도 gemma-g0 91콜 19.8분(직렬 대비 ~7.7x), 429/재시도 0건. "1 RPM"은 **채움 구간 착시**(gemma 실추출 콜당 ~100초라 첫 ~100초는 완료 0건 → 대시보드 바닥). 램프업 후 ~7 RPM.
- **recall(엔티티/관계/벡터):** flash-g0 296/318/91 · flash-g1 539/582/91 · gemma-g0 419/643/91 · gemma-g1 660/940/91. gleaning +82%(flash)/+58%(gemma) 엔티티, gemma>flash(glean0 +42%, glean1 +22%).
- **답변 품질(8문항 페어와이즈, flash 답변모델):** flash gleaning **B 3승·0패·무5**(개선), gemma gleaning 1-1-6(미미), 모델효과 glean0 1-1-6·glean1 2-2-4(**차이 없음**). **무승부는 "둘 다 잘 답함"**(정보부족 답변 f0 1/f1 0/g0 1/g1 0, 총 32칸 중 2칸만 부족) — 옛 교란런의 "둘 다 부족"과 정반대.
- **결론:** 벡터 검색이 제대로 있으면 **2배 넘는 추출량 차이가 Q&A 답변엔 거의 전환 안 됨**(답변은 공유하는 91 벡터청크가 지배, 그래프는 보조). Q6에서 gemma가 flash-g1 환각비유('난로')를 잡아 이긴 반례 → 추출↑이 잡티↑이기도. **레버리지는 추출 recall이 아니라 retrieval/합성**(오늘 오전 개선이 옳았음을 공정비교가 확증). 단 그래프 순회·교차 인사이트 등 '구조' 용도엔 recall 여전히 중요. caveat: flash-lite 심판·8문항·서사형 문서(캐럴은 벡터청크가 답을 많이 품음), 표·희소 문서(잠수함류)는 그래프 기여가 더 클 수 있음.
- 실DB에 `캐럴-flash-g0`·`캐럴-flash-g1`·`캐럴-gemma-g0`·`캐럴-gemma-g1`(각 벡터 91청크) 보존 — 향후 retrieval 실험 자산.
- 참고: `config.llm_daily_limit=500`은 flash 기준 기본값. 사용자 실제 한도는 더 넉넉(확인). 재적재/비교 스크립트는 CLI 가드를 우회(process_file 직접)라 실제 한도만 유효.

### ▶️ 다음 단계
1. **커밋 대기:** `config.py query.py tests/test_query.py implementation_plan.md walkthrough.md HANDOFF-sub.md`. (scratchpad 재적재/비교 스크립트는 비커밋.)
2. (권장) 이번 공정비교 결과를 근거로, 추출 고도화(gemma·gleaning)보다 **retrieval/답변 합성**에 우선 투자. HANDOFF-main M4 note의 "retrieval 병목" 방향성은 강화됨(단 그 eval 자체는 벡터 미적재라 무효 — 사용자 승인 시 caveat 기재).
3. (선택) 잠수함류 표·희소 문서에서 그래프 기여도 재측정(캐럴은 서사형이라 벡터 우세).
4. (이월) HANDOFF-main "다음 후보"의 나머지 — 프롬프트 자동튠(컬렉션당 페르소나+few-shot).

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
