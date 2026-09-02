"""Фоновая автоматизация (порт авто-циклов расширения).

Циклы:
- чат-автоматизация (команды по ключевым словам) — automationTick
- авто-поднятие лотов — runBumpCycle
- авто-перевыставление лотов — runRelistCycle
- сообщение при возврате — rollbackTick
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

from client import PlayerokClient, PlayerokError, AuthError
import storage
from fmt import money, now_ms

MOSCOW_TZ = timezone(timedelta(hours=3))

REBUMP_COOLDOWN_MS = 12 * 3600000  # один лот не трогаем чаще раза в 12 часов
ITEM_BACKOFF_MS = 1800000          # после ошибки — пауза 30 минут на этот лот
ROLLBACK_INTERVAL_MS = 300000      # возвраты проверяем раз в 5 минут
BUMP_INTERVAL_MS = 300000          # поднятие — раз в 5 минут
DEAL_INTERVAL_MS = 60000           # сделки проверяем раз в минуту
REVIEWS_INTERVAL_MS = 300000       # отзывы — раз в 5 минут

# callable(user_id, text) — отправка сообщения владельцу бота в Telegram.
_notifier = None
_run_locks: dict[int, asyncio.Lock] = {}


def set_notifier(fn) -> None:
    global _notifier
    _notifier = fn


async def _notify(user_id: int, text: str) -> None:
    if _notifier:
        try:
            await _notifier(user_id, text)
        except Exception:
            pass


def _lock(user_id: int) -> asyncio.Lock:
    if user_id not in _run_locks:
        _run_locks[user_id] = asyncio.Lock()
    return _run_locks[user_id]


def locked(fn):
    """Серийная обёртка: цикл одного пользователя не пересекается сам с собой."""
    async def wrapper(user_id, *args, **kwargs):
        async with _lock(user_id):
            return await fn(user_id, *args, **kwargs)
    return wrapper


def free_status(statuses: list) -> dict | None:
    for s in statuses:
        if s["price"] == 0 and (s["type"] == "DEFAULT" or not s["type"]):
            return s
    for s in statuses:
        if s["price"] == 0:
            return s
    return None


def prune_state(state: dict) -> dict:
    if len(state) > 600:
        keys = list(state.keys())
        for k in keys[: len(keys) - 600]:
            state.pop(k, None)
    return state


def other_participant(chat: dict, viewer: dict) -> dict | None:
    vid = viewer.get("id")
    parts = chat.get("participants") or []
    for u in parts:
        if u.get("id") != vid:
            return u
    return parts[0] if parts else None


def match_command(text: str, command: str) -> bool:
    lower = (text or "").strip().lower()
    cmd = (command or "").strip().lower()
    if not lower or not cmd:
        return False
    if lower == cmd:
        return True
    if lower.startswith(cmd):
        nxt = lower[len(cmd)]
        return nxt in " \t.,!?;:«”\"'"
    return False


def latest_inbound(messages: list, viewer_id: str) -> dict | None:
    cands = [
        m for m in messages
        if m.get("id") and m.get("user") and m["user"].get("id") != viewer_id and not m.get("deleted_at")
    ]
    if not cands:
        return None
    cands.sort(key=lambda m: m.get("created_at") or "", reverse=True)
    return cands[0]


def render_auto_text(text: str, chat: dict, viewer: dict) -> str:
    user = other_participant(chat, viewer)
    name = user.get("username") if user and user.get("username") else "покупатель"
    t = datetime.now(MOSCOW_TZ).strftime("%H:%M")
    return (text or "").replace("{{username}}", name).replace("{{time}}", t)


def render_return_text(text: str, deal: dict) -> str:
    uname = (deal.get("user") or {}).get("username") or "покупатель"
    iname = (deal.get("item") or {}).get("name") or "товар"
    t = datetime.now(MOSCOW_TZ).strftime("%H:%M")
    return (text or "").replace("{{username}}", uname).replace("{{item}}", iname).replace("{{time}}", t)


# ================= чат-автоматизация =================

async def _baseline(client: PlayerokClient, user_id: int, chats: list, viewer: dict) -> None:
    async def mutate(rec):
        rt = rec["runtime"]
        unread = [
            c for c in chats
            if c.get("unread_counter", 0) > 0
            and c.get("type") not in ("SUPPORT", "NOTIFICATIONS")
        ][:60]
        for chat in unread:
            try:
                res = await client.get_messages(chat["id"], 12)
                m = latest_inbound(res["messages"], viewer["id"])
                if m and m["id"] not in rt["processed_message_ids"]:
                    rt["processed_message_ids"].append(m["id"])
            except Exception:
                pass
        rt["baseline_done"] = True
    await storage.update(user_id, mutate)
    await storage.log_automation(user_id, f"Базовая точка создана: {len([
        c for c in chats if c.get('unread_counter', 0) > 0])} текущих чатов без автоответа.")
    await _notify(user_id, "🤖 Автоматизация готова. Новые сообщения будут обрабатываться по правилам.")


@locked
async def automation_tick(user_id: int) -> None:
    rec = await storage.load(user_id)
    if not rec.get("cookies"):
        return
    s = rec["settings"]
    rt = rec["runtime"]
    auto = s["automation"]
    if not auto.get("enabled") or not auto.get("accepted"):
        return
    if now_ms() < rt.get("next_automation_at", 0):
        return

    poll_seconds = max(10, int(auto.get("poll_seconds") or 15))
    rt["next_automation_at"] = now_ms() + poll_seconds * 1000
    await storage.update(user_id, lambda r: r["runtime"].update(next_automation_at=rt["next_automation_at"]))

    client = PlayerokClient(rec["cookies"])
    try:
        data = await client.get_chats(max_pages=5)
        viewer = data["viewer"]
        chats = data["chats"]

        if not rt.get("baseline_done"):
            await _baseline(client, user_id, chats, viewer)
            return

        candidates = [
            c for c in chats
            if c.get("unread_counter", 0) > 0 and c.get("type") not in ("SUPPORT", "NOTIFICATIONS")
        ][:20]

        handled = 0
        for chat in candidates:
            if handled >= 3:
                break
            try:
                res = await client.get_messages(chat["id"], 12)
                message = latest_inbound(res["messages"], viewer["id"])
            except Exception as exc:
                await storage.log_automation(user_id, f"Не удалось проверить чат: {exc}", "error")
                continue
            if not message or message["id"] in rt["processed_message_ids"]:
                continue
            rt["processed_message_ids"].append(message["id"])
            handled += 1

            user = other_participant(chat, viewer)
            username = user.get("username") if user and user.get("username") else "покупатель"

            if auto.get("notifications"):
                preview = (
                    f": {message['text'][:140]}" if auto.get("notification_preview") and message.get("text") else ""
                )
                await _notify(user_id, f"💬 Новое сообщение от {username}{preview}")

            reply = ""
            if auto.get("keyword_enabled") and message.get("text"):
                rule = next(
                    (r for r in auto.get("rules") or [] if r.get("enabled") is not False
                     and match_command(message["text"], r.get("keyword"))),
                    None,
                )
                if rule:
                    reply = render_auto_text(rule.get("response"), chat, viewer)

            if reply:
                hour_ago = now_ms() - 3600000
                rt["sent_timestamps"] = [t for t in rt["sent_timestamps"] if t >= hour_ago]
                limit = min(max(int(auto.get("hourly_limit") or 15), 1), 60)
                if len(rt["sent_timestamps"]) >= limit:
                    await storage.log_automation(
                        user_id, f"Лимит {limit} автоответов/час достигнут — сообщение {username} пропущено.", "error"
                    )
                else:
                    delay = min(max(int(auto.get("delay_seconds") or 3), 2), 60)
                    await asyncio.sleep(delay)
                    try:
                        await client.send_message(chat["id"], reply)
                        rt["sent_timestamps"].append(now_ms())
                        await storage.log_automation(user_id, f"Отправлен автоответ пользователю {username}.")
                    except Exception as exc:
                        await storage.log_automation(user_id, f"Ошибка автоответа {username}: {exc}", "error")
            else:
                await storage.log_automation(user_id, f"Новое сообщение от {username}; подходящего автоответа нет.")

            if auto.get("auto_read"):
                try:
                    await client.mark_chat_read(chat["id"])
                except Exception as exc:
                    await storage.log_automation(user_id, f"Не удалось прочитать чат {username}: {exc}", "error")

            await storage.update(user_id, lambda r: r["runtime"].update(
                processed_message_ids=rt["processed_message_ids"],
                sent_timestamps=rt["sent_timestamps"],
            ))
    except AuthError as exc:
        await storage.log_automation(user_id, f"Сессия недействительна: {exc}", "error")
        await _notify(user_id, f"⚠️ Авторизация Playerok недействительна: {exc}\nОбновите cookies через /login")
    except Exception as exc:
        await storage.log_automation(user_id, f"Проверка остановлена: {exc}", "error")
    finally:
        await client.close()


# ================= авто-поднятие =================

@locked
async def bump_cycle(user_id: int, manual: bool = False) -> None:
    rec = await storage.load(user_id)
    if not rec.get("cookies"):
        return
    s = rec["settings"]
    rt = rec["runtime"]
    bump = s["bump"]
    if not manual and (not bump.get("enabled") or not bump.get("accepted")):
        return
    if not manual and now_ms() < rt.get("next_bump_at", 0):
        return

    rt["next_bump_at"] = now_ms() + BUMP_INTERVAL_MS
    await storage.log_automation(user_id, "Ручной цикл поднятия запущен." if manual else "Цикл поднятия запущен.")

    client = PlayerokClient(rec["cookies"])
    try:
        viewer = await client.get_viewer()
        data = await client.get_items(statuses=["APPROVED"], max_pages=30)
        candidates = (data["items"] or [])[:15]
        if not candidates:
            await storage.log_automation(user_id, "Поднятие: активных лотов не найдено.")
            return

        state = prune_state(rt.setdefault("bump_state", {}))
        bumped = 0
        failed = 0
        for item in candidates:
            st = state.setdefault(item["id"], {"last_at": 0, "backoff_until": 0})
            if st.get("backoff_until") and now_ms() < st["backoff_until"]:
                continue
            if st.get("last_at") and now_ms() - st["last_at"] < REBUMP_COOLDOWN_MS:
                continue
            if item.get("priority") == "PREMIUM" and item.get("status_expiration_date"):
                try:
                    exp = datetime.fromisoformat(item["status_expiration_date"].replace("Z", "+00:00"))
                    if exp.timestamp() * 1000 > now_ms() + 3600000:
                        continue
                except Exception:
                    pass
            try:
                statuses = await client.get_priority_statuses(item["id"], item["price"])
                free = free_status(statuses)
                if not free:
                    st["backoff_until"] = now_ms() + ITEM_BACKOFF_MS
                    await storage.log_automation(user_id, f"«{item['name']}»: бесплатное поднятие сейчас недоступно.", "error")
                    continue
                await client.increase_item_priority(item["id"], free["id"])
                st["last_at"] = now_ms()
                st["backoff_until"] = 0
                bumped += 1
                await storage.log_automation(user_id, f"Поднят: «{item['name']}» — бесплатно")
                if bump.get("notifications", True):
                    await _notify(user_id, f"⬆️ Поднят: {item['name']} (бесплатно)")
            except Exception as exc:
                failed += 1
                st["backoff_until"] = now_ms() + ITEM_BACKOFF_MS
                await storage.log_automation(user_id, f"Ошибка поднятия «{item['name']}»: {exc}", "error")
            await storage.update(user_id, lambda r: r["runtime"].update(bump_state=state))
            await asyncio.sleep(0.8)

        rt["last_bump_at"] = now_ms()
        await storage.update(user_id, lambda r: r["runtime"].update(bump_state=state, last_bump_at=rt["last_bump_at"]))
        await storage.log_automation(user_id, f"Цикл поднятия завершён: {bumped} поднято, {failed} ошибок.")
        if bumped and bump.get("notifications", True):
            await _notify(user_id, f"⬆️ Поднятие завершено: {bumped} лотов{f', ошибок: {failed}' if failed else ''}")
    except AuthError as exc:
        await storage.log_automation(user_id, f"Сессия недействительна: {exc}", "error")
        await _notify(user_id, f"⚠️ Авторизация недействительна: {exc}")
    except Exception as exc:
        await storage.log_automation(user_id, f"Цикл поднятия остановлен: {exc}", "error")
    finally:
        await client.close()


# ================= авто-перевыставление =================

@locked
async def relist_cycle(user_id: int, manual: bool = False, force_paid_ok: bool = False) -> None:
    rec = await storage.load(user_id)
    if not rec.get("cookies"):
        return
    s = rec["settings"]
    rt = rec["runtime"]
    relist = s["relist"]
    if not manual and (not relist.get("enabled") or not relist.get("accepted")):
        return
    if not manual and now_ms() < rt.get("next_relist_at", 0):
        return

    delay_minutes = min(max(int(relist.get("delay_minutes") or 5), 1), 120)
    rt["next_relist_at"] = now_ms() + delay_minutes * 60000
    await storage.log_automation(user_id, "Ручной цикл перевыставления запущен." if manual else "Цикл перевыставления запущен.")

    client = PlayerokClient(rec["cookies"])
    try:
        viewer = await client.get_viewer()

        min_balance = max(float(relist.get("min_balance") or 0), 0)
        if min_balance > 0 and (viewer["balance"] or 0) < min_balance:
            await storage.log_automation(
                user_id, f"Баланс {money(viewer['balance'])} ниже минимума {money(min_balance)} — перевыставление пропущено.", "error"
            )
            return

        data = await client.get_items(statuses=["EXPIRED"], max_pages=30)
        candidates = (data["items"] or [])[:15]
        if not candidates:
            await storage.log_automation(user_id, "Перевыставление: истёкших лотов не найдено.")
            return

        paid_enabled = bool(relist.get("paid_enabled"))
        max_price = max(float(relist.get("max_price_per_item") or 0), 0)
        state = prune_state(rt.setdefault("relist_state", {}))
        relisted = 0
        failed = 0
        balance = float(viewer["balance"] or 0)

        for item in candidates:
            st = state.setdefault(item["id"], {"last_at": 0, "backoff_until": 0})
            if st.get("backoff_until") and now_ms() < st["backoff_until"]:
                continue
            if st.get("last_at") and now_ms() - st["last_at"] < REBUMP_COOLDOWN_MS:
                continue
            try:
                statuses = await client.get_priority_statuses(item["id"], item["price"])
                free = free_status(statuses)
                paid = None
                if paid_enabled:
                    paid_options = sorted(
                        [x for x in statuses if x["price"] > 0], key=lambda x: x["price"]
                    )
                    paid = next(
                        (x for x in paid_options if max_price == 0 or x["price"] <= max_price), None
                    )
                chosen = paid or free
                if not chosen:
                    await storage.log_automation(
                        user_id,
                        f"«{item['name']}»: подходящий тариф не найден (макс. {money(max_price) if max_price else 'без лимита'}).",
                        "error",
                    )
                    continue
                if chosen["price"] > 0:
                    if balance < chosen["price"]:
                        await storage.log_automation(
                            user_id, f"Недостаточно баланса для «{item['name']}» (нужно {money(chosen['price'])}).", "error"
                        )
                        break
                    if min_balance > 0 and (balance - chosen["price"]) < min_balance:
                        await storage.log_automation(
                            user_id, f"«{item['name']}»: после оплаты баланс уйдёт ниже минимума {money(min_balance)} — пропущено.", "error"
                        )
                        continue
                    balance -= chosen["price"]

                await client.publish_item(item["id"], chosen["id"])
                st["last_at"] = now_ms()
                st["backoff_until"] = 0
                relisted += 1
                await storage.log_automation(
                    user_id,
                    f"Перевыставлен: «{item['name']}» — {chosen['name'] or 'тариф'}, {money(chosen['price']) if chosen['price'] else 'бесплатно'}",
                )
                if relist.get("notifications", True):
                    await _notify(
                        user_id,
                        f"♻️ Перевыставлен: {item['name']} ({money(chosen['price']) if chosen['price'] else 'бесплатно'})",
                    )
            except Exception as exc:
                failed += 1
                st["backoff_until"] = now_ms() + ITEM_BACKOFF_MS
                await storage.log_automation(user_id, f"Ошибка перевыставления «{item['name']}»: {exc}", "error")
            await storage.update(user_id, lambda r: r["runtime"].update(relist_state=state))
            await asyncio.sleep(0.8)

        rt["last_relist_at"] = now_ms()
        await storage.update(user_id, lambda r: r["runtime"].update(relist_state=state, last_relist_at=rt["last_relist_at"]))
        await storage.log_automation(user_id, f"Цикл перевыставления завершён: {relisted} перевыставлено, {failed} ошибок.")
        if relisted and relist.get("notifications", True):
            await _notify(user_id, f"♻️ Перевыставление завершено: {relisted} лотов{f', ошибок: {failed}' if failed else ''}")
    except AuthError as exc:
        await storage.log_automation(user_id, f"Сессия недействительна: {exc}", "error")
        await _notify(user_id, f"⚠️ Авторизация недействительна: {exc}")
    except Exception as exc:
        await storage.log_automation(user_id, f"Цикл перевыставления остановлен: {exc}", "error")
    finally:
        await client.close()


# ================= возвраты =================

@locked
async def rollback_tick(user_id: int) -> None:
    rec = await storage.load(user_id)
    if not rec.get("cookies"):
        return
    s = rec["settings"]
    rt = rec["runtime"]
    rm = s["return_message"]
    if not rm.get("enabled") or not rm.get("accepted"):
        return
    if now_ms() < rt.get("next_rollback_at", 0):
        return
    rt["next_rollback_at"] = now_ms() + ROLLBACK_INTERVAL_MS

    client = PlayerokClient(rec["cookies"])
    try:
        viewer = await client.get_viewer()
        data = await client.get_deals(max_pages=3)
        deals = data["deals"] or []
        rolled_back = [d for d in deals if d.get("status") == "ROLLED_BACK"]

        if not rt.get("rollback_baseline_done"):
            rt["processed_rollback_ids"] = [d["id"] for d in rolled_back if d.get("id")]
            rt["rollback_baseline_done"] = True
            await storage.log_automation(
                user_id, f"Возвраты: базовая точка создана ({len(rolled_back)} текущих возвратов без сообщений)."
            )
            await storage.update(user_id, lambda r: r["runtime"].update(
                processed_rollback_ids=rt["processed_rollback_ids"],
                rollback_baseline_done=True,
            ))
            return

        seen = set(rt.get("processed_rollback_ids") or [])
        fresh = [d for d in rolled_back if d.get("id") and d["id"] not in seen]
        if not fresh:
            return

        for deal in fresh:
            rt["processed_rollback_ids"].append(deal["id"])
            text = render_return_text(rm.get("text") or "", deal).strip()
            if not text:
                await storage.log_automation(user_id, f"Возврат по «{deal['item']['name']}»: текст сообщения пуст — ничего не отправлено.")
                continue
            if not deal.get("chat_id"):
                await storage.log_automation(user_id, f"Возврат по «{deal['item']['name']}»: чат сделки не найден — сообщение не отправлено.", "error")
                continue
            try:
                await client.send_message(deal["chat_id"], text)
                username = (deal.get("user") or {}).get("username") or "покупателю"
                await storage.log_automation(user_id, f"Возврат: отправлено сообщение {username} по «{deal['item']['name']}».")
                if rm.get("notifications", True):
                    await _notify(user_id, f"↩️ Возврат: отправлено сообщение по «{deal['item']['name']}»")
            except Exception as exc:
                await storage.log_automation(user_id, f"Возврат: не удалось отправить сообщение по «{deal['item']['name']}»: {exc}", "error")
            await asyncio.sleep(0.6)

        await storage.update(user_id, lambda r: r["runtime"].update(
            processed_rollback_ids=rt["processed_rollback_ids"],
        ))
    except AuthError as exc:
        await storage.log_automation(user_id, f"Сессия недействительна: {exc}", "error")
    except Exception as exc:
        await storage.log_automation(user_id, f"Проверка возвратов остановлена: {exc}", "error")
    finally:
        await client.close()


# ================= сделки и отзывы =================

def render_deal_text(text: str, deal: dict) -> str:
    """Подстановка переменных {{username}} и {{item}} в сообщение по сделке."""
    item = ((deal.get("item") or {}).get("name")) or "товар"
    user = ((deal.get("user") or {}).get("username")) or "покупатель"
    return (text or "").replace("{{username}}", user).replace("{{item}}", item)


async def _send_deal_message(client: PlayerokClient, deal: dict, msg: str, user_id: int, label: str) -> None:
    try:
        await client.send_message(deal["chat_id"], msg)
        await storage.log_automation(user_id, f"{label}: отправлено по «{((deal.get('item') or {}).get('name') or 'товар')}».")
    except Exception as exc:
        await storage.log_automation(user_id, f"{label}: ошибка — {exc}", "error")


@locked
async def deal_tick(user_id: int) -> None:
    """Автоподтверждение сделок + сообщение после выполнения."""
    rec = await storage.load(user_id)
    if not rec.get("cookies"):
        return
    s = rec["settings"]
    rt = rec["runtime"]
    confirm = s.get("deal_confirm") or {}
    complete = s.get("deal_complete") or {}
    if not confirm.get("enabled") and not complete.get("enabled"):
        return
    if now_ms() < rt.get("next_deal_at", 0):
        return
    rt["next_deal_at"] = now_ms() + DEAL_INTERVAL_MS

    client = PlayerokClient(rec["cookies"])
    try:
        data = await client.get_deals(max_pages=6)
    except AuthError as exc:
        await storage.log_automation(user_id, f"Сессия недействительна: {exc}", "error")
        await client.close()
        return
    except Exception as exc:
        await storage.log_automation(user_id, f"Проверка сделок остановлена: {exc}", "error")
        await client.close()
        return

    try:
        deals = data.get("deals") or []
        processed = set(rt.get("processed_deal_ids") or [])
        confirm_items = set(confirm.get("items") or [])
        exclude_items = set(complete.get("exclude_items") or [])
        saved = False

        if complete.get("enabled") and not rt.get("deal_baseline_done"):
            # первый запуск — не шлём сообщения по старым выполненным сделкам
            for d in deals:
                if d.get("id") and (d.get("status") or "").upper() in ("CONFIRMED", "CONFIRMED_AUTOMATICALLY"):
                    processed.add(d["id"])
            rt["deal_baseline_done"] = True
            saved = True

        for d in deals:
            did = d.get("id")
            if not did:
                continue
            status = (d.get("status") or "").upper()
            item_id = ((d.get("item") or {}).get("id")) or ""

            # --- автоподтверждение (отправить товар) ---
            if confirm.get("enabled") and status in ("PAID", "PENDING"):
                if confirm_items and item_id not in confirm_items:
                    continue
                try:
                    await client.update_deal(did, "SENT")
                except Exception as exc:
                    await storage.log_automation(user_id, f"Ошибка автоподтверждения «{((d.get('item') or {}).get('name') or 'товар')}»: {exc}", "error")
                    continue
                await storage.log_automation(user_id, f"Автоподтверждено: «{((d.get('item') or {}).get('name') or 'товар')}».")
                msg = (confirm.get("message") or "").strip()
                if msg and d.get("chat_id"):
                    await _send_deal_message(client, d, render_deal_text(msg, d), user_id, "После подтверждения")
                continue

            # --- сообщение после выполнения ---
            if complete.get("enabled") and status in ("CONFIRMED", "CONFIRMED_AUTOMATICALLY"):
                if did in processed:
                    continue
                if item_id in exclude_items:
                    processed.add(did)
                    saved = True
                    continue
                msg = (complete.get("message") or "").strip()
                if msg and d.get("chat_id"):
                    await _send_deal_message(client, d, render_deal_text(msg, d), user_id, "После выполнения")
                processed.add(did)
                saved = True

        if saved or processed:
            rt["processed_deal_ids"] = list(processed)[-500:]
        await storage.update(user_id, lambda r: r["runtime"].update(
            processed_deal_ids=rt["processed_deal_ids"],
            deal_baseline_done=rt.get("deal_baseline_done", False),
            next_deal_at=rt["next_deal_at"],
        ))
    finally:
        await client.close()


@locked
async def reviews_tick(user_id: int) -> None:
    """Ответ на новые отзывы (по оценкам 1–5)."""
    rec = await storage.load(user_id)
    if not rec.get("cookies"):
        return
    s = rec["settings"]
    rt = rec["runtime"]
    rr = s.get("reviews_reply") or {}
    if not rr.get("enabled"):
        return
    if now_ms() < rt.get("next_reviews_at", 0):
        return
    rt["next_reviews_at"] = now_ms() + REVIEWS_INTERVAL_MS

    client = PlayerokClient(rec["cookies"])
    try:
        data = await client.get_testimonials(max_pages=3)
    except AuthError as exc:
        await storage.log_automation(user_id, f"Сессия недействительна: {exc}", "error")
        await client.close()
        return
    except Exception as exc:
        await storage.log_automation(user_id, f"Проверка отзывов остановлена: {exc}", "error")
        await client.close()
        return

    try:
        reviews = data.get("reviews") or []
        seen = set(rt.get("processed_review_ids") or [])
        msgs = rr.get("messages") or {}

        if not rt.get("review_baseline_done"):
            rt["processed_review_ids"] = [r["id"] for r in reviews if r.get("id")]
            rt["review_baseline_done"] = True
            await storage.update(user_id, lambda r: r["runtime"].update(
                processed_review_ids=rt["processed_review_ids"],
                review_baseline_done=True,
                next_reviews_at=rt["next_reviews_at"],
            ))
            return

        for rv in reviews:
            rid = rv.get("id")
            if not rid or rid in seen:
                continue
            rating = rv.get("rating") or 0
            msg = (msgs.get(str(rating)) or "").strip()
            if msg and rv.get("chat_id"):
                text = (msg.replace("{{username}}", (rv.get("user") or {}).get("username") or "")
                           .replace("{{rating}}", str(rating)))
                try:
                    await client.send_message(rv["chat_id"], text)
                    await storage.log_automation(user_id, f"Ответ на отзыв ({rating}★): отправлено.")
                except Exception as exc:
                    await storage.log_automation(user_id, f"Ответ на отзыв ({rating}★): ошибка — {exc}", "error")
            seen.add(rid)

        rt["processed_review_ids"] = list(seen)[-500:]
        await storage.update(user_id, lambda r: r["runtime"].update(
            processed_review_ids=rt["processed_review_ids"],
            next_reviews_at=rt["next_reviews_at"],
        ))
    finally:
        await client.close()


# ================= планировщик =================

async def run_all_cycles(user_id: int) -> None:
    """Запускает все причитающиеся циклы для пользователя (серийно, без пересечения)."""
    for fn in (automation_tick, deal_tick, reviews_tick, bump_cycle, relist_cycle, rollback_tick):
        try:
            await fn(user_id)
        except Exception as exc:
            await storage.log_automation(user_id, f"Внутренняя ошибка цикла: {exc}", "error")


async def scheduler_loop(interval: float = 2.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            for user_id in await storage.all_user_ids():
                rec = await storage.load(user_id)
                if not rec.get("cookies"):
                    continue
                await run_all_cycles(user_id)
        except Exception:
            pass
