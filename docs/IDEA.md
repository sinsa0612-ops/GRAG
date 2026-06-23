# 개인용 단독 가성비 GraphRAG 아키텍처 및 초기화 스크립트

이 문서는 외부 프레임워크(LlamaIndex, MS GraphRAG 등) 없이 순수 파이썬과 로컬 임베디드 데이터베이스 3종을 활용하여 구축하는 개인용 GraphRAG 시스템의 설계도와 초기 세팅 코드를 담고 있습니다.

---

## 1. 시스템 핵심 아키텍처 (5 Point Summary)

1.  **3중 데이터베이스 연계:**
    * `SQLite (Master)`: 문서 원본 텍스트, 파일 해시값, 업데이트 상태를 관리하는 중앙 지휘소.
    * `ChromaDB (Vector)`: 텍스트 청크를 임베딩(bge-m3 등)하여 의미 기반으로 가장 빠른 1차 검색을 수행.
    * `KùzuDB (Graph)`: 추출된 엔티티와 관계를 저장하고, 2-hop 이상의 관계망 추적을 담당.
2.  **비용 효율적 추출 및 병합:**
    * Google AI Studio의 `Gemini 3.1 Flash lite` (무료 API)를 활용하여 정형화된 JSON 스키마 기반 추출.
    * 로컬 임베딩 모델(BGE-m3)의 코사인 유사도를 활용하여 파편화된 노드를 비용($0) 없이 병합(Entity Resolution).
3.  **동명이인 및 시간 맥락 방어:**
    * 노드 추출 시 이름(`name`)뿐만 아니라 설명(`description`)과 타입(`type`)을 함께 추출하여 동명이인 분리.
    * 엣지(관계선)에 `valid_from`, `source_doc` 등의 메타데이터를 기록하여 변경 이력과 인과관계 추적.
4.  **라우팅(Routing) 및 프롬프트 최적화:**
    * 질문의 복잡도에 따라 일반 LLM, Vector 검색, Graph+Vector 하이브리드 검색을 분기하여 API 한도 절약.
5.  **유지보수 및 예외 처리:**
    * 증분 업데이트(Incremental Update)를 위해 SQLite의 해시값 변경 감지 후 부분 삭제/재삽입(Cascade Delete) 구현.
    * 사용자가 직접 병합 예외를 설정할 수 있는 블랙리스트 테이블 운영.

---

## 2. DB 초기화 세팅 스크립트 (`init_dbs.py`)

시스템 구동을 위한 디렉토리를 생성하고 3개의 데이터베이스에 필수 테이블과 컬렉션을 세팅하는 파이썬 스크립트입니다. 테스트를 위한 샘플 노드 삽입 로직이 포함되어 있습니다.

### 필수 라이브러리 설치
```bash
pip install sqlite3 chromadb kuzu