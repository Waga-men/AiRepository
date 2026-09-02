"""Хранилище настроек и состояния каждого пользователя (JSON-файлы).

Модель повторяет chrome.storage расширения: settings + runtime.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path(__file__).parent / "data"

_locks: dict[int, asyncio.Lock] = {}


def _lock(user_id: int) -> asyncio.Lock:
    if user_id not in _locks:
        _locks[user_id] = asyncio.Lock()
    return _locks[user_id]


def _path(user_id: int) -> Path:
    return DATA_DIR / f"{user_id}.json"


DEFAULT_SETTINGS: dict = {
    "emoji_theme": "green",
    "templates": [
        {"id": "hello", "title": "Приветствие",
         "text": "Здравствуйте! Спасибо за обращение. Чем могу помочь?"},
        {"id": "thanks", "title": "Спасибо",
         "text": "Спасибо за покупку! Если появятся вопросы — напишите, обязательно помогу."},
    ],
    "automation": {
        "enabled": False,
        "accepted": False,
        "poll_seconds": 15,
        "delay_seconds": 3,
        "hourly_limit": 15,
        "notifications": True,
        "notification_preview": True,
        "auto_read": False,
        "keyword_enabled": False,
        "rules": [],
    },
    "bump": {"enabled": False, "accepted": False},
    "relist": {
        "enabled": False,
        "accepted": False,
        "paid_enabled": False,
        "max_price_per_item": 100,
        "min_balance": 0,
        "delay_minutes": 5,
    },
    "return_message": {
        "enabled": False,
        "accepted": False,
        "text": ("Здравствуйте, {{username}}! Увидели возврат по сделке с «{{item}}». "
                 "Если что-то пошло не так — напишите, обязательно разберёмся и решим вопрос."),
    },
    "deal_confirm": {
        "enabled": False,
        "accepted": False,
        "items": [],   # список id товаров; пусто = все товары
        "message": "Товар отправлен. Спасибо за покупку!",
    },
    "deal_complete": {
        "enabled": False,
        "accepted": False,
        "exclude_items": [],   # id товаров-исключений
        "message": "Спасибо за покупку! Будем рады вашему отзыву.",
    },
    "reviews_reply": {
        "enabled": False,
        "accepted": False,
        "messages": {"1": "", "2": "", "3": "", "4": "", "5": ""},
    },
}


def _default_runtime() -> dict:
    return {
        "processed_message_ids": [],
        "sent_timestamps": [],
        "automation_log": [],
        "baseline_done": False,
        "relist_state": {},
        "bump_state": {},
        "last_relist_at": 0,
        "last_bump_at": 0,
        "rollback_baseline_done": False,
        "processed_rollback_ids": [],
        # планировщик (epoch ms)
        "next_automation_at": 0,
        "next_bump_at": 0,
        "next_relist_at": 0,
        "next_rollback_at": 0,
        # сделки и отзывы
        "next_deal_at": 0,
        "next_reviews_at": 0,
        "processed_deal_ids": [],
        "deal_baseline_done": False,
        "processed_review_ids": [],
        "review_baseline_done": False,
    }


def _new_record(user_id: int, username: str = "") -> dict:
    return {
        "user_id": user_id,
        "username": username,
        "playerok_id": None,
        "name": None,
        "cookies": None,
        "settings": copy.deepcopy(DEFAULT_SETTINGS),
        "runtime": _default_runtime(),
        "accounts": [],   # остальные (неактивные) аккаунты Playerok
    }


def _deep_merge(base: dict, incoming: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (incoming or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_raw(user_id: int) -> dict:
    p = _path(user_id)
    rec = None
    if p.exists():
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            rec = None
    if rec is None:
        return _new_record(user_id)
    # гарантируем наличие новых ключей настроек (обратная совместимость)
    if isinstance(rec.get("settings"), dict):
        rec["settings"] = _deep_merge(DEFAULT_SETTINGS, rec["settings"])
    if not isinstance(rec.get("runtime"), dict):
        rec["runtime"] = _default_runtime()
    else:
        for k, v in _default_runtime().items():
            rec["runtime"].setdefault(k, v)
    if not isinstance(rec.get("accounts"), list):
        rec["accounts"] = []
    rec.setdefault("playerok_id", None)
    rec.setdefault("name", None)
    return rec


def _save_raw(user_id: int, record: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(user_id).with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _path(user_id))


async def load(user_id: int) -> dict:
    async with _lock(user_id):
        return await asyncio.to_thread(_load_raw, user_id)


async def save(user_id: int, record: dict) -> None:
    async with _lock(user_id):
        await asyncio.to_thread(_save_raw, user_id, record)


async def update(user_id: int, mutate) -> dict:
    """Атомарно: читает запись, применяет mutate(record), сохраняет, возвращает запись.

    mutate может быть как синхронным, так и асинхронным (async def).
    """
    async with _lock(user_id):
        rec = await asyncio.to_thread(_load_raw, user_id)
        result = mutate(rec)
        if inspect.isawaitable(result):
            await result
        await asyncio.to_thread(_save_raw, user_id, rec)
        return rec


async def get_settings(user_id: int) -> dict:
    rec = await load(user_id)
    return rec["settings"]


async def get_runtime(user_id: int) -> dict:
    rec = await load(user_id)
    return rec["runtime"]


async def set_settings(user_id: int, settings: dict) -> None:
    await update(user_id, lambda rec: rec.update(settings=settings))


async def set_cookies(user_id: int, cookie_header: Optional[str]) -> None:
    await update(user_id, lambda rec: rec.update(cookies=cookie_header))


async def get_cookies(user_id: int) -> Optional[str]:
    rec = await load(user_id)
    return rec.get("cookies")


# ================== несколько аккаунтов Playerok ==================

MAX_ACCOUNTS = 3


def _accounts_list(rec: dict) -> list:
    """Полный список аккаунтов: индекс 0 — активный, далее — неактивные."""
    lst = []
    if rec.get("playerok_id") or rec.get("cookies"):
        lst.append({
            "playerok_id": rec.get("playerok_id"),
            "name": rec.get("name"),
            "cookies": rec.get("cookies"),
            "settings": rec.get("settings"),
            "runtime": rec.get("runtime"),
        })
    lst.extend(list(rec.get("accounts") or []))
    return lst


async def list_accounts(user_id: int) -> list:
    rec = await load(user_id)
    return _accounts_list(rec)


async def add_account(user_id: int, playerok_id: str, cookies: str, name: str) -> int:
    """Добавляет аккаунт Playerok и делает его активным.

    Возвращает новое количество аккаунтов, либо:
      -1 — достигнут лимит MAX_ACCOUNTS;
      -2 — такой аккаунт уже добавлен.
    """
    async with _lock(user_id):
        rec = await asyncio.to_thread(_load_raw, user_id)
        full = _accounts_list(rec)
        for a in full:
            if a.get("playerok_id") == playerok_id:
                return -2
        if len(full) >= MAX_ACCOUNTS:
            return -1
        new_acc = {
            "playerok_id": playerok_id,
            "name": name,
            "cookies": cookies,
            "settings": copy.deepcopy(DEFAULT_SETTINGS),
            "runtime": _default_runtime(),
        }
        if full:
            # текущий активный уходит в список неактивных
            rec.setdefault("accounts", []).append(full[0])
        rec["playerok_id"] = playerok_id
        rec["name"] = name
        rec["cookies"] = cookies
        rec["settings"] = new_acc["settings"]
        rec["runtime"] = new_acc["runtime"]
        await asyncio.to_thread(_save_raw, user_id, rec)
        return len(full) + 1


async def switch_account(user_id: int, index: int) -> bool:
    """Делает аккаунт с индексом `index` (в полном списке) активным."""
    async with _lock(user_id):
        rec = await asyncio.to_thread(_load_raw, user_id)
        full = _accounts_list(rec)
        if index <= 0 or index >= len(full):
            return False
        target = full[index]
        current = full[0]
        others = full[1:]
        others[index - 1] = current
        rec["playerok_id"] = target["playerok_id"]
        rec["name"] = target["name"]
        rec["cookies"] = target["cookies"]
        rec["settings"] = target["settings"]
        rec["runtime"] = target["runtime"]
        rec["accounts"] = others
        await asyncio.to_thread(_save_raw, user_id, rec)
        return True


async def delete_account(user_id: int, index: int) -> Optional[str]:
    """Удаляет аккаунт с индексом `index`. Возвращает playerok_id удалённого (или None)."""
    async with _lock(user_id):
        rec = await asyncio.to_thread(_load_raw, user_id)
        full = _accounts_list(rec)
        if index < 0 or index >= len(full):
            return None
        removed = full[index].get("playerok_id")
        if index == 0:
            others = list(rec.get("accounts") or [])
            if others:
                nxt = others.pop(0)
                rec["playerok_id"] = nxt["playerok_id"]
                rec["name"] = nxt["name"]
                rec["cookies"] = nxt["cookies"]
                rec["settings"] = nxt["settings"]
                rec["runtime"] = nxt["runtime"]
                rec["accounts"] = others
            else:
                rec["playerok_id"] = None
                rec["name"] = None
                rec["cookies"] = None
                rec["accounts"] = []
                rec["settings"] = copy.deepcopy(DEFAULT_SETTINGS)
                rec["runtime"] = _default_runtime()
        else:
            others = list(rec.get("accounts") or [])
            others.pop(index - 1)
            rec["accounts"] = others
        await asyncio.to_thread(_save_raw, user_id, rec)
        return removed


async def transfer_settings(user_id: int, from_index: int, to_index: int) -> bool:
    """Копирует настройки с аккаунта from_index на аккаунт to_index."""
    async with _lock(user_id):
        rec = await asyncio.to_thread(_load_raw, user_id)
        full = _accounts_list(rec)
        if from_index == to_index:
            return False
        if from_index < 0 or to_index < 0 or from_index >= len(full) or to_index >= len(full):
            return False
        src_settings = copy.deepcopy(full[from_index].get("settings") or {})
        if to_index == 0:
            rec["settings"] = src_settings
        else:
            others = list(rec.get("accounts") or [])
            others[to_index - 1]["settings"] = src_settings
            rec["accounts"] = others
        await asyncio.to_thread(_save_raw, user_id, rec)
        return True


async def log_automation(user_id: int, message: str, kind: str = "info") -> None:
    def mutate(rec):
        rec["runtime"].setdefault("automation_log", []).append(
            {"at": int(__import__("time").time() * 1000), "message": message, "kind": kind}
        )
        # оставляем последние 300 записей
        rec["runtime"]["automation_log"] = rec["runtime"]["automation_log"][-300:]
    await update(user_id, mutate)


async def all_user_ids() -> list[int]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ids = []
    for p in DATA_DIR.glob("*.json"):
        try:
            ids.append(int(p.stem))
        except ValueError:
            continue
    return ids


# ================== админка: владелец, доступы, оплата ==================

ADMIN_FILE = DATA_DIR / "admin.json"
_admin_lock = asyncio.Lock()

# Цены по умолчанию: длительности подписки (дни + цена в звёздах).
DEFAULT_PRICES = {
    "durations": [
        {"key": "1d", "label": "1 день", "days": 1, "stars": 30},
        {"key": "7d", "label": "Неделя", "days": 7, "stars": 150},
        {"key": "30d", "label": "Месяц", "days": 30, "stars": 500},
        {"key": "180d", "label": "6 месяцев", "days": 180, "stars": 2500},
    ],
}


def _default_admin() -> dict:
    import config
    return {
        "owner_id": config.OWNER_ID,
        "known": {},   # user_id -> username (кто когда-либо писал боту)
        "users": {
            str(config.OWNER_ID): {
                "status": "owner",
                "added_at": int(time.time()),
                "until": None,
                "granted_by": None,
            }
        },
        "prices": copy.deepcopy(DEFAULT_PRICES),
        "promos": {},   # code -> {"discount": int (процент)}
        "payments": [],
        "playerok": {},   # playerok_id -> telegram user_id (кто какой аккаунт привязал)
    }


def _load_admin_raw() -> dict:
    import config
    ADMIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if ADMIN_FILE.exists():
        try:
            data = json.loads(ADMIN_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = _default_admin()
    else:
        data = _default_admin()
    # гарантируем присутствие владельца и свежих цен
    data.setdefault("owner_id", config.OWNER_ID)
    data.setdefault("known", {})
    data.setdefault("users", {})
    data.setdefault("payments", [])
    data.setdefault("prices", copy.deepcopy(DEFAULT_PRICES))
    data.setdefault("promos", {})
    data.setdefault("playerok", {})
    data["users"].setdefault(str(config.OWNER_ID), {
        "status": "owner", "added_at": int(time.time()), "until": None, "granted_by": None,
    })
    data["users"][str(config.OWNER_ID)]["status"] = "owner"
    # миграция старых цен (stars/card_rub) на durations
    if "durations" not in data["prices"]:
        data["prices"]["durations"] = copy.deepcopy(DEFAULT_PRICES["durations"])
    return data


def _save_admin_raw(data: dict) -> None:
    ADMIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ADMIN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, ADMIN_FILE)


async def load_admin() -> dict:
    async with _admin_lock:
        return await asyncio.to_thread(_load_admin_raw)


async def update_admin(mutate) -> dict:
    async with _admin_lock:
        data = await asyncio.to_thread(_load_admin_raw)
        result = mutate(data)
        if inspect.isawaitable(result):
            await result
        await asyncio.to_thread(_save_admin_raw, data)
        return data


async def is_owner(user_id: int) -> bool:
    data = await load_admin()
    return int(data.get("owner_id", 0)) == int(user_id)


async def is_allowed(user_id: int) -> bool:
    data = await load_admin()
    if int(data.get("owner_id", 0)) == int(user_id):
        return True
    u = data["users"].get(str(user_id))
    if not u or u.get("status") != "active":
        return False
    until = u.get("until")
    if until and int(until) < int(time.time()):
        return False
    return True


async def register_user(user_id: int, username: str = "") -> None:
    def mutate(data):
        data["known"][str(user_id)] = username or data["known"].get(str(user_id), "")
        data["users"].setdefault(str(user_id), {"status": "none", "added_at": int(time.time()), "until": None, "granted_by": None})
    await update_admin(mutate)


async def resolve_user(ref: str) -> tuple[int | None, str | None]:
    """Возвращает (user_id, username) по '@username' (латиница) или числовому ID,
    либо (None, ошибка). Неверный формат или ненайденный пользователь -> «Неправильные данные».
    """
    ref = (ref or "").strip()
    data = await load_admin()
    if ref.startswith("@"):
        uname = ref[1:].strip()
        if not uname or not re.fullmatch(r"[A-Za-z0-9_]+", uname):
            return None, "Неправильные данные. Отправьте @username или ID"
        for uid, name in data["known"].items():
            if (name or "").lower() == uname.lower():
                return int(uid), name
        return None, "Неправильные данные. Отправьте @username или ID"
    if ref.isdigit():
        uid = int(ref)
        return uid, data["known"].get(str(uid), "")
    return None, "Неправильные данные. Отправьте @username или ID"


def parse_duration(text: str):
    """Разбирает срок доступа.

    Возвращает (seconds, ok):
      seconds=None — «навсегда»; ok=False — строка не распознана.
    Примеры: '', 'perm', 'навсегда' -> навсегда; '7d', '12h', '30m', '60s'.
    """
    text = (text or "").strip().lower()
    if text in ("", "perm", "permanent", "forever", "навсегда"):
        return None, True
    m = re.match(r"^(\d+)\s*(d|h|m|s)$", text)
    if not m:
        return None, False
    n = int(m.group(1))
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return n * mult, True


def duration_human(seconds) -> str:
    """Человекочитаемое описание срока (seconds=None -> «навсегда»)."""
    if seconds is None:
        return "навсегда"
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}д")
    if h:
        parts.append(f"{h}ч")
    if m:
        parts.append(f"{m}м")
    if s or not parts:
        parts.append(f"{s}с")
    return " ".join(parts)


async def grant_access(user_id: int, until: int | None, granted_by: int) -> dict:
    def mutate(data):
        data["users"][str(user_id)] = {
            "status": "active",
            "added_at": int(time.time()),
            "until": until,
            "granted_by": granted_by,
        }
        return data["users"][str(user_id)]
    return await update_admin(mutate)


async def revoke_access(user_id: int) -> None:
    def mutate(data):
        if str(user_id) in data["users"]:
            data["users"][str(user_id)]["status"] = "revoked"
    await update_admin(mutate)


async def set_duration_price(key: str, stars: int) -> None:
    """Устанавливает цену (в звёздах) для длительности подписки по её ключу."""
    def mutate(data):
        for d in data["prices"]["durations"]:
            if d["key"] == key:
                d["stars"] = int(stars)
                break
    await update_admin(mutate)


async def get_duration(key: str) -> dict | None:
    data = await load_admin()
    for d in data["prices"]["durations"]:
        if d["key"] == key:
            return d
    return None


# ---- промокоды ----

async def add_promo(code: str, discount: int, max_uses: int = 0, durations: str = "all") -> None:
    """max_uses: 0 = без ограничения; durations: 'all'|'1d'|'7d'|'30d'|'180d'."""
    def mutate(data):
        data.setdefault("promos", {})
        data["promos"][code.strip().upper()] = {
            "discount": int(discount),
            "max_uses": int(max_uses),
            "used": 0,
            "durations": durations,
        }
    await update_admin(mutate)


async def delete_promo(code: str) -> None:
    def mutate(data):
        data.setdefault("promos", {})
        data["promos"].pop(code.strip().upper(), None)
    await update_admin(mutate)


async def get_promo(code: str) -> dict | None:
    """Возвращает промокод {'discount': N, 'max_uses': M, 'used': U} или None."""
    data = await load_admin()
    return data.get("promos", {}).get((code or "").strip().upper())


async def consume_promo(code: str) -> None:
    """Увеличивает счётчик использований промокода (при успешной оплате)."""
    def mutate(data):
        data.setdefault("promos", {})
        p = data["promos"].get((code or "").strip().upper())
        if p:
            p["used"] = int(p.get("used", 0)) + 1
    await update_admin(mutate)


async def get_playerok_owner(playerok_id: str) -> int | None:
    """Кто из Telegram-пользователей уже привязал этот аккаунт Playerok."""
    data = await load_admin()
    return data.get("playerok", {}).get(str(playerok_id))


async def bind_playerok(user_id: int, playerok_id: str) -> None:
    def mutate(data):
        data.setdefault("playerok", {})
        data["playerok"][str(playerok_id)] = int(user_id)
    await update_admin(mutate)


async def unbind_playerok(playerok_id: str) -> None:
    def mutate(data):
        data.get("playerok", {}).pop(str(playerok_id), None)
    await update_admin(mutate)


async def unbind_user_playerok(user_id: int) -> None:
    """Снимает привязку Playerok-аккаунта пользователя (при /logout)."""
    rec = await load(user_id)
    pid = rec.get("playerok_id")
    if pid:
        await unbind_playerok(pid)
        await update(user_id, lambda r: r.pop("playerok_id", None))


async def log_payment(user_id: int, kind: str, amount, status: str) -> None:
    def mutate(data):
        data["payments"].append({
            "user_id": int(user_id),
            "kind": kind,
            "amount": amount,
            "status": status,
            "ts": int(time.time()),
        })
        data["payments"] = data["payments"][-200:]
    await update_admin(mutate)
