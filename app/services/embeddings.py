import threading

import numpy as np

from app.config import get_settings

_lock = threading.Lock()
_model = None

BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def get_model():
    global _model
    with _lock:
        if _model is None:
            from fastembed import TextEmbedding

            _model = TextEmbedding(model_name=get_settings().embedding_model)
        return _model


def embed_texts(texts: list[str]) -> np.ndarray:
    vectors = np.array(list(get_model().embed(texts)), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


def embed_query(query: str) -> np.ndarray:
    settings = get_settings()
    text = f"{BGE_QUERY_PREFIX}{query}" if "bge" in settings.embedding_model.lower() else query
    return embed_texts([text])[0]
