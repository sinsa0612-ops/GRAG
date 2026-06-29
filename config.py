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
    # 일시적 오류(서버 과부하 5xx, 429 한도초과)일 때만 재시도한다. 잘못된 요청/인증 오류는 재시도 안 함.
    llm_max_retries: int = 2
    llm_retry_backoff_sec: float = 5.0
    # Gemini 무료 등급 하루 요청 한도(RPD). 요청 수 = 청크 수라, ingest 전에 초과를 예측·차단하는 데 쓴다.
    llm_daily_limit: int = 500
    merge_similarity_threshold: float = 0.92
    # 컬렉션을 넘는 same_as 브릿지 '제안'에 쓰는 유사도 임계값. 교차 사업의 같은 대상은 설명이 조금씩
    # 달라 병합 임계값보다 약간 느슨하게 둔다. 제안만 하고 실제 연결은 사용자가 결정한다(자동 연결 아님).
    bridge_similarity_threshold: float = 0.90
    # 추출 프롬프트에 붙이는 '기존 엔티티 이름' 힌트의 최대 개수.
    # 전체 이름을 통째로 넣지 않고 현재 청크에 등장하는 이름만 이 개수까지 추려, 입력 토큰이 무한정 늘지 않게 한다.
    max_name_hints: int = 30
    # 백업(backups/)을 최신 몇 개까지 보관할지. 새 백업을 만들 때마다 이 개수를 넘는 오래된 백업은 자동 삭제한다.
    backup_keep: int = 10

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
