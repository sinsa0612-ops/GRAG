# GraphRAG 사용 가이드

개인용 단독 GraphRAG 시스템을 **`graphrag` 통합 CLI**로 쓰는 방법입니다.
핵심 개념은 **컬렉션(사업)** 입니다 — 사업마다 그래프를 격리하되, 필요하면 여러 사업을 가로질러 종합할 수 있습니다.

---

## 0. 사전 준비 (최초 1회)

1. `.env`에 Gemini API 키를 채웁니다.
   ```
   GEMINI_API_KEY=발급받은_키
   ```
2. 가상환경을 활성화하고 의존성 + `graphrag` 명령을 설치합니다.
   ```bash
   .venv\Scripts\activate
   pip install -r requirements.txt
   pip install -e . --config-settings editable_mode=compat
   ```
   마지막 줄이 `graphrag` 명령을 가상환경에 등록합니다. 이제 어디서든 `graphrag ...`로 쓸 수 있습니다.

---

## 1. 핵심 개념 — 컬렉션(사업)

- 문서를 넣을 때 **컬렉션 이름**(사업명)을 지정합니다. 같은 컬렉션 안에서만 엔티티가 연결·병합됩니다.
- **서로 다른 컬렉션은 격리**됩니다. 사업A의 "김부장"과 사업B의 "김부장"은 이름이 같아도 별개로 취급됩니다(교차 오염 방지).
- **종합**이 필요하면 쿼리·조회 시 `--all`(전체) 또는 `--collection A,B`(여러 개)로 범위를 넓힙니다. 주간업무보고·자료 종합이 이 방식입니다.

---

## 2. DB 초기화

```bash
graphrag init            # 스키마 생성(이미 있으면 그대로)
graphrag init --reset    # 기존 DB를 전부 지우고 새로 시작 (주의: 되돌릴 수 없음)
```

---

## 3. 문서 넣기 (추출 + 그래프 생성)

```bash
# 파일을 사업A 컬렉션으로 처리
graphrag ingest "메모.md" "보고서.md" --collection 사업A

# inbox/ 폴더의 파일을 한 번에 사업A로 처리 (처리 후 processed/사업A/ 로 이동)
graphrag ingest --inbox --collection 사업A
```

- `--collection`을 생략하면 `default` 컬렉션으로 들어갑니다.
- 문서는 약 2000자 단위로 잘려 청크당 LLM 호출 1번이 나갑니다. **긴 문서는 시간이 걸립니다**(무료 한도 보호를 위해 호출당 3초 대기).
- 같은 컬렉션에 같은 파일을 다시 넣으면, 내용이 안 바뀌었으면 건너뛰고 바뀌었으면 옛 데이터를 교체합니다.

---

## 4. 질문하기 (쿼리)

```bash
# 한 사업 범위 안에서만 답 (다른 사업 정보가 섞이지 않음)
graphrag query "김부장은 무슨 일을 해?" --collection 사업A

# 여러 사업을 함께
graphrag query "A와 B 사업의 공통 거래처는?" --collection 사업A,사업B

# 전체 종합 (행정 종합 — 주간보고/자료 종합 등)
graphrag query "이번 주 전체 사업 현황 요약해줘" --all
```

- 범위를 지정하지 않으면 **전체 종합(`--all`과 동일)** 으로 답합니다.
- 답변은 **추출된 그래프 + 의미 검색된 본문 조각만 근거**로 생성됩니다. 그래프에 없는 내용은 추측하지 않고 "부족하다"고 답하도록 설계돼 있습니다.
- 질문에 **그래프에 등록된 이름을 그대로 쓰면** 그 엔티티의 관계 정보까지 활용돼 정확도가 올라갑니다(`graphrag status --collection 사업A`로 등록된 타입/관계 확인).

---

## 5. 그래프 보기 (Gephi)

```bash
graphrag graph --collection 사업A -o 사업A.gexf   # 사업A만 내보내기
graphrag graph --all                              # 전체를 exports/ 에 저장
```
생성된 `.gexf`를 [Gephi](https://gephi.org/)에서 `File → Open`으로 엽니다. 노드에 **collection 속성**이 있어 Gephi에서 사업별로 필터·색 구분이 가능하고, 타입별 색상은 자동 적용됩니다.

---

## 6. 현황 / 컬렉션 / 유지보수

```bash
graphrag status                        # 전체 현황
graphrag status --collection 사업A      # 사업A 현황
graphrag collections                   # 컬렉션 목록 + 각 문서/엔티티 수
graphrag merge --collection 사업A       # 사업A 안에서 중복 엔티티 자동 병합(임베딩 유사도)
graphrag delete "메모.md" --collection 사업A   # 문서 삭제(벡터/관계/기록 + 고립 엔티티 정리)
graphrag delete-collection 사업A               # 사업A를 통째로 삭제(엔티티 포함)
graphrag backup                        # 전체 DB 백업 (최신 10개 자동 보관)
graphrag restore backups/그_백업.zip    # 백업 복원 (원자적 — 실패해도 기존 DB 보존)
```

### 잘못된 컬렉션으로 추출했을 때 (롤백)
A사업 자료를 실수로 B사업으로 넣었다면:
```bash
graphrag delete-collection 사업B            # B를 통째로 비우거나 (B에 그것만 있을 때)
graphrag delete "메모.md" --collection 사업B  # 그 문서만 빼고 (B에 다른 자료도 있을 때)
graphrag ingest "메모.md" --collection 사업A  # 올바른 컬렉션으로 다시 넣기
```
- `delete`(문서 단위)는 그 문서의 벡터·관계·기록을 지우고 **관계가 끊겨 고립된 엔티티**까지 정리합니다. 다만 다른 엔티티와 연결된 채 남은 노드는 자동 삭제되지 않으니, 그럴 땐 `delete-collection`이 가장 깔끔합니다.
- 미리 `graphrag backup`을 해뒀다면 `graphrag restore`로 추출 직전 상태로 통째 되돌릴 수도 있습니다.

- `merge`는 **컬렉션 안에서만** 병합합니다(사업 간 자동 병합 없음).
- 특정 두 이름이 자동 병합되지 않게 막으려면:
  ```bash
  python -c "from db import sqlite_manager; sqlite_manager.add_merge_blacklist('이름A','이름B','이유')"
  ```

---

## 7. 웹 UI (선택)

```bash
streamlit run app.py
```
업로드 탭에서 **컬렉션 이름**을 입력하고 파일을 끌어넣으면 그 컬렉션으로 처리됩니다. 그래프 시각화/현황 탭도 제공됩니다.

---

## 자주 쓰는 명령 요약

```bash
graphrag init --reset                              # 처음부터 새로
graphrag ingest "파일.md" --collection 사업A         # 넣기
graphrag query "질문" --collection 사업A             # 한 사업에 묻기
graphrag query "질문" --all                          # 전체 종합
graphrag graph --collection 사업A -o out.gexf        # Gephi로 보기
graphrag collections                               # 사업 목록
```
