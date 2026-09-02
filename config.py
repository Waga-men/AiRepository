"""Конфигурация бота."""
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# ID владельца бота (постоянный статус владельца).
OWNER_ID = int(os.getenv("OWNER_ID", "5848676904"))

# (устарело, но оставлено) дополнительный белый список Telegram-айди через запятую.
_ALLOWED = os.getenv("ALLOWED_USERS", "").strip()
ALLOWED_USERS = {int(x) for x in _ALLOWED.split(",") if x.strip().isdigit()} if _ALLOWED else None

# Москва, UTC+3.
LOCAL_TZ_OFFSET_MIN = -180

# Прокси для обхода блокировки api.telegram.org (например "http://127.0.0.1:7890").
# Пусто = без прокси.
PROXY = os.getenv("PROXY", "").strip()

# Прокси для запросов к playerok.com. Нужен, если IP сервера блокируется
# защитой Playerok (DDoS-Guard). Если пусто — используется PROXY (если задан).
PLAYEROK_PROXY = os.getenv("PLAYEROK_PROXY", "").strip() or PROXY

# Адрес локального Bot API сервера (обход блокировки без VPN).
# Например "http://localhost:8081". Пусто = стандартный api.telegram.org.
BOT_API_URL = os.getenv("BOT_API_URL", "").strip()
