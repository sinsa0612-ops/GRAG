# SentenceTransformer 임베딩 모델을 격리하는 어댑터 — 다른 모듈은 이 라이브러리의 존재를 모른다.
from functools import lru_cache

from sentence_transformers import SentenceTransformer

from config import settings


# 임베딩 모델을 1회만 로드해 재사용한다.
@lru_cache(maxsize=1)
def _get_model() -> SentenceTransformer:
    return SentenceTransformer(settings.embedding_model_name)


# 텍스트 목록을 임베딩 벡터 목록으로 변환한다.
def embed_texts(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    return model.encode(texts).tolist()
