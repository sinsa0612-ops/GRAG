# implementation_plan.md — retrieval/답변 합성 개선 (top_k + 벡터→그래프 브릿지)

> 2026-07-01 세션. HANDOFF-main "다음 후보"의 retrieval 트랙. 직전(eval 하네스) 계획은 walkthrough.md/HANDOFF-sub.md·git에 보존.
> 사용자 승인: 범위=(1)+(2), 구현 후 즉시 eval 재측정.

## 배경 / 문제
- M4 `eval` 실측(캐럴-flash 단순 vs 캐럴-gemma-glean1 풍부): B 2승·**4무**·A 0승. 4무는 전부 "둘 다 정보 부족".
- **근본 원인:** `query._gather_graph_context`가 **엔티티 이름이 질문 문자열에 글자 그대로 있을 때만**(`name in question`) 그래프를 끌어온다. 벡터가 관련 본문을 찾아도 그 본문 속 엔티티는 그래프 조회를 트리거하지 않는다. → 추출을 2.5배 풍부하게 해도, 질문에 이름이 안 적히면 그 풍부함이 답변에 못 닿는다. 병목은 추출량이 아니라 retrieval.

## 구조 사전 이해 보고서 (3줄) — 사용자 확답 완료
1. **근본 원인:** 그래프 컨텍스트가 '질문 문자열에 엔티티 이름이 그대로 등장'할 때만 열려, 풍부한 추출이 답변에 도달하지 못함.
2. **데이터 흐름/규칙:** `answer_question`은 DAL·어댑터의 기존 함수만 조립하는 오케스트레이션. DB 스키마·인터페이스·Pydantic 계약 무변경. `top_k=8`은 함수 기본값 하드코딩(§2-1 위반 소지).
3. **정석:** (1) `top_k` 설정화·상향, (2) 벡터로 찾은 본문 속 엔티티도 그래프로 끌어오는 브릿지. 폭발 반경 = `query.py`+`config.py`.

## 구현
### (1) top_k 설정화·상향 — `config.py`
- `retrieval_top_k: int = 12` 신설(기존 하드코딩 8 제거, 설정 중앙화 §2-1 준수).
- `graph_context_max_entities: int = 20` 신설 — 본문 매칭으로 그래프가 폭주하지 않게 상한(질문 매칭 우선 보존 후 채움).

### (2) 벡터→그래프 브릿지 — `query.py`
- `_gather_graph_context(question, collections=None, extra_text="")` — `extra_text` 옵션 추가.
  - 질문에 직접 등장한 엔티티(`name in question`)를 **우선** 수집(기존 동작 = `extra_text=""`일 때 그대로).
  - 그다음 `extra_text`(벡터 청크)에 등장한 엔티티를 보강. 단 **한 글자 이름은 제외**(`len(name) >= 2`, 본문 substring 오탐 억제).
  - 총합 `graph_context_max_entities`로 상한. 이후 브릿지·관계 표면화 로직은 기존과 동일(matched_set 재사용).
- `_gather_vector_context` 제거 → `answer_question`에서 `query_similar`를 1회 호출해 청크를 얻고, (a) 벡터 컨텍스트 문자열, (b) 그래프 매칭용 `extra_text`로 재사용.
- `answer_question(question, collections=None, top_k=None)` — `top_k` 미지정 시 `settings.retrieval_top_k`.

## 영향 파일
- 수정: `config.py`, `query.py`, `tests/test_query.py`.
- 문서: `implementation_plan.md`, `walkthrough.md`, `HANDOFF-sub.md`.
- scratchpad(비커밋): eval 재측정 스크립트.

## 검증
- 기존 `test_query.py` 5개 하위호환(전부 `extra_text` 미지정 = 옛 동작).
- 신규 테스트: ①본문 청크 엔티티가 그래프로 표면화 ②한 글자 이름 제외 ③`top_k=None`→설정값 호출.
- `pytest` 전체 green 확인.
- **eval 재측정(scratchpad, 비커밋):** `extra_text=""`·`top_k=8`(개선 전) vs 신 경로(개선 후)를 같은 컬렉션·같은 질문으로 `judge_pairwise` → 실제 리프트 수치화. 오늘 잔여 한도 내 소규모(6~8문항).

## 롤백
- `query.py`·`config.py` 되돌리면 끝(스키마·데이터 변경 없음).
