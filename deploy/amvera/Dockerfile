# Образ Heroku Userbot для Amvera Cloud (и любого PaaS без терминала).
#
# Отличия от корневого Dockerfile:
#   * код берётся из этого репозитория, а не клонируется с GitHub на сборке;
#   * код лежит в /app, а /data остаётся папкой данных (Amvera монтирует туда
#     постоянное хранилище, и если положить код в /data, он будет перекрыт);
#   * вместо интерактивного `python -m heroku` запускается supervisor.py,
#     который поднимает страницу входа с QR-кодом и следит за процессом.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    PIP_NO_CACHE_DIR=1 \
    DOCKER=1 \
    HEROKU_NO_GIT=1 \
    GIT_PYTHON_REFRESH=quiet \
    HEROKU_DATA_DIR=/data \
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

RUN mkdir -p /data/sessions && chmod 700 /data/sessions

EXPOSE 8080

CMD ["python", "deploy/amvera/supervisor.py"]
