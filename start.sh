#!/bin/bash
# Автоустановка и запуск Heroku UserBot на ctrlfree.host
set -e

echo "=== Heroku UserBot: автоустановка для ctrlfree.host ==="

# 1. Устанавливаем зависимости Python
if [ -f requirements.txt ]; then
    echo "[1/4] Установка Python-зависимостей..."
    pip install --upgrade pip
    pip install -r requirements.txt
fi

# 2. Проверяем переменные окружения
echo "[2/4] Проверка переменных окружения..."
if [ -z "$API_ID" ] || [ -z "$API_HASH" ]; then
    echo "ОШИБКА: Не заданы API_ID и API_HASH!"
    echo "Добавьте их в панели ctrlfree.host (Environment Variables):"
    echo "  API_ID=ваш_api_id"
    echo "  API_HASH=ваш_api_hash"
    echo "Получить их можно на https://my.telegram.org"
    exit 1
fi

# 3. Создаём папку для сессий
mkdir -p sessions

# 4. Запуск бота
echo "[3/4] Запуск Heroku UserBot..."
echo "[4/4] Готово. Бот работает."
exec python3 -m heroku --no-auth
