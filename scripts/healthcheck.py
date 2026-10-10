#!/usr/bin/env python
"""Healthcheck для Docker.

Проверяет:
  1. Процесс orchestrator.main жив (или его дочерние сервисы).
  2. HTTP web-backend отвечает на /health.

Возвращает:
  0 — всё ок
  1 — что-то упало
"""
from __future__ import annotations

import os
import subprocess
import sys
import urllib.request


def check_processes() -> bool:
    """Проверяет, что в контейнере живут 4 подпроцесса."""
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "cmd"], text=True, stderr=subprocess.DEVNULL
        )
    except Exception as exc:
        print(f"[healthcheck] ps failed: {exc}", file=sys.stderr)
        return False

    expected_markers = [
        "orchestrator.main",
        "workers.dialogue.main",
        "workers.telegram.main",
        "uvicorn web.backend.main",
    ]
    for marker in expected_markers:
        if marker not in out:
            print(f"[healthcheck] NOT FOUND in ps: {marker}", file=sys.stderr)
            return False
    return True


def check_http() -> bool:
    """Проверяет web-backend."""
    port = os.getenv("WEB_BACKEND_PORT", "8000")
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            if resp.status != 200:
                print(f"[healthcheck] HTTP {resp.status} from {url}", file=sys.stderr)
                return False
        return True
    except Exception as exc:
        print(f"[healthcheck] HTTP check failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    ok_proc = check_processes()
    ok_http = check_http()
    if ok_proc and ok_http:
        print("[healthcheck] OK")
        return 0
    print(
        f"[healthcheck] FAILED: processes={ok_proc}, http={ok_http}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
