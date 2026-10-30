#!/usr/bin/env python3
"""Точка входа Heroku Userbot для Amvera Cloud (и любого PaaS без терминала).

Зачем это нужно
---------------
Heroku Userbot при первом запуске спрашивает API ID, API hash, телефон и код
из Telegram через stdin. На Amvera консоли нет, поэтому обычный запуск
`python -m heroku` падает с EOFError.

Этот скрипт заменяет терминал веб-страницей:

1. поднимает HTTP-сервер на порту приложения (Amvera ждёт открытый порт);
2. если в /data ещё нет сессии — показывает страницу входа со QR-кодом
   (Telegram → Настройки → Устройства → Подключить устройство);
3. после успешного входа сохраняет сессию в /data/sessions и запускает
   сам юзербот (`python -m heroku --root`) как дочерний процесс;
4. следит за процессом юзербота и перезапускает его при падении.

Доступ к странице входа защищён ключом: он печатается в логах приложения
(или задаётся переменной AMVERA_LOGIN_TOKEN).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP_DIR))

DATA_DIR = Path(os.environ.get("HEROKU_DATA_DIR", "/data"))
SESSIONS_DIR = DATA_DIR / "sessions"
CONFIG_PATH = DATA_DIR / "config.json"

# containerPort в amvera.yml должен совпадать с этим портом
PORT = int(os.environ.get("APP_PORT", "8080"))

# Ключ доступа к странице входа. Печатается в логах при старте.
LOGIN_KEY = (
    os.environ.get("AMVERA_LOGIN_TOKEN")
    or os.environ.get("LOGIN_TOKEN")
    or secrets.token_urlsafe(9)
)

BOT_CMD = [sys.executable, "-m", "heroku", "--root"]
BOT_CWD = str(APP_DIR)
RESTART_DELAYS = [3, 5, 10, 20, 30]

STOP = threading.Event()
STATE_LOCK = threading.RLock()

STATE: dict = {
    "phase": "init",  # init | no_creds | login | password | logged_in | running
    "detail": "",
    "qr_url": "",
    "qr_svg": "",
    "account": "",
    "restarts": 0,
    "last_exit": "",
    "bot_started_at": 0.0,
    "pw_event": None,
    "pw_value": None,
    "loop": None,
}


def log(message: str) -> None:
    print(f"[amvera] {message}", flush=True)


def set_state(phase: str | None = None, detail: str | None = None, **kwargs) -> None:
    with STATE_LOCK:
        if phase is not None:
            STATE["phase"] = phase
        if detail is not None:
            STATE["detail"] = detail
        STATE.update(kwargs)


def snapshot() -> dict:
    with STATE_LOCK:
        return {
            "phase": STATE["phase"],
            "detail": STATE["detail"],
            "account": STATE["account"],
            "has_qr": bool(STATE["qr_svg"]),
            "restarts": STATE["restarts"],
            "last_exit": STATE["last_exit"],
            "uptime": int(time.time() - STATE["bot_started_at"])
            if STATE["bot_started_at"]
            else 0,
        }


# --------------------------------------------------------------------------- #
#  Работа с файлами данных (/data)
# --------------------------------------------------------------------------- #


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(SESSIONS_DIR, 0o700)
    except OSError:
        pass


def read_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_creds() -> tuple[int, str] | None:
    config = read_config()
    api_id = os.environ.get("api_id") or os.environ.get("API_ID") or config.get("api_id")
    api_hash = (
        os.environ.get("api_hash")
        or os.environ.get("API_HASH")
        or config.get("api_hash")
    )

    try:
        api_id_int = int(str(api_id).strip())
    except (TypeError, ValueError):
        return None

    api_hash_str = str(api_hash or "").strip()
    if len(api_hash_str) != 32 or any(ch not in "0123456789abcdefABCDEF" for ch in api_hash_str):
        return None

    return api_id_int, api_hash_str


def save_config(api_id: int, api_hash: str) -> None:
    config = read_config()
    config["api_id"] = api_id
    config["api_hash"] = api_hash
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=4), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)


def existing_sessions() -> list[Path]:
    try:
        return sorted(
            path
            for path in SESSIONS_DIR.iterdir()
            if path.name.startswith("heroku-")
            and path.name.endswith(".session")
            and path.stat().st_size > 0
        )
    except FileNotFoundError:
        return []


# --------------------------------------------------------------------------- #
#  QR-код в SVG (используем кодировщик из самого юзербота)
# --------------------------------------------------------------------------- #


def qr_svg(url: str) -> str:
    try:
        from heroku.qr import QRCode
    except Exception:  # pragma: no cover - исходники должны быть на месте
        return ""

    code = QRCode(border=2)
    code.add_data(url)
    code.make()

    border = code.border
    count = code.modules_count
    side = count + border * 2

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" '
        f'width="360" height="360" shape-rendering="crispEdges" '
        f'aria-label="Telegram QR">',
        f'<rect width="{side}" height="{side}" fill="#ffffff"/>',
    ]

    path = []
    for row in range(count):
        line = code.modules[row]
        for col in range(count):
            if line[col]:
                path.append(f"M{col + border} {row + border}h1v1h-1z")

    parts.append(f'<path d="{"".join(path)}" fill="#000000"/>')
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
#  Вход по QR-коду
# --------------------------------------------------------------------------- #


async def _wait_password() -> str:
    loop = asyncio.get_running_loop()
    event = asyncio.Event()
    with STATE_LOCK:
        STATE["loop"] = loop
        STATE["pw_event"] = event
        STATE["pw_value"] = None

    try:
        await asyncio.wait_for(event.wait(), timeout=900)
    finally:
        with STATE_LOCK:
            STATE["pw_event"] = None

    return str(STATE["pw_value"] or "")


def _store_session(client, tg_id: int) -> None:
    from herokutl.sessions import SQLiteSession

    ensure_dirs()
    path = SESSIONS_DIR / f"heroku-{tg_id}"
    session = SQLiteSession(str(path))
    try:
        session.set_dc(
            client.session.dc_id,
            client.session.server_address,
            client.session.port,
        )
        session.auth_key = client.session.auth_key
        session.save()
    finally:
        try:
            session.close()
        except Exception:
            pass

    try:
        os.chmod(f"{path}.session", 0o600)
    except OSError:
        pass

    for extra in SESSIONS_DIR.glob("heroku-*.session-journal"):
        try:
            extra.unlink()
        except OSError:
            pass


async def login_flow(api_id: int, api_hash: str) -> None:
    from herokutl import TelegramClient
    from herokutl.errors import SessionPasswordNeededError
    from herokutl.sessions import MemorySession

    save_config(api_id, api_hash)

    client = TelegramClient(
        MemorySession(),
        api_id,
        api_hash,
        device_model="Heroku Userbot",
        system_version="Linux",
        app_version="2.2 x64",
        lang_code="en",
        system_lang_code="en-US",
        connection_retries=None,
    )

    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            _store_session(client, me.id)
            set_state("logged_in", f"Уже авторизован как {me.id}", account=str(me.id))
            return

        qr = await client.qr_login()
        set_state("login", "Отсканируйте QR-код в Telegram", qr_url=qr.url, qr_svg=qr_svg(qr.url))

        while True:
            try:
                await qr.wait(25)
                break
            except asyncio.TimeoutError:
                await qr.recreate()
                set_state(qr_url=qr.url, qr_svg=qr_svg(qr.url))
            except SessionPasswordNeededError:
                set_state("password", "Включён облачный пароль (2FA)")
                password = await _wait_password()
                await client.sign_in(password=password)
                break

        me = await client.get_me()
        _store_session(client, me.id)
        set_state(
            "logged_in",
            f"Вход выполнен: {me.id}",
            account=f"{me.id}" + (f" (@{me.username})" if me.username else ""),
            qr_svg="",
        )
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def login_worker() -> None:
    """Крутится в отдельном потоке: ждёт креды, показывает QR, пишет сессию."""

    failures = 0

    while not STOP.is_set():
        if existing_sessions():
            # Не перебиваем статус работающего юзербота
            with STATE_LOCK:
                if STATE["phase"] not in {"running", "logged_in"}:
                    set_state("idle", "")
            STOP.wait(3)
            continue

        creds = get_creds()
        if not creds:
            set_state(
                "no_creds",
                "Добавьте переменные api_id и api_hash в настройках проекта",
            )
            STOP.wait(5)
            continue

        try:
            asyncio.run(login_flow(*creds))
        except Exception as error:  # сеть, блокировки, неверные креды и т.п.
            failures += 1
            detail = f"{type(error).__name__}: {error}"
            log(f"вход не удался: {detail}")
            traceback.print_exc()
            set_state("login_error", detail, qr_svg="")
            delay = [10, 20, 40, 60][min(failures - 1, 3)]
            log(f"повторная попытка входа через {delay} с")
            STOP.wait(delay)
            continue

        failures = 0
        STOP.wait(1)


# --------------------------------------------------------------------------- #
#  Супервизор процесса юзербота
# --------------------------------------------------------------------------- #


def spawn_bot() -> subprocess.Popen:
    env = dict(os.environ)
    env.setdefault("DOCKER", "true")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("HEROKU_NO_GIT", "1")
    env.setdefault("GIT_PYTHON_REFRESH", "quiet")

    log("запускаю юзербота: " + " ".join(BOT_CMD))
    return subprocess.Popen(
        BOT_CMD,
        cwd=BOT_CWD,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def stop_bot(proc: subprocess.Popen | None, timeout: int = 20) -> None:
    if proc is None or proc.poll() is not None:
        return

    log("останавливаю юзербота")
    try:
        proc.terminate()
        proc.wait(timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def supervise() -> None:
    proc: subprocess.Popen | None = None
    restarts = 0
    started_at = 0.0

    while not STOP.is_set():
        if not existing_sessions():
            if proc is not None:
                stop_bot(proc)
                proc = None
                set_state("idle", "", bot_started_at=0.0)
            STOP.wait(2)
            continue

        if proc is None or proc.poll() is not None:
            if not get_creds():
                set_state(
                    "no_creds",
                    "Добавьте переменные api_id и api_hash в настройках проекта",
                )
                STOP.wait(3)
                continue

            if proc is not None:
                code = proc.returncode
                lived = time.time() - started_at
                set_state(last_exit=f"код {code}, работал {int(lived)} с")
                log(f"юзербот завершился (код {code}) после {int(lived)} с")
                restarts = restarts + 1 if lived < 180 else 1
                proc = None
                if STOP.is_set():
                    break
                delay = RESTART_DELAYS[min(restarts - 1, len(RESTART_DELAYS) - 1)]
                log(f"перезапуск через {delay} с (перезапуск №{restarts})")
                STOP.wait(delay)
                if STOP.is_set():
                    break

            proc = spawn_bot()
            started_at = time.time()
            set_state(
                "running",
                "Юзербот запущен",
                restarts=restarts,
                bot_started_at=started_at,
            )
            continue

        # Процесс жив: держим актуальный статус и ждём
        STOP.wait(2)

    stop_bot(proc)


# --------------------------------------------------------------------------- #
#  Веб-интерфейс
# --------------------------------------------------------------------------- #

PHASE_TEXT = {
    "init": ("Запуск", "Готовлю контейнер…"),
    "no_creds": (
        "Нужны API ID и API hash",
        "Откройте «Переменные» проекта в Amvera и добавьте api_id и api_hash "
        "(их выдаёт my.telegram.org → API development tools). После этого "
        "перезапустите приложение.",
    ),
    "login": (
        "Вход в Telegram",
        "Telegram → Настройки → Устройства → Подключить устройство и отсканируйте код. "
        "Код обновляется автоматически.",
    ),
    "password": (
        "Облачный пароль",
        "У аккаунта включена двухэтапная проверка. Введите пароль Telegram ниже.",
    ),
    "login_error": (
        "Не удалось войти",
        "Проверьте api_id/api_hash и доступность Telegram из контейнера. "
        "Попробую ещё раз автоматически.",
    ),
    "logged_in": ("Готово", "Сессия сохранена, запускаю юзербота…"),
    "running": ("Юзербот работает", "Всё хорошо: сессия найдена, процесс запущен."),
    "idle": ("Ожидание", "Проверяю состояние…"),
}

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Heroku Userbot — Amvera</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; padding: 24px;
    background: #12161b; color: #e8ecf1;
    font: 16px/1.5 -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
  }
  .card {
    width: 100%; max-width: 460px; background: #1b2129; border-radius: 18px;
    padding: 28px; box-shadow: 0 18px 45px rgba(0,0,0,.45);
    border: 1px solid #262e38;
  }
  h1 { margin: 0 0 6px; font-size: 21px; }
  .hint { color: #97a3b3; font-size: 14px; margin: 0 0 20px; }
  .qr { background: #fff; border-radius: 14px; padding: 12px; display: flex; justify-content: center; }
  .qr img { display: block; width: 100%; max-width: 340px; height: auto; }
  .row { display: flex; gap: 10px; margin-top: 14px; }
  input[type=password] {
    flex: 1; padding: 12px 14px; border-radius: 10px; border: 1px solid #333d4a;
    background: #141a21; color: inherit; font-size: 15px;
  }
  button {
    padding: 12px 18px; border: 0; border-radius: 10px; cursor: pointer;
    background: #c9a36a; color: #171a1f; font-weight: 600; font-size: 15px;
  }
  button.ghost { background: transparent; color: #97a3b3; border: 1px solid #333d4a; }
  .status { display: flex; gap: 10px; align-items: flex-start; margin-bottom: 18px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; margin-top: 8px; flex: 0 0 auto; background: #4a5563; }
  .dot.ok { background: #4caf7d; }
  .dot.warn { background: #d6a04a; }
  .dot.err { background: #d1595a; }
  .meta { margin-top: 18px; color: #97a3b3; font-size: 13px; }
  code { background: #141a21; padding: 2px 6px; border-radius: 6px; }
  .hidden { display: none; }
</style>
</head>
<body>
<div class="card">
  <div class="status">
    <div class="dot" id="dot"></div>
    <div>
      <h1 id="title">Загрузка…</h1>
      <p class="hint" id="hint">Секунду…</p>
    </div>
  </div>

  <div id="qrbox" class="qr hidden">
    <img id="qrimg" alt="QR-код для входа в Telegram">
  </div>

  <form id="pwform" class="row hidden" autocomplete="off">
    <input type="password" id="pw" placeholder="Пароль Telegram (2FA)" required>
    <button type="submit">Войти</button>
  </form>

  <div class="meta" id="meta"></div>

  <div class="row" id="resetbox" style="display:none">
    <button class="ghost" id="reset">Сбросить сессии и войти заново</button>
  </div>
</div>
<script>
const KEY = new URLSearchParams(location.search).get("key") || "";
const $ = (id) => document.getElementById(id);
let timer = null;

function setPhase(phase, title, hint, hasQr) {
  $("title").textContent = title;
  $("hint").textContent = hint;
  const dot = $("dot");
  dot.className = "dot " + (
    phase === "running" || phase === "logged_in" ? "ok" :
    phase === "login_error" || phase === "no_creds" ? "err" : "warn");
  $("qrbox").classList.toggle("hidden", !hasQr);
  $("pwform").classList.toggle("hidden", phase !== "password");
  $("resetbox").style.display = phase === "running" || phase === "login_error" ? "flex" : "none";
}

async function poll() {
  try {
    const r = await fetch("api/state?key=" + encodeURIComponent(KEY), {cache: "no-store"});
    const s = await r.json();
    setPhase(s.phase, s.title, s.hint, s.phase === "login");
    if (s.phase === "login") {
      $("qrimg").src = "qr.svg?key=" + encodeURIComponent(KEY) + "&t=" + Date.now();
    }
    const bits = [];
    if (s.account) bits.push("Аккаунт: <code>" + s.account + "</code>");
    if (s.phase === "running") bits.push("Аптайм: " + s.uptime + " с");
    if (s.restarts) bits.push("Перезапусков: " + s.restarts);
    if (s.last_exit) bits.push("Последний выход: " + s.last_exit);
    $("meta").innerHTML = bits.join("<br>");
  } catch (e) {
    setPhase("error", "Нет связи", "Приложение перезапускается, подождите…", false);
  }
}

$("pwform").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const value = $("pw").value;
  $("pw").value = "";
  await fetch("api/password?key=" + encodeURIComponent(KEY), {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({password: value})
  });
  setTimeout(poll, 800);
});

$("reset").addEventListener("click", async () => {
  if (!confirm("Удалить сохранённые сессии? Придётся войти заново.")) return;
  await fetch("api/reset?key=" + encodeURIComponent(KEY), {method: "POST"});
  setTimeout(poll, 1500);
});

poll();
timer = setInterval(poll, 2500);
</script>
</body>
</html>
"""

FORBIDDEN = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Heroku Userbot — нужен ключ</title>
<style>body{background:#12161b;color:#e8ecf1;font:16px/1.6 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:24px}
.card{max-width:520px;background:#1b2129;border:1px solid #262e38;border-radius:18px;padding:26px}
code{background:#141a21;padding:2px 6px;border-radius:6px}a{color:#c9a36a}</style></head>
<body><div class="card"><h1>Нужен ключ доступа</h1>
<p>Страница первого входа закрыта ключом, чтобы чужой человек не мог привязать
свой Telegram-аккаунт к вашему юзерботу.</p>
<p>Ключ напечатан в логах приложения при старте (вкладка «Логи» в Amvera) —
откройте адрес вида <code>https://ваш-домен/?key=КЛЮЧ</code>.</p>
<p>Свой ключ можно задать переменной <code>AMVERA_LOGIN_TOKEN</code>.</p>
</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "HerokuAmvera/1.0"

    def log_message(self, fmt: str, *args) -> None:  # тише в логах
        return

    # -- helpers ---------------------------------------------------------- #

    def _key_ok(self, query: dict) -> bool:
        supplied = (query.get("key") or [""])[0]
        return secrets.compare_digest(
            supplied.encode("utf-8", "replace"),
            LOGIN_KEY.encode("utf-8", "replace"),
        )

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json; charset=utf-8")

    # -- routes ----------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if path == "/health":
            self._json({"ok": True})
            return

        if not self._key_ok(query):
            # 200, а не 403: часть платформ считает ответ 4xx на "/" признаком
            # нездорового приложения и перезапускает контейнер.
            self._send(200, FORBIDDEN.encode(), "text/html; charset=utf-8")
            return

        if path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return

        if path == "/qr.svg":
            with STATE_LOCK:
                svg = STATE["qr_svg"]
            if not svg:
                self._json({"error": "no qr"}, 404)
                return
            self._send(200, svg.encode(), "image/svg+xml; charset=utf-8")
            return

        if path == "/api/state":
            state = snapshot()
            title, hint = PHASE_TEXT.get(state["phase"], ("Работаю", ""))
            if state["phase"] == "running" and state["restarts"]:
                hint = (
                    "Процесс перезапускается — смотрите логи приложения. "
                    "Если сессия слетела, нажмите кнопку ниже и войдите заново."
                )
            state.update({"title": title, "hint": hint or state["detail"]})
            self._json(state)
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if not self._key_ok(query):
            self._json({"error": "forbidden"}, 403)
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}

        if path == "/api/password":
            password = str(payload.get("password") or "")
            with STATE_LOCK:
                event = STATE["pw_event"]
                loop = STATE["loop"]
                STATE["pw_value"] = password
            if event is not None and loop is not None:
                loop.call_soon_threadsafe(event.set)
                self._json({"ok": True})
            else:
                self._json({"error": "password not required"}, 400)
            return

        if path == "/api/reset":
            reset_requested.set()
            self._json({"ok": True})
            return

        self._json({"error": "not found"}, 404)


reset_requested = threading.Event()


def http_worker() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log(f"HTTP-сервер слушает 0.0.0.0:{PORT}")
    server.serve_forever(poll_interval=0.5)


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #


def reset_sessions() -> None:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    target = DATA_DIR / f"sessions-removed-{timestamp}"
    ensure_dirs()
    moved = 0
    for path in list(SESSIONS_DIR.glob("heroku-*")):
        target.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(path), str(target / path.name))
            moved += 1
        except OSError:
            log(f"не удалось переместить {path}")
    set_state("idle", f"Сессии сброшены ({moved}), можно входить заново", qr_svg="", account="")


def main() -> int:
    ensure_dirs()

    log("=" * 64)
    log("Heroku Userbot on Amvera")
    log(f"данные: {DATA_DIR}")
    log(f"КЛЮЧ ДОСТУПА К СТРАНИЦЕ ВХОДА: {LOGIN_KEY}")
    log(f"откройте: https://<домен-приложения>/?key={LOGIN_KEY}")
    log("=" * 64)

    if not get_creds():
        log("api_id/api_hash не заданы — добавьте их в переменные проекта")

    threading.Thread(target=http_worker, name="http", daemon=True).start()
    threading.Thread(target=login_worker, name="login", daemon=True).start()

    def handle_signal(signum, _frame):
        log(f"получен сигнал {signum}, останавливаюсь")
        STOP.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):
            pass

    bot_thread = threading.Thread(target=supervise, name="bot", daemon=True)
    bot_thread.start()

    while not STOP.is_set():
        if reset_requested.is_set():
            reset_requested.clear()
            reset_sessions()
        STOP.wait(0.5)

    bot_thread.join(timeout=25)
    return 0


if __name__ == "__main__":
    sys.exit(main())
