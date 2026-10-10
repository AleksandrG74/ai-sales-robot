"""Мини-тест этапа 3: проверка core.llm + core.embeddings.

Запуск (из корня проекта, с активированным venv):
    python -m scripts.test_core
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from core.config import settings
from core.embeddings import get_embedder
from core.llm import ChatMessage, get_llm_client


def test_settings() -> None:
    print("\n" + "=" * 60)
    print("TEST 1: settings")
    print("=" * 60)
    print(f"  robot_db_path       : {settings.robot_db_path}")
    print(f"  deepseek_base_url   : {settings.deepseek_base_url}")
    print(f"  deepseek_model      : {settings.deepseek_model}")
    print(f"  deepseek_api_key    : {'set' if settings.deepseek_api_key else 'NOT SET'}")
    print(f"  embeddings_model    : {settings.embeddings_model}")
    print(f"  embeddings_cache    : {settings.embeddings_cache}")
    if not settings.deepseek_api_key:
        print("  ! DEEPSEEK_API_KEY не задан — LLM-тесты будут пропущены")


def test_embeddings() -> None:
    print("\n" + "=" * 60)
    print("TEST 2: Embedder")
    print("=" * 60)

    t0 = time.perf_counter()
    emb = get_embedder()
    print(f"  embedder created in {time.perf_counter() - t0:.2f}s")

    t0 = time.perf_counter()
    v1 = emb.encode("Здравствуйте, меня интересует ваш товар")
    t_load = time.perf_counter() - t0
    print(f"  first encode (with model load): {t_load:.2f}s")
    print(f"  vector shape: {v1.shape}, dtype: {v1.dtype}")
    print(f"  vector norm : {float((v1 ** 2).sum() ** 0.5):.4f}  (должно быть ~1.0)")

    t0 = time.perf_counter()
    v2 = emb.encode("Привет, расскажите подробнее о ценах")
    print(f"  second encode (новый текст): {time.perf_counter() - t0:.4f}s")

    t0 = time.perf_counter()
    v1_again = emb.encode("Здравствуйте, меня интересует ваш товар")
    print(f"  third encode (cached — тот же текст): {time.perf_counter() - t0:.4f}s")

    sim_close = emb.cosine(v1, v1_again)
    sim_far = emb.cosine(v1, v2)
    print(f"  cosine(v1, v1_same_text) = {sim_close:.4f}  (должно быть 1.0)")
    print(f"  cosine(v1, v2_diff_text) = {sim_far:.4f}  (обычно 0.3-0.8)")

    info = emb.cache_info()
    print(f"  cache: {info}")

    vecs = emb.encode_batch(["да", "нет", "ок", "база"])
    print(f"  batch shape: {vecs.shape}  (ожидаем (4, 312))")
    print(f"  dim property: {emb.dim}")


def test_llm_sync() -> None:
    print("\n" + "=" * 60)
    print("TEST 3: LLMClient.chat_sync")
    print("=" * 60)
    if not settings.deepseek_api_key:
        print("  SKIPPED (no API key)")
        return

    llm = get_llm_client()
    t0 = time.perf_counter()
    resp = llm.chat_sync(
        messages=[ChatMessage(role="user", content="Скажи одним словом: 'работает'?")],
        system="Отвечай очень коротко.",
        temperature=0.0,
        max_tokens=20,
    )
    dt = time.perf_counter() - t0
    print(f"  ответ    : {resp.text!r}")
    print(f"  модель   : {resp.model}")
    print(f"  finish   : {resp.finish_reason}")
    print(f"  tokens   : in={resp.tokens_in}, out={resp.tokens_out}")
    print(f"  latency  : {resp.latency_ms:.0f}ms (замер снаружи: {dt * 1000:.0f}ms)")
    print(f"  str(resp): {str(resp)!r}  (должен совпадать с текстом)")

    llm.close()


async def test_llm_async() -> None:
    print("\n" + "=" * 60)
    print("TEST 4: LLMClient.chat (async)")
    print("=" * 60)
    if not settings.deepseek_api_key:
        print("  SKIPPED (no API key)")
        return

    llm = get_llm_client()
    resp = await llm.chat(
        messages=[{"role": "user", "content": "Назови одно число от 1 до 10."}],
        temperature=0.0,
        max_tokens=10,
    )
    print(f"  ответ  : {resp.text!r}")
    print(f"  tokens : in={resp.tokens_in}, out={resp.tokens_out}")

    print("\n  --- chat_stream ---")
    chunks = []
    async for chunk in llm.chat_stream(
        messages=[{"role": "user", "content": "Сосчитай от 1 до 5 через запятую."}],
        temperature=0.0,
        max_tokens=30,
    ):
        chunks.append(chunk)
        print(f"  chunk: {chunk!r}")
    print(f"  всего чанков: {len(chunks)}")
    print(f"  полный текст: {''.join(chunks)!r}")

    await llm.aclose()


def main() -> int:
    try:
        test_settings()
        test_embeddings()
        test_llm_sync()
        asyncio.run(test_llm_async())
    except Exception as exc:
        print(f"\n!!! FAILED: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
