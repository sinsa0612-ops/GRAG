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

### ⚠️ 하루 요청 한도(RPD 500) 관리
무료 등급은 **하루 500요청** 한도가 있고, **요청 수 = 청크 수**라 처리 전에 정확히 예측됩니다.
```bash
graphrag ingest "큰문서.md" --dry-run          # 처리 없이 예상 요청 수만 확인
graphrag usage                                 # 오늘 사용량 / 남은 한도
```
- ingest는 시작 전에 **"예상 + 오늘 사용 > 500"이면 자동으로 막고**, 문서를 몇 개로 나눠야 하는지 안내합니다.
  - 예: 510청크 문서 → *"약 2개로 나눠 각각 넣으세요 (큰문서-1.md, 큰문서-2.md)"*. 의미 단위(전반/후반 등)로 나눠 각각 `ingest`하면 됩니다.
- 정말 강행하려면 `--force`. (단, 실제 한도에 닿으면 API가 막습니다.)
- 한 문서가 한도를 넘지 않게 **청크 수 자체를 줄이려면** `config.py`의 `chunk_size`를 키우면 됩니다(요청 수↓, 추출 정밀도는 약간↓ 트레이드오프).
- **더 촘촘히 뽑고 싶으면(recall↑) `--glean N`**: 청크마다 놓친 엔티티/관계를 최대 N번 더 캐냅니다(MS GraphRAG의 gleaning). 대신 **요청 수가 최대 (1+N)배**로 늘어 한도를 그만큼 빨리 씁니다(예상·가드가 자동 반영). 기본 0=끔. 예: `graphrag ingest "메모.md" --collection 사업A --glean 1`.
- *참고: 한도는 보통 태평양시간 자정에 리셋되며, 재시도(503/429)는 요청을 조금 더 쓰므로 예측은 하한값입니다.*

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

### 답변 품질 비교 (eval)
두 컬렉션 중 어느 쪽이 **실제 질문에 더 잘 답하는지**를, 질문 자동생성 + LLM 페어와이즈 심판으로 비교합니다(추출 '수'가 아니라 Q&A로 판정). 프롬프트·모델·gleaning을 바꿔 만든 두 그래프의 우열을 잴 때 씁니다.
```bash
graphrag eval --a 캐럴-flash --b 캐럴-gemma-glean1 --source "원문.md" --questions 6
```
- `--source` 원문에서 질문을 뽑고(없으면 A의 엔티티로), 각 질문을 A·B로 답하게 한 뒤 심판이 승자를 고릅니다. 순서를 뒤집어 두 번 심판해 위치 편향을 줄이고, 일치할 때만 승자로 인정합니다.
- 질문당 **답변 2 + 심판 2회**의 LLM 호출이 나갑니다(비용 있음 → 소수 질문으로 가끔). 무료 심판이라 절대평가가 아니라 '일관된 상대비교'로 보세요.

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

## 7. 웹 UI — 위 명령들을 버튼으로 (선택)

```bash
streamlit run app.py
```
터미널 대신 **마우스로 위 모든 명령을 실행**할 수 있는 GUI입니다. 7개 탭으로 나뉩니다.

| 탭 | 대응 CLI 명령 | 하는 일 |
|----|----------------|---------|
| 📊 현황 | `status` · `usage` · `collections` | 범위(전체/특정 컬렉션)별 문서·청크·엔티티·관계 수, 오늘 한도, 컬렉션 계층 트리 |
| 📥 문서 넣기 | `ingest` (+ `--dry-run`/`--force`/`--no-merge`/`--glean`) | 파일 업로드 또는 inbox/ 일괄. 처리 전 **예상 요청 수·하루 한도 가드**(초과 시 차단·분할 안내) + **gleaning 라운드 입력**(recall↑, 요청 (1+N)배), 처리 후 고아 정리·자동 병합 |
| 💬 질문 | `query` (`--all` / `--collection`) | 전체 종합 또는 컬렉션 다중 선택(계층은 부모→자손 자동 펼침)으로 질의 |
| 🕸️ 그래프 | `graph` | 범위별 그래프 시각화 + Gephi용 GEXF 내보내기·다운로드 |
| 🗂️ 컬렉션 | `merge` · `set-parent`/`unset-parent` · `delete` · `delete-collection` | 엔티티 병합, 본부(부모) 지정/해제, 문서 삭제, 컬렉션 통째 삭제 |
| 🔗 브릿지 | `bridge list\|suggest\|add\|remove` | 컬렉션 간 같은 대상 연결(SAME_AS) 관리 |
| 🛠️ 유지보수 | `init`/`init --reset` · `backup` · `restore` | DB 초기화/리셋, 백업, 복원 |

- 되돌릴 수 없는 작업(`init --reset`, `delete-collection`, `restore`)은 **확인 체크박스를 켜야** 실행 버튼이 활성화됩니다.
- 화면 동작은 CLI와 동일합니다(같은 함수를 그대로 호출). 첫 실행 시 임베딩 라이브러리 로딩으로 잠깐 느릴 수 있습니다.

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
