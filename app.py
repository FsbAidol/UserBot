#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Точка входа для хостингов (ctrlfree.host и подобные).
Сам устанавливает зависимости и запускает Heroku UserBot."""
import os
import sys
import subprocess

# Автоустановка зависимостей, если их нет
try:
    import herokutl  # noqa: F401
except ImportError:
    print("[*] Установка зависимостей, подождите...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])

from heroku.main import Heroku

if __name__ == "__main__":
    bot = Heroku()
    bot.main()
