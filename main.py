#!/usr/bin/env python3
"""Точка входа Heroku Userbot для PaaS (BotHost, Amvera и подобных).

Зачем нужен этот файл
---------------------
`python -m heroku` (и `heroku/main.py`) при первом запуске просят API ID,
телефон и код из Telegram через stdin. На хостинге без терминала это падает
с EOFError.

main.py — тонкая обёртка над deploy/paas/supervisor.py: он поднимает страницу
входа с QR-кодом, сохраняет сессию на постоянный диск и запускает юзербота.
На BotHost достаточно указать «Главный файл (точка входа)» = main.py.
"""

import runpy
import sys
from pathlib import Path

SUPERVISOR = Path(__file__).resolve().parent / "deploy" / "paas" / "supervisor.py"

if __name__ == "__main__":
    if not SUPERVISOR.is_file():
        sys.exit(f"Не найден {SUPERVISOR}: загружен неполный репозиторий")

    sys.argv[0] = str(SUPERVISOR)
    runpy.run_path(str(SUPERVISOR), run_name="__main__")
