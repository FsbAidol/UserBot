# UserBot

Готовый к развёртыванию на [Heroku](https://heroku.com) пользовательский бот.

## Быстрый старт на Heroku

### 1. Подготовка репозитория
В репозитории уже есть файлы для Heroku:
- `Procfile` — запуск бота как воркеру (`worker: python3 main.py`)
- `requirements.txt` — Python-зависимости
- `runtime.txt` — версия Python
- `.env.example` — пример переменных окружения

Если точка входа вашего бота называется не `main.py`, измените команду в `Procfile`.

### 2. Развёртывание через Heroku CLI

```bash
# Войдите в Heroku
heroku login

# Создайте приложение
heroku create ваш-имя-бота

# Добавьте сборку Python
heroku buildpacks:set heroku/python

# Настройте переменные окружения (скопируйте из .env.example)
heroku config:set API_ID=ваш_api_id
heroku config:set API_HASH=ваш_api_hash
heroku config:set SESSION_STRING=ваша_строка_сессии
heroku config:set BOT_TOKEN=ваш_бот_токен
heroku config:set ADMIN_ID=ваш_id

# Запушьте код
git push heroku arena/cc21a241-userbot:main

# Масштабируйте воркеры
heroku ps:scale worker=1

# Посмотрите логи
heroku logs --tail
```

### 3. Развёртывание через GitHub

1. Подключите этот репозиторий к Heroku в разделе **Deploy** → **GitHub**.
2. Выберите ветку `arena/cc21a241-userbot`.
3. Включите **Automatic deploys** или сделайте ручной деплой.
4. Добавьте переменные окружения в **Settings** → **Config Vars**.

### 3. Локальный запуск

```bash
cp .env.example .env
# отредактируйте .env, вставив свои API_ID, API_HASH и SESSION_STRING

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

## Переменные окружения

| Переменная | Описание | Обязательная |
|------------|----------|--------------|
| `API_ID` | ID приложения Telegram (my.telegram.org) | Да |
| `API_HASH` | Hash приложения Telegram | Да |
| `SESSION_STRING` | Строка сессии Telethon | Да |
| `BOT_TOKEN` | Токен бота от @BotFather | Нет |
| `ADMIN_ID` | ID администратора бота | Нет |
| `LANG` | Язык интерфейса | Нет |

## Лицензия

MIT
