"""Запуск бота с локальным Bot API сервером (обход блокировки в РФ).

Всё в одном: спрашивает BOT_TOKEN / API_ID / API_HASH (если их нет в .env),
находит или скачивает локальный сервер Telegram Bot API, запускает его,
затем запускает самого бота.

Запуск:  python run.py   (или двойной клик по start.bat)
"""
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE, ".env")

TG_API_DIR = os.path.join(BASE, "tg-api")
TG_EXE = os.path.join(TG_API_DIR, "telegram-bot-api.exe")
TG_DATA_DIR = os.path.join(BASE, "tg-data")

LOCAL_ZIP = os.path.join(BASE, "telegram-bot-api-win64.zip")
REMOTE_7Z_URL = (
    "https://github.com/Bezdarnost01/telegram-bot-api-windows/"
    "releases/download/windows-build/build.7z"
)


def load_env() -> dict:
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def save_env(env: dict) -> None:
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        for k, v in env.items():
            f.write(f"{k}={v}\n")


def ask(label: str, current: str = "") -> str:
    if current:
        ans = input(f"{label} [{current}]: ").strip()
        return ans or current
    return input(f"{label}: ").strip()


def ensure_env() -> dict:
    env = load_env()
    changed = False

    if not env.get("BOT_TOKEN"):
        env["BOT_TOKEN"] = ask("BOT_TOKEN (получите у @BotFather)")
        changed = True
    if not env.get("API_ID"):
        env["API_ID"] = ask("API_ID (с https://my.telegram.org -> API development tools)")
        changed = True
    if not env.get("API_HASH"):
        env["API_HASH"] = ask("API_HASH (там же)")
        changed = True

    env.setdefault("OWNER_ID", "5848676904")
    env["BOT_API_URL"] = "http://localhost:8081"

    if changed:
        save_env(env)
        print(f"Настройки сохранены в {ENV_PATH}\n")
    return env


def _extract_zip(zip_path: str, dest: str) -> None:
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)


def ensure_server() -> None:
    if os.path.exists(TG_EXE):
        return

    os.makedirs(TG_API_DIR, exist_ok=True)

    # 1) Готовый zip рядом со скриптом
    if os.path.exists(LOCAL_ZIP):
        print("Распаковываю локальный сервер из telegram-bot-api-win64.zip ...")
        _extract_zip(LOCAL_ZIP, TG_API_DIR)
    else:
        # 2) Скачиваем сборку с GitHub
        print("Скачиваю локальный сервер Telegram Bot API (около 7 МБ)...")
        archive = os.path.join(BASE, "tg-api-build.7z")
        try:
            urllib.request.urlretrieve(REMOTE_7Z_URL, archive)
        except Exception as e:
            print(f"Не удалось скачать сервер: {e}")
            print(f"Скачайте вручную telegram-bot-api-win64.zip и положите в папку {BASE}")
            sys.exit(1)
        print("Распаковываю...")
        try:
            subprocess.run(["tar", "-xf", archive, "-C", TG_API_DIR], check=True)
        except Exception:
            try:
                import py7zr
                with py7zr.SevenZipFile(archive) as z:
                    z.extractall(TG_API_DIR)
            except Exception:
                print("Не удалось распаковать архив. Распакуйте build.7z вручную в папку tg-api")
                sys.exit(1)
        # если внутри была подпапка build — поднимем файлы наверх
        build_dir = os.path.join(TG_API_DIR, "build")
        if os.path.isdir(build_dir):
            for fn in os.listdir(build_dir):
                shutil.move(os.path.join(build_dir, fn), os.path.join(TG_API_DIR, fn))
            os.rmdir(build_dir)

    if not os.path.exists(TG_EXE):
        print(f"Сервер не найден: {TG_EXE}")
        sys.exit(1)
    print("Локальный сервер готов.")


def start_server(api_id: str, api_hash: str):
    os.makedirs(TG_DATA_DIR, exist_ok=True)
    cmd = [
        TG_EXE,
        f"--api-id={api_id}",
        f"--api-hash={api_hash}",
        "--local",
        f"--dir={TG_DATA_DIR}",
        "--http-port=8081",
    ]
    print("Запускаю локальный сервер Telegram Bot API...")
    proc = subprocess.Popen(cmd, cwd=BASE)

    # ждём, пока сервер поднимется на порту 8081
    for _ in range(120):
        time.sleep(0.5)
        if proc.poll() is not None:
            print("Сервер завершился с ошибкой (см. сообщения выше).")
            sys.exit(1)
        try:
            s = socket.create_connection(("127.0.0.1", 8081), timeout=1)
            s.close()
            print("Сервер запущен на http://localhost:8081\n")
            return proc
        except OSError:
            continue
    print("Сервер не запустился за 60 секунд.")
    sys.exit(1)


def main() -> None:
    print("=" * 55)
    print(" Playerok Tools — запуск с локальным Bot API")
    print("=" * 55)
    env = ensure_env()
    ensure_server()
    proc = start_server(env["API_ID"], env["API_HASH"])

    # значения уже в .env — config.py их подхватит
    try:
        import asyncio
        import bot as bot_module
        try:
            asyncio.run(bot_module.main())
        except KeyboardInterrupt:
            print("\nОстановлено.")
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
