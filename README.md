# GraphRAG — 개인용 단독 GraphRAG

**컬렉션(사업) 격벽 + 통합 CLI**를 갖춘 개인용 GraphRAG 시스템입니다.
문서를 넣으면 LLM이 엔티티·관계를 뽑아 **그래프 + 벡터**로 저장하고, 질문하면 그 근거만으로 답합니다.

- **컬렉션 격벽:** 사업마다 그래프를 격리해 교차 오염을 막고, 필요할 때만 `--all`로 종합합니다.
- **3중 DB:** SQLite(마스터/문서·해시·사용량·계층) + ChromaDB(벡터) + KùzuDB(그래프).
- **근거 기반 답변:** 검색된 본문 조각을 1차 근거로, 그래프를 보조 힌트로 써서 환각을 억제합니다.
- **가성비 추출:** 무료 Gemini Flash-lite 기본, 필요 시 gleaning(`--glean N`)·모델 토글(Gemma)을 옵트인.

> 자세한 사용법은 [USAGE.md](USAGE.md)를 참고하세요.

---

## 요구 사항

- **Python 3.12+**
- Gemini API 키 (무료 등급 가능)
- 첫 실행 시 임베딩 모델 `BAAI/bge-m3`(약 2GB)를 HuggingFace에서 자동 내려받습니다.

## 설치 (최초 1회)

```bash
git clone https://github.com/sinsa0612-ops/GRAG.git
cd GRAG
python -m venv .venv
.venv\Scripts\activate            # Windows (PowerShell/CMD)
# source .venv/bin/activate       # macOS/Linux

pip install -r requirements.txt
pip install -e . --config-settings editable_mode=compat   # graphrag CLI 등록
```

## 환경 변수

`.env`는 저장소에 포함되지 않습니다(민감정보). 아래처럼 직접 만듭니다.

```bash
copy .env.example .env    # Windows  (macOS/Linux: cp .env.example .env)
```

그 후 `.env`를 열어 발급받은 키를 채웁니다.

```
GEMINI_API_KEY=발급받은_키
```

## 빠른 시작

```bash
graphrag init                                    # DB 스키마 생성
graphrag ingest "메모.md" --collection 사업A      # 문서 넣기(추출+그래프)
graphrag query "김부장은 무슨 일을 해?" --collection 사업A   # 한 사업에 질문
graphrag query "이번 주 전체 사업 요약" --all          # 전체 종합
graphrag collections                             # 사업 목록/현황
streamlit run app.py                             # 웹 UI(모든 명령을 버튼으로)
```

전체 명령·옵션·한도 관리·평가(eval)·그래프 내보내기는 [USAGE.md](USAGE.md)에 있습니다.

---

## 저장소에 포함되지 않는 것 (로컬 전용)

아래는 `.gitignore`로 제외됩니다. 다른 PC에서 이어서 쓰려면 별도로 옮기거나 새로 생성해야 합니다.

| 경로 | 내용 | 다른 PC에서 |
|------|------|-------------|
| `.env` | Gemini API 키 | **직접 생성**(위 참고) |
| `graphrag_dbs/` | 실제 3중 DB(적재된 데이터) | 데이터를 이어가려면 복사, 아니면 `graphrag init` 후 재적재 |
| `processed/` | 적재 완료된 원본 문서 | 재적재하려면 복사 |
| `backups/`, `exports/` | 백업·그래프 내보내기 | 재생성 가능(불필요) |
| `.venv/` | 가상환경 | **복사 금지** — 위 설치로 새로 생성 |

## 프로젝트 문서

- [USAGE.md](USAGE.md) — 전체 사용 가이드
- [AI-INSTRUCTIONS.md](AI-INSTRUCTIONS.md) — 아키텍처 규칙(개발 헌법)
- [HANDOFF-main.md](HANDOFF-main.md) / [HANDOFF-sub.md](HANDOFF-sub.md) — 마일스톤 / 변경 로그

## 라이선스

[MIT License](LICENSE) — 자유롭게 사용·수정·재배포할 수 있습니다.
