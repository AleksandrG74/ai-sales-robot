"""
app.py — FastAPI веб-интерфейс dialogues-miner.

Читает JSON-треды из data/raw_threads/, показывает статистику:
  - главная: всего тредов, по каналам, по группам
  - /channels: таблица по каналам
  - /dialogues/{file}: просмотр конкретного треда
  - /download: скачать всё одним архивом
"""
from __future__ import annotations

import io
import json
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web.auth import check_auth

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

THREADS_DIR = PROJECT_ROOT / "data" / "raw_threads"
TEMPLATES_DIR = PROJECT_ROOT / "web" / "templates"
STATIC_DIR = PROJECT_ROOT / "web" / "static"

app = FastAPI(title="dialogues-miner", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def load_index() -> dict[str, Any]:
    """Читает _index.json."""
    path = THREADS_DIR / "_index.json"
    if not path.exists():
        return {"total": 0, "dialogues": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_dialogue(filename: str) -> dict[str, Any]:
    """Читает один JSON-тред."""
    path = THREADS_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Тред не найден")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_stats() -> dict[str, Any]:
    """Собирает статистику по каналам и группам."""
    index = load_index()
    dialogues = index.get("dialogues", [])

    by_channel = Counter(d["sender"] for d in dialogues)
    by_group = Counter()
    words_total = 0
    target_total = 0

    # Группы из sources.json
    sources_path = PROJECT_ROOT / "sources.json"
    group_map: dict[str, str] = {}
    if sources_path.exists():
        with open(sources_path, "r", encoding="utf-8") as f:
            sources = json.load(f)
        for s in sources.get("sources", []):
            group_map[s["name"]] = s.get("group", "other")

    for d in dialogues:
        grp = group_map.get(d["sender"], "other")
        by_group[grp] += 1
        words_total += d.get("words_count", 0)
        target_total += d.get("target_count", 0)

    avg_words = round(words_total / len(dialogues), 1) if dialogues else 0

    return {
        "total": len(dialogues),
        "by_channel": by_channel.most_common(),
        "by_group": by_group.most_common(),
        "avg_words": avg_words,
        "target_total": target_total,
        "channels_count": len(by_channel),
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: str = Depends(check_auth)):
    stats = build_stats()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "stats": stats, "user": user},
    )


@app.get("/channels", response_class=HTMLResponse)
def channels(request: Request, user: str = Depends(check_auth)):
    stats = build_stats()
    return templates.TemplateResponse(
        "channels.html",
        {"request": request, "stats": stats, "user": user},
    )


@app.get("/dialogues", response_class=HTMLResponse)
def dialogues_list(request: Request, user: str = Depends(check_auth)):
    index = load_index()
    return templates.TemplateResponse(
        "dialogues.html",
        {"request": request, "index": index, "user": user},
    )


@app.get("/dialogues/{filename}", response_class=HTMLResponse)
def dialogue_detail(
    filename: str,
    request: Request,
    user: str = Depends(check_auth),
):
    dialogue = load_dialogue(filename)
    return templates.TemplateResponse(
        "dialogue.html",
        {"request": request, "d": dialogue, "user": user},
    )


@app.get("/download")
def download_all(user: str = Depends(check_auth)):
    """Скачать все JSON-треды одним ZIP-архивом."""
    if not THREADS_DIR.exists():
        raise HTTPException(status_code=404, detail="Нет данных")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in THREADS_DIR.glob("*.json"):
            zf.write(p, p.name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=threads.zip"},
    )