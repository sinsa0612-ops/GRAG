# 구현 계획서 — 품질평가 하네스(BenchmarkQED-lite: AutoQ + AutoE)

> 직전(모델 토글·gleaning) 계획을 본 작업으로 덮어씀. 직전 내역은 walkthrough.md/HANDOFF-sub.md·git에 보존.

## 1. 목적
- 지금까지 추출 '수'(엔티티/관계 수·RELATED_TO 비율)로만 보던 품질을, **실제 Q&A 답변 품질**로 상대 비교할 수 있게 한다.
- MS BenchmarkQED의 아이디어만 경량 자체 구현(패키지 도입 X — OpenAI 전제·무거움·우리 query와 불일치).

## 2. 구조 사전 이해 보고서 (3줄)
1. **현재:** `query.answer_question(q, collections)`가 그래프+벡터 근거로 답을 만든다. 두 컬렉션을 같은 질문으로 답하게 하면 '추출 품질 → 답변 품질' 영향을 비교할 수 있다.
2. **데이터 흐름:** AutoQ(발췌→질문 n개) → 각 질문을 컬렉션 A·B로 답변(`answer_question`) → AutoE(LLM 페어와이즈 심판, 순서 뒤집어 2회 → 일치할 때만 승자) → 승패 집계. DB 쓰기 없음(읽기+LLM만).
3. **정석:** 새 얇은 모듈 `evaluate.py` + `graphrag eval` 서브커맨드. 기존 어댑터·query 재사용, 스키마/DB 무변경. flash 기반·순차(콜 빠름 + Kuzu 동시읽기 회피).

## 3. 설계
- `evaluate.py`:
  - `generate_questions(sample, n, model)` — 발췌로 질문 n개(구체+종합 혼합) 생성, JSON 배열 파싱(+줄단위 폴백).
  - `judge_pairwise(q, ans_a, ans_b, model)` — 순서 A→1/B→1 **두 번** 심판, 두 결과가 일치할 때만 승자(위치 편향 제거), 불일치=무승부.
  - `compare_collections(coll_a, coll_b, questions, judge_model)` — 질문마다 A·B 답변 + 심판, 승패 집계.
- `graphrag_cli.py`: `cmd_eval` + `graphrag eval --a A --b B [--questions N] [--source 파일] [--model M]`. `--source` 없으면 A의 엔티티로 발췌 구성.
- 심판/질문 모델 기본=설정 기본(flash). 답변은 `answer_question`의 기본 모델.

## 4. 영향 파일
- 신규: `evaluate.py`, `tests/test_evaluate.py`.
- 수정: `graphrag_cli.py`(서브커맨드), `USAGE.md`(eval 설명).

## 5. 검증
- `pytest`: 질문 파싱(JSON/폴백), 심판 위치 매핑(일치=승자/뒤바뀜=무), 집계.
- 라이브 스모크: `graphrag eval --a 캐럴-flash --b 캐럴-gemma-glean1 --questions 6 --source "processed/크리스마스 캐럴.md"` (flash, ~25콜).

## 6. 한계/비고
- LLM 심판은 편향 존재(순서 2회로 위치편향만 완화). 무료 flash 심판이라 절대평가 아닌 '일관된 상대비교'로 사용.
- 비용 있음(질문+답변×2+심판×2) → 소수 질문으로 가끔 실행.
- 후속(옵션): `answer_question`에 model 인자 → '답변 모델' 비교, 병렬화(속도), R1 프롬프트 자동튠과 결합한 튠→측정 루프.
