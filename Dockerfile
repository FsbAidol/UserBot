# Heroku Userbot для PaaS без терминала: Amvera, BotHost (bothost.ru) и других
# «app engine», где нельзя ввести команды в консоль.
#
# Логика:
#   * код лежит в /app;
#   * данные — в /data (Amvera) или /app/data (BotHost), supervisor.py находит
#     нужную папку сам и передаёт её боту через --data-root;
#   * CMD запускает deploy/paas/supervisor.py: страница входа с QR-кодом
#     вместо консольного ввода + запуск и перезапуск `python -m heroku`.
#
# На BotHost каталог /app при старте примонтирован из репозитория, поэтому
# зависимости ставим в образ, а не в /app.
#
# Обычный интерактивный запуск в Docker/на VPS — deploy/local/Dockerfile.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    PIP_NO_CACHE_DIR=1 \
    GIT_PYTHON_REFRESH=quiet \
    HEROKU_NO_GIT=1 \
    APP_PORT=8080

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        ffmpeg \
        fonts-dejavu-core \
        git \
        libcairo2 \
        libmagic1 \
        nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --disable-pip-version-check --no-warn-script-location -r requirements.txt \
    && sha256sum requirements.txt | cut -d' ' -f1 > .requirements_hash

COPY . .

EXPOSE 8080

CMD ["python", "deploy/paas/supervisor.py"]
