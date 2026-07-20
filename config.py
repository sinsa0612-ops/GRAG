# 프로젝트 전역 설정 — 모든 경로/모델명/임계값을 이 파일 하나에서만 관리한다 (단일 출처 원칙).
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# 환경설정 단일 출처 클래스 — .env 값을 읽어 경로/모델명/임계값을 제공한다.
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    project_root: Path = Path(__file__).resolve().parent
    db_dir: Path = Path(__file__).resolve().parent / "graphrag_dbs"

    gemini_api_key: str = ""
    llm_model_name: str = "gemini-3.1-flash-lite"
    embedding_model_name: str = "BAAI/bge-m3"
    # 컬렉션(사업)을 따로 지정하지 않고 넣을 때 쓰는 기본 컬렉션 이름.
    default_collection: str = "default"

    # 1200토큰 기준으로 환산한 값. 실제 한국어 문서로 Gemini count_tokens API를 직접 측정해보니
    # 1.63자당 1토큰이었음(1만자 샘플 -> 6121토큰) -> 1200토큰 * 1.63 ≈ 1960자.
    # overlap도 원래 비율(20%)을 그대로 유지해서 392자로 같이 늘림.
    chunk_size: int = 1960
    chunk_overlap: int = 392
    # Gemini 무료 등급 한도는 분당 15회(RPM)다. 매 LLM 호출 뒤 이 간격만큼 쉬어 한도를 보호한다.
    # 실측 처리량이 7~9 RPM에 그쳐 한도까지 여유가 있어 4.5초 -> 3.0초로 당겼다.
    # (요청 자체 지연까지 더하면 실효 ~12 RPM 수준이라 15 미만 안전 마진을 유지한다. 429가 보이면 다시 올린다.)
    llm_request_interval_sec: float = 3.0
    # RPM이 더 낮은 모델(무료 Gemma는 RPM 15)용 호출 간격(초). 15 RPM이면 호출당 4초가 하한이라 4.5초로 안전 마진을 둔다.
    gemma_request_interval_sec: float = 4.5
    # 일시적 오류(서버 과부하 5xx, 429 한도초과)일 때만 재시도한다. 잘못된 요청/인증 오류는 재시도 안 함.
    llm_max_retries: int = 2
    llm_retry_backoff_sec: float = 5.0
    # Gemini 무료 등급 하루 요청 한도(RPD). 요청 수 = 청크 수라, ingest 전에 초과를 예측·차단하는 데 쓴다.
    llm_daily_limit: int = 500
    # gleaning: 청크당 '놓친 것 추가 추출' 라운드 수(MS GraphRAG 방식). 0=끔(기본, 청크당 1회 유지).
    # N>0이면 청크당 최대 (1+N)회 호출 → 요청 수·시간이 그만큼 늘지만 recall(포착량)이 올라간다.
    glean_rounds: int = 0
    # 질문에 답할 때 벡터 검색으로 끌어올 본문 조각 개수(top_k). 값이 클수록 근거는 풍부하나 프롬프트가 길어진다.
    # 8→12로 올려 retrieval recall을 높였다(질문에 이름이 안 적힌 대상도 본문 조각으로 닿게 하기 위함).
    retrieval_top_k: int = 12
    # 그래프 컨텍스트에 표면화할 엔티티 최대 개수. 질문 직접 매칭을 우선 보존한 뒤,
    # 벡터 청크에 등장한 엔티티로 이 개수까지 채운다. 본문 매칭이 그래프를 무한정 부풀리지 않게 막는 상한.
    graph_context_max_entities: int = 20
    merge_similarity_threshold: float = 0.92
    # 컬렉션을 넘는 same_as 브릿지 '제안'에 쓰는 유사도 임계값. 교차 사업의 같은 대상은 설명이 조금씩
    # 달라 병합 임계값보다 약간 느슨하게 둔다. 제안만 하고 실제 연결은 사용자가 결정한다(자동 연결 아님).
    bridge_similarity_threshold: float = 0.90
    # 추출 프롬프트에 붙이는 '기존 엔티티 이름' 힌트의 최대 개수.
    # 전체 이름을 통째로 넣지 않고 현재 청크에 등장하는 이름만 이 개수까지 추려, 입력 토큰이 무한정 늘지 않게 한다.
    max_name_hints: int = 30
    # 백업(backups/)을 최신 몇 개까지 보관할지. 새 백업을 만들 때마다 이 개수를 넘는 오래된 백업은 자동 삭제한다.
    backup_keep: int = 10

    # --- 신규 LLM 백엔드(옵트인) 설정 — 기본 Gemini 경로에는 영향 없음 ---
    # Ollama(로컬 LLM) HTTP 엔드포인트. 로컬 기본 포트가 11434.
    ollama_base_url: str = "http://localhost:11434"
    # 로컬 LLM 백엔드 기본 모델(이미 로컬에 받아둔 모델, 재다운로드 없음).
    ollama_model_name: str = "qwen3:14b"
    # Ollama 요청 타임아웃(초). 로컬 추출 1콜(1900자 청크 → 구조화 엔티티/관계 JSON)이 qwen3:14b에서
    # 실측 ~250초 걸린다(요약 콜은 수십 초). Gemini 폴백이 없는 완전 로컬 구성이라 추출이 유일 경로이므로,
    # 추출 1콜이 완주할 수 있게 여유를 둔다(120초로는 추출이 전부 타임아웃났다 — 실전 검증에서 확인).
    ollama_request_timeout_sec: float = 300.0
    # Claude/Codex CLI 바이너리 경로. 기본은 이름만 두어 PATH에서 찾게 하고(대화형 셸 함수가 아니라
    # 실바이너리가 필요), 비로그인 subprocess가 PATH로 못 찾으면 .env에서 절대경로로 오버라이드한다.
    claude_cli_path: str = "claude"
    codex_cli_path: str = "codex"
    # CLI 백엔드 subprocess 타임아웃(초). 에이전트형 CLI라 단순 API 콜보다 오래 걸릴 수 있다.
    cli_llm_timeout_sec: float = 300.0

    # --- 설명 요약(M1.5, 옵트인 배치) 설정 ---
    # 엔티티 설명 후보가 이 개수 이상 쌓여야 통합 요약을 트리거한다(후보 1개는 통합할 게 없어 스킵, 호출 절약).
    desc_summary_min_candidates: int = 2

    # --- 커뮤니티 탐지(M2, igraph+leidenalg) 설정 — 순수 CPU, LLM 호출 없음 ---
    # Leiden 알고리즘 난수 시드. 고정해야 같은 그래프에서 재탐지해도 같은 커뮤니티가 나온다(테스트 재현성).
    leiden_seed: int = 42
    # 커뮤니티 크기가 이 값을 넘으면 그 유도 서브그래프에 Leiden을 한 번 더 돌려 하위 레벨로 쪼갠다.
    community_max_size: int = 30
    # 계층 재귀가 내려갈 수 있는 최대 레벨(0=최상위). 무한 재귀를 막는 안전판이기도 하다.
    community_max_level: int = 3

    # --- 커뮤니티 리포트(M3, LLM 배치) 설정 — spec-addendum §A 라우팅 정책 ---
    # 대량 배치(하위/중간 레벨) 리포트 생성 기본 백엔드. 무료·무제한이라 수백 콜도 부담 없다.
    # Gemini는 폐기하지 않고(CEO 지시) 이 값을 "gemini"로 바꿔 선택할 수 있게 열어둔다.
    community_report_bulk_backend: str = "ollama"
    # 최상위 레벨(소수·고가치) 리포트 생성 기본 백엔드. 재빌드당 몇십 콜 수준이라 쿼터 부담이 적다.
    community_report_top_backend: str = "claude_cli"
    # 레벨 0(최상위)부터 이 개수만큼의 레벨을 top 백엔드로, 나머지(대량)는 bulk 백엔드로 라우팅한다.
    report_cli_top_levels: int = 1

    # --- 글로벌(map-reduce) 검색(M4, 옵트인 질의) 설정 — spec-addendum §A 라우팅 정책 ---
    # MAP 단계(스코프 내 리포트마다 1콜, 대량) 기본 백엔드. 무료·무제한이라 리포트 수가 많아도 부담 없다.
    # Gemini는 폐기하지 않고(CEO 지시) 이 값을 "gemini"로 바꿔 선택할 수 있게 열어둔다.
    global_search_map_backend: str = "ollama"
    # REDUCE 단계(질의당 1콜, 소수) 기본 백엔드. config로 "claude_cli"를 옵트인하면 고품질 단발 종합이 가능하다.
    global_search_reduce_backend: str = "ollama"
    # 글로벌 검색 기본 레벨(레벨 0 = 최상위, community_max_level 주석과 동일 규칙). --level로 질의별 오버라이드 가능.
    global_search_default_level: int = 0

    @property
    # SQLite 마스터 DB 파일 경로를 계산한다.
    def sqlite_path(self) -> Path:
        return self.db_dir / "master.db"

    @property
    # ChromaDB 저장 디렉터리 경로를 계산한다.
    def chroma_path(self) -> Path:
        return self.db_dir / "chroma_db"

    @property
    # KuzuDB 저장 디렉터리 경로를 계산한다.
    def kuzu_path(self) -> Path:
        return self.db_dir / "kuzu_db"

    @property
    # 처리 대기 중인 파일이 쌓이는 폴더 경로를 계산한다.
    def inbox_dir(self) -> Path:
        return self.project_root / "inbox"

    @property
    # 처리 성공한 파일이 옮겨지는 폴더 경로를 계산한다.
    def processed_dir(self) -> Path:
        return self.project_root / "processed"

    @property
    # 처리 실패한 파일이 옮겨지는 폴더 경로를 계산한다.
    def failed_dir(self) -> Path:
        return self.project_root / "failed"

    @property
    # Gephi 등 외부 도구로 볼 그래프 내보내기 파일(GEXF)이 저장되는 폴더 경로를 계산한다.
    def export_dir(self) -> Path:
        return self.project_root / "exports"


settings = Settings()
