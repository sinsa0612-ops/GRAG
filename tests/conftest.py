# pytest 전역 fixture — 모든 DB 테스트를 임시 디렉터리로 격리하고 싱글톤 커넥션을 초기화한다.
import os

import pytest

import db.graph_manager as graph_manager
import db.vector_manager as vector_manager
from config import settings


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_dir", tmp_path / "graphrag_dbs")
    monkeypatch.setattr(graph_manager, "_db", None)
    monkeypatch.setattr(graph_manager, "_conn", None)
    monkeypatch.setattr(vector_manager, "_client", None)
    yield


# 실제 LLM(Ollama/Claude CLI)을 호출하는 통합 스모크 테스트는 서비스가 살아있으면 매 실행마다
# 실호출로 수 분씩 걸려 기본 pytest를 느리게(hang처럼) 만든다. 조직 원칙(네트워크·실서비스 없이
# 빠르게 통과)을 지키기 위해, 이름에 real_ollama/real_claude가 든 스모크는 기본 실행에선 건너뛴다.
# 실제로 돌려 품질을 확인하려면 GRAG_RUN_LLM_SMOKE=1 로 명시적으로 옵트인한다.
def pytest_collection_modifyitems(config, items):
    if os.environ.get("GRAG_RUN_LLM_SMOKE") == "1":
        return
    skip_llm = pytest.mark.skip(reason="실LLM 스모크: GRAG_RUN_LLM_SMOKE=1 일 때만 실행")
    for item in items:
        if "real_ollama" in item.name or "real_claude" in item.name:
            item.add_marker(skip_llm)
