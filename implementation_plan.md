# 구현 계획서 — 🟡 관계 시계열 정체성 (우선순위 5, B안 트랙)

## 1. 목적 / 배경
- 같은 (주체, 대상, predicate) 관계가 시점(valid_from)만 다를 때 서로 덮어써 이력이 사라지던 문제 제거.
- 예: 2020년 A사 근무 → 2024년 B사 근무가 같은 WORKS_AT면 앞 기록 손실.

## 2. 근본 원인 (구조 사전 이해)
- `upsert_relation`의 `MERGE (a)-[r:RELATION {predicate}]->(b)` 가 관계를 predicate만으로 식별 → SET이 valid_from/source_doc 덮어씀.
- `merge_entity_into`의 두 MERGE도 predicate만으로 식별 → 병합 시 시계열 엣지 재병합.

## 3. 변경 설계 (A안: 평행 엣지)
- 관계 식별 키를 `{predicate, valid_from}`로 확장. 시점이 다르면 별개 엣지로 공존, 같으면 idempotent(중복 안 생기고 source_doc만 갱신).
- `upsert_relation`: MERGE 패턴에 valid_from 포함, SET은 source_doc만(valid_from은 키라 불변).
- `merge_entity_into`: 두 MERGE 패턴에 r.valid_from 포함, SET은 source_doc만.
- **스키마 무변경**(RELATION 테이블에 valid_from 컬럼 이미 존재) → 마이그레이션 없음. 기존 엣지 그대로, 신규 upsert부터 적용.
- B안(valid_to 기반 완전 양시간 모델)은 제외: LLM valid_from이 자주 비어 자동 이력관리가 불안정, KISS 위반.

## 4. 영향 파일
- `db/graph_manager.py` — `upsert_relation`, `merge_entity_into`.
- 테스트: `test_graph_manager.py`(시점 다르면 별개 엣지 / 같으면 idempotent / 병합 시 시계열 보존).

## 5. 검증
- `pytest` 전체 그린(기존 단일-관계 테스트는 영향 없음).
- Kuzu에서 같은 쌍에 평행 엣지가 실제로 생성되는지 라이브 1회 확인.

## 6. 알려진 한계(허용)
- 무날짜("") 관계와 동일 predicate의 날짜 관계가 공존하면 약간 중복(의미상 포함관계)될 수 있음. 데이터 손실은 없음.
- source_doc은 엣지당 1개(최신 우선) — 동일 관계를 여러 문서가 주장할 때의 출처 다중화는 범위 밖.
