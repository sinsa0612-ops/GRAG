# pytest 전역 fixture — 모든 DB 테스트를 임시 디렉터리로 격리하고 싱글톤 커넥션을 초기화한다.
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
