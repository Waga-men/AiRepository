"""Форматирование чисел, дат и подписей."""
from __future__ import annotations

import html
from datetime import datetime, timezone, timedelta

MOSCOW_TZ = timezone(timedelta(hours=3))

STATUS_LABELS = {
    "PENDING_APPROVAL": "На проверке",
    "PENDING_MODERATION": "Проверка изменений",
    "APPROVED": "Активен",
    "DECLINED": "Отклонён",
    "BLOCKED": "Заблокирован",
    "EXPIRED": "Истёк",
    "SOLD": "Продан",
    "DRAFT": "Черновик",
    "PAID": "Оплачен",
    "PENDING": "Ожидает отправки",
    "SENT": "Отправлен",
    "CONFIRMED": "Завершён",
    "CONFIRMED_AUTOMATICALLY": "Завершён автоматически",
    "ROLLED_BACK": "Возврат",
}


def esc(value) -> str:
    """Экранирование для HTML parse_mode."""
    return html.escape(str(value if value is not None else ""))


def money(value) -> str:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        v = 0
    return f"{int(round(v)):,}".replace(",", " ") + " ₽"


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status or "—")


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MOSCOW_TZ)
    except Exception:
        return None


def dt_str(value: str, with_time: bool = False) -> str:
    dt = _parse_dt(value)
    if not dt:
        return "—"
    fmt = "%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y"
    return dt.strftime(fmt)


def time_str(value: str) -> str:
    dt = _parse_dt(value)
    return dt.strftime("%H:%M") if dt else "—"


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
