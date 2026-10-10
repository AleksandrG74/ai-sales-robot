from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Optional

import numpy as np

from core.config import settings

log = logging.getLogger("core.embeddings")
log.setLevel(settings.log_level.upper())


class Embedder:
    def __init__(
        self,
        model_name: Optional[str] = None,
        cache_dir: Optional[str] = None,
        device: str = "cpu",
        normalize: bool = True,
        cache_size: int = 10000,
    ) -> None:
        self.model_name = model_name or settings.embeddings_model
        self.cache_dir = str(cache_dir or settings.embeddings_cache)
        self.device = device
        self.normalize = normalize
        self.cache_size = cache_size

        self._model = None
        self._model_dim: Optional[int] = None
        self._load_lock = threading.Lock()

        self._cache: "OrderedDict[str, np.ndarray]" = OrderedDict()
        self._cache_lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    # ---------- model loading ----------
    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            try:
                from sentence_transformers import SentenceTransformer
            except Exception as exc:
                log.exception("sentence-transformers is not available: %s", exc)
                raise RuntimeError(
                    "sentence-transformers not installed or torch missing"
                ) from exc

            log.info(
                "Loading embedding model '%s' on '%s' (cache=%s)",
                self.model_name, self.device, self.cache_dir,
            )
            import time as _time
            t0 = _time.perf_counter()
            try:
                self._model = SentenceTransformer(
                    self.model_name,
                    cache_folder=self.cache_dir,
                    device=self.device,
                )
            except Exception as exc:
                log.exception("Failed to load model '%s': %s", self.model_name, exc)
                raise
            try:
                self._model_dim = int(self._model.get_sentence_embedding_dimension())
            except Exception:
                self._model_dim = None
            dt = _time.perf_counter() - t0
            log.info("Model loaded in %.2fs, dim=%s", dt, self._model_dim)

    # ---------- cache ----------
    def _cache_get(self, key: str) -> Optional[np.ndarray]:
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._hits += 1
                return self._cache[key]
            self._misses += 1
            return None

    def _cache_put(self, key: str, value: np.ndarray) -> None:
        with self._cache_lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

    # ---------- encode ----------
    def encode(self, text: str) -> np.ndarray:
        if text is None:
            text = ""
        key = text
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        self._ensure_model()
        assert self._model is not None
        try:
            vec = self._model.encode(
                [text],
                normalize_embeddings=self.normalize,
                convert_to_numpy=True,
                show_progress_bar=False,
            )[0]
        except Exception as exc:
            log.exception("encode failed for text=%r: %s", text[:80], exc)
            raise

        vec = np.asarray(vec, dtype=np.float32)
        self._cache_put(key, vec)
        return vec

    def encode_batch(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)

        keys = [t if t is not None else "" for t in texts]
        result: list[Optional[np.ndarray]] = [None] * len(keys)
        missing_idx: list[int] = []
        missing_keys: list[str] = []

        for i, k in enumerate(keys):
            c = self._cache_get(k)
            if c is not None:
                result[i] = c
            else:
                missing_idx.append(i)
                missing_keys.append(k)

        if missing_keys:
            self._ensure_model()
            assert self._model is not None
            try:
                vecs = self._model.encode(
                    missing_keys,
                    normalize_embeddings=self.normalize,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                    batch_size=32,
                )
            except Exception as exc:
                log.exception("encode_batch failed (%d texts): %s", len(missing_keys), exc)
                raise
            vecs = np.asarray(vecs, dtype=np.float32)
            for j, i in enumerate(missing_idx):
                v = vecs[j]
                result[i] = v
                self._cache_put(missing_keys[j], v)

        return np.stack([r for r in result if r is not None], axis=0)  # type: ignore[arg-type]

    # ---------- similarity ----------
    @staticmethod
    def cosine(v1: np.ndarray, v2: np.ndarray) -> float:
        a = np.asarray(v1, dtype=np.float32).ravel()
        b = np.asarray(v2, dtype=np.float32).ravel()
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))

    @staticmethod
    def cosine_batch(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
        q = np.asarray(query, dtype=np.float32).ravel()
        m = np.asarray(matrix, dtype=np.float32)
        if m.ndim == 1:
            m = m.reshape(1, -1)
        nq = float(np.linalg.norm(q))
        if nq == 0.0:
            return np.zeros(m.shape[0], dtype=np.float32)
        norms = np.linalg.norm(m, axis=1)
        denom = norms * nq
        denom[denom == 0.0] = 1.0
        return (m @ q) / denom

    # ---------- properties / helpers ----------
    @property
    def dim(self) -> int:
        if self._model_dim is None:
            self._ensure_model()
        return int(self._model_dim or 0)

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def cache_info(self) -> dict[str, int]:
        with self._cache_lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._cache),
                "max_size": self.cache_size,
            }


# ============================================================
# Singleton
# ============================================================
_embedder_instance: Optional[Embedder] = None
_embedder_lock = threading.Lock()


def get_embedder() -> Embedder:
    global _embedder_instance
    if _embedder_instance is None:
        with _embedder_lock:
            if _embedder_instance is None:
                _embedder_instance = Embedder()
    return _embedder_instance
