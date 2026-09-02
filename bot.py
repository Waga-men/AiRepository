"""Telegram-бот «Playerok Tools» — порт расширения для playerok.com."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    MenuButtonCommands,
    Message,
    PreCheckoutQuery,
)

import config
import storage
from client import PlayerokClient, AuthError, parse_cookies
from fmt import esc, money, status_label, dt_str, time_str, now_ms
import automation

bot = None  # создаётся в make_bot()


def make_bot():
    """Создаёт Bot с учётом прокси/локального Bot API (обход блокировки)."""
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer

    kwargs = {}
    if config.BOT_API_URL:
        kwargs["session"] = AiohttpSession(
            api=TelegramAPIServer.from_base(config.BOT_API_URL)
        )
    elif config.PROXY:
        kwargs["session"] = AiohttpSession(proxy=config.PROXY)

    return Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        **kwargs,
    )
dp = Dispatcher(storage=MemoryStorage())
router = Router()

# Небольшой кэш списков, чтобы не дёргать API на каждый клик.
_cache: dict = {}


def cache_get(key: str, ttl: float = 30.0):
    item = _cache.get(key)
    if item and time.time() - item[0] < ttl:
        return item[1]
    return None


def cache_set(key: str, value) -> None:
    _cache[key] = (time.time(), value)
    if len(_cache) > 500:
        for k in list(_cache)[:100]:
            _cache.pop(k, None)


# ================= FSM =================

class Form(StatesGroup):
    login = State()
    reply = State()
    add_template = State()
    add_command = State()
    set_value = State()
    grant = State()          # ввод @username/ID для выдачи доступа
    grant_duration = State()  # ввод срока доступа
    set_price = State()      # владелец: ввод новой цены
    promo = State()          # ввод промокода при покупке
    promo_add = State()      # владелец: ввод нового промокода
    set_deal_message = State()   # текст сообщения сделки (confirm/complete)
    set_review_message = State() # текст ответа на отзыв (по оценке)
    bug = State()                # заявка на вознаграждение за найденный баг


# ================= доступ =================

# Команды, доступные без покупки (публичные).
PUBLIC_COMMANDS = {"start", "policy", "bug"}


class AccessMiddleware(BaseMiddleware):
    """Пропускает только владельца и пользователей с активным доступом.

    Публичные команды и диалоги /login, /promo доступны всем.
    """

    async def __call__(self, handler, event, data):
        user_id = event.from_user.id if event.from_user else None
        if user_id is None:
            return await handler(event, data)

        if await storage.is_allowed(user_id):
            return await handler(event, data)

        # подтверждение оплаты звёздами должно пройти всегда
        if getattr(event, "successful_payment", None):
            return await handler(event, data)

        # публичные команды
        text = getattr(event, "text", None) or getattr(event, "data", None) or ""
        if isinstance(text, str) and text.startswith("/"):
            cmd = text.split()[0].lstrip("/").split("@")[0].lower()
            if cmd in PUBLIC_COMMANDS:
                return await handler(event, data)

        # публичные кнопки покупки (callback_data)
        if isinstance(event, CallbackQuery) and (
            event.data in ("buy", "promo_enter")
            or (event.data or "").startswith("buy_dur:")
        ):
            return await handler(event, data)

        # диалоги, доступные без подписки: ввод cookies, промокода, заявка на баг
        state: FSMContext = data.get("state")
        if state is not None:
            cur = await state.get_state()
            if cur in ("Form:login", "Form:promo", "Form:bug"):
                return await handler(event, data)

        # отказ
        deny = (
            "⛔ <b>Доступ ограничен</b>\n\n"
            "Бот платный. Чтобы получить доступ, оплатите подписку звёздами Telegram\n\n"
            "После оплаты доступ активируется автоматически"
        )
        if isinstance(event, CallbackQuery):
            await event.answer("⛔ Доступ ограничен", show_alert=True)
            try:
                await event.message.answer(deny)
            except Exception:
                pass
        else:
            await event.answer(deny)
        return


async def _get_client(user_id: int) -> Optional[PlayerokClient]:
    cookies = await storage.get_cookies(user_id)
    if not cookies:
        return None
    return PlayerokClient(cookies)


# ================= утилиты клавиатур =================

def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def menu_kb():
    return kb([
        [btn("🏠 Дашборд", "dashboard")],
        [btn("💬 Чаты", "chats")],
        [btn("📦 Лоты", "lots")],
        [btn("⚙️ Авто", "auto")],
        [btn("🤝 Сделки и отзывы", "deals")],
        [btn("👥 Аккаунты", "accounts")],
        [btn("🔧 Настройки", "settings")],
    ])


def owner_menu_kb():
    return kb([
        [btn("🏠 Дашборд", "dashboard")],
        [btn("💬 Чаты", "chats")],
        [btn("📦 Лоты", "lots")],
        [btn("⚙️ Авто", "auto")],
        [btn("🤝 Сделки и отзывы", "deals")],
        [btn("👥 Аккаунты", "accounts")],
        [btn("🔧 Настройки", "settings")],
        [btn("👑 Админ-панель", "admin")],
    ])


def back_kb(to: str = "start"):
    return kb([[btn("‹ Назад", to)]])


def yesno_kb(data_yes: str):
    return kb([
        [btn("✅ Да", data_yes)],
        [btn("❌ Нет", "start")],
    ])


def split_text(text: str, limit: int = 4000):
    """Делит текст на части <= limit символов по границам строк."""
    chunks = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            if cur:
                chunks.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    return chunks


POLICY_URL = "https://telegra.ph/Pravila-servisa-Playerok-Bot-10-22"


def url_btn(text: str, url: str):
    return InlineKeyboardButton(text=text, url=url)


POLICY_TEXT = """Правила сервиса Playerok Bot
t.me/PlayerokTools_bot • 1 сентября 2026

1. Общие положения
1.1. Настоящие условия (далее — «Условия») регулируют порядок использования бота для автоматизированных продаж (далее — «Бот»), предоставленного пользователям платформы Playerok.com.
1.2. Используя Бот, вы соглашаетесь с настоящими Условиями. Если вы не согласны с какими-либо положениями Условий, вам следует отказаться от использования Бота.
1.3. Бот предоставляется для помощи в автоматизации процесса размещения и управления товарами на платформе Playerok.com.
1.4. Настоящие Условия могут быть изменены разработчиком Бота в любое время без предварительного уведомления. Изменения вступают в силу с момента их публикации.

2. Условия использования
2.1. Бот предназначен только для зарегистрированных пользователей Playerok.com, которые используют платформу для продажи цифровых товаров или услуг.
2.2. Пользователь обязуется использовать Бот только для целей, предусмотренных настоящими Условиями, и в рамках законодательства, действующего на территории его юрисдикции.
2.3. Для доступа к Боту может потребоваться регистрация и предоставление актуальной информации, включая, но не ограничиваясь, данные учетной записи Playerok.com и информацию о товарах, подлежащих автоматизации продаж.
2.4. Пользователь несет ответственность за все действия, совершенные через его учетную запись при использовании Бота, включая точность и актуальность информации о товарах.

3. Ограничения использования
3.1. Запрещается использовать Бот для продажи незаконных товаров, нарушающих права третьих лиц или нарушающих правила маркетплейса Playerok.com.
3.2. Пользователь не имеет права изменять программный код Бота, пытаться получить несанкционированный доступ к его функционалу или использовать Бот для создания конкурентных продуктов.
3.3. Пользователь обязуется не использовать Бот для совершения действий, нарушающих нормальное функционирование маркетплейса Playerok.com, включая, но не ограничиваясь, отправкой спама или вводом некорректной информации о товарах.

4. Ответственность
4.1. Разработчик Бота не несет ответственности за любые убытки, возникшие в результате использования Бота, включая потерю данных, упущенную прибыль или любые другие убытки, вызванные некорректным использованием Бота или техническими проблемами на платформе Playerok.com.
4.2. Пользователь самостоятельно несет ответственность за соблюдение всех применимых законов и правил, связанных с продажей товаров и услуг через платформу Playerok.com.
4.3. Бот предоставляется на условиях «как есть». Разработчик не гарантирует, что работа Бота будет бесперебойной или безошибочной, а также что Бот полностью соответствует ожиданиям пользователей.
4.4. Возврат средств не гарантируется при полном прекращении работы бота

5. Конфиденциальность и обработка данных
5.1. При использовании Бота может осуществляться сбор и обработка данных пользователя, необходимых для его функционирования. Эти данные включают информацию об учетной записи, товарах и операциях.
5.2. Личные данные пользователя обрабатываются в соответствии с действующими нормами о защите данных и конфиденциальности.
5.3. Разработчик не передает личные данные пользователей третьим лицам, за исключением случаев, предусмотренных законодательством.

6. Прекращение использования
6.1. Разработчик оставляет за собой право без предупреждения приостановить или прекратить доступ пользователя к Боту в случае нарушения настоящих Условий, а также в случае выявления злоупотреблений в использовании Бота.

7. Технические условия
7.1. Разработчик не имеет права использовать личные данные пользователей, даже в целях отладки ошибок, без их согласия
7.2. Вознаграждение за найденные технические ошибки осуществляется в виде подарка в Telegram. Заявку на вознаграждение необходимо подать через бот командой /bug. Размер вознаграждения зависит от степени критичности ошибки и не имеет строгих границ
7.3. Бесплатный тест может выдаваться любому пользователю по усмотрению разработчика. Наиболее весомыми факторами аккаунтов являются:
Количество отзывов
Количество сделок
Дата регистрации на сайте
Репутация
7.4. Возможны компенсации в случае технических неполадок со стороны бота. Каждый случай рассматривается индивидуально
7.5. Лимит аккаунтов в боте — 3 шт.
7.6. В случае любых проблем и вопросов обращаться к @MAP3X7AHA"""


# ================= /start и меню =================

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await storage.register_user(message.from_user.id, message.from_user.username or "")

    if await storage.is_owner(message.from_user.id):
        await message.answer("📋 Меню", reply_markup=owner_menu_kb())
        return

    if await storage.is_allowed(message.from_user.id):
        await message.answer("📋 Меню", reply_markup=menu_kb())
        return

    await message.answer(
        "👋 <b>Playerok Tools</b> — бот-помощник продавца Playerok\n\n"
        "Бот <b>платный</b>. Чтобы получить доступ, оплатите подписку звёздами Telegram\n\n"
        "После оплаты откроются дашборд, чаты, лоты, авто-поднятие, "
        "перевыставление, команды, сделки и возвраты\n\n"
        "👮 Используя бот, вы принимаете <a href=\"" + POLICY_URL + "\">политику конфиденциальности</a>",
        reply_markup=kb([
            [btn("⭐ Купить доступ", "buy")],
            [url_btn("👮 Политика конфиденциальности", POLICY_URL)],
        ]),
        disable_web_page_preview=True,
    )


@router.message(Command("policy"))
async def policy_cmd(message: Message):
    for chunk in split_text(POLICY_TEXT):
        await message.answer(chunk, disable_web_page_preview=True)
    await message.answer(
        "👮 Открыть политику на Telegraph:",
        reply_markup=kb([[url_btn("Правила сервиса Playerok Bot", POLICY_URL)]]),
    )


@router.message(Command("bug"))
async def bug_cmd(message: Message, state: FSMContext):
    await state.set_state(Form.bug)
    await message.answer(
        "🐞 <b>Заявка на вознаграждение за баг</b>\n\n"
        "Опишите найденную ошибку: что делали, что ожидали, что произошло. "
        "Чем подробнее — тем точнее оценим критичность",
        reply_markup=kb([[btn("❌ Отменить", "bug_cancel")]]),
    )


@router.callback_query(F.data == "bug_cancel")
async def bug_cancel_cb(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.answer("Отменено")
    await cq.message.edit_text("Отменено")


@router.message(Form.bug)
async def bug_receive(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    await state.clear()
    if not text:
        await message.answer("Описание не может быть пустым")
        return
    owner_id = (await storage.load_admin())["owner_id"]
    name = message.from_user.username or f"ID {message.from_user.id}"
    try:
        await bot.send_message(
            owner_id,
            f"🐞 <b>Заявка на вознаграждение за баг</b>\n\n"
            f"От: @{esc(name)} (ID {message.from_user.id})\n\n"
            f"{esc(text)}",
        )
    except Exception:
        pass
    await message.answer("✅ Заявка отправлена. С вами свяжутся для вознаграждения")


@router.callback_query(F.data == "login")
async def login_cb(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await state.set_state(Form.login)
    await cq.message.answer(
        "🔑 <b>Подключение аккаунта Playerok</b>\n\n"
        "Отправьте ваш <b>Token</b> (JWT). Он выглядит примерно так:\n"
        "<code>eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOi...</code>\n\n"
        "Где взять: откройте playerok.com → DevTools (F12) → вкладка <b>Application</b> → "
        "Cookies → playerok.com → кука <b>token</b> → скопируйте её значение и вставьте сюда",
        disable_web_page_preview=True,
    )


# ================= login: приём cookies =================

@router.message(Form.login)
async def login_receive(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    cookie_header = parse_cookies(raw)
    if not cookie_header:
        await message.answer("⚠️ Не удалось распознать токен. Пришлите JWT-токен целиком")
        return
    await message.answer("⏳ Проверяю сессию в Playerok…")
    client = PlayerokClient(cookie_header)
    try:
        viewer = await client.get_viewer()
    except AuthError as exc:
        await state.clear()
        await message.answer(f"❌ Сессия недействительна: {esc(exc)}\nПопробуйте ещё раз")
        return
    except Exception as exc:
        await state.clear()
        await message.answer(f"❌ Ошибка подключения: {esc(exc)}\nПопробуйте ещё раз")
        return
    finally:
        await client.close()

    # Проверка: этот аккаунт Playerok уже привязан к другому Telegram-пользователю?
    bound_to = await storage.get_playerok_owner(viewer["id"])
    if bound_to is not None and bound_to != message.from_user.id:
        await state.clear()
        await message.answer(
            "❌ Этот аккаунт Playerok уже привязан к другому пользователю бота.\n"
            "Один аккаунт Playerok можно привязать только один раз"
        )
        return

    name = viewer.get("username") or f"ID {viewer['id']}"
    res = await storage.add_account(message.from_user.id, viewer["id"], cookie_header, name)
    if res == -1:
        await state.clear()
        await message.answer(f"❌ Достигнут лимит: максимум {storage.MAX_ACCOUNTS} аккаунта")
        return
    if res == -2:
        await state.clear()
        await message.answer("Этот аккаунт уже добавлен")
        return

    await storage.bind_playerok(message.from_user.id, viewer["id"])
    await storage.update(
        message.from_user.id,
        lambda r: r.update(username=message.from_user.username or ""),
    )
    logging.info("login: user=%s добавил Playerok %s (%s)", message.from_user.id, viewer["id"], viewer.get("username"))
    await state.clear()
    await message.answer(
        f"✅ Аккаунт подключён и сделан активным!\n\n"
        f"Аккаунт: <b>{esc(name)}</b>\n"
        f"Баланс: <b>{money(viewer['balance'])}</b>\n"
        f"Непрочитанных: <b>{viewer['unread_chats_counter']}</b>",
        reply_markup=menu_kb(),
    )


# ================= Аккаунты =================

def _acc_label(a: dict) -> str:
    return a.get("name") or f"ID {a.get('playerok_id')}"


async def render_accounts(cq: CallbackQuery):
    accs = await storage.list_accounts(cq.from_user.id)
    lines = [f"👥 <b>Аккаунты</b> · {len(accs)}/{storage.MAX_ACCOUNTS}\n"]
    if not accs:
        lines.append("Аккаунтов нет")
    for i, a in enumerate(accs):
        mark = "🟢" if i == 0 else "🔴"
        lines.append(f"{mark} <b>{esc(_acc_label(a))}</b>" + (" — активен" if i == 0 else ""))

    rows = []
    for i, a in enumerate(accs):
        if i > 0:
            rows.append([btn(f"🔄 Сделать активным: {_acc_label(a)}", f"acc_switch:{i}")])
    rows.append([btn("➕ Добавить аккаунт", "login")])
    if len(accs) > 1:
        rows.append([btn("🔁 Перенести настройки", "acc_transfer")])
    for i, a in enumerate(accs):
        rows.append([btn(f"🗑 Удалить: {_acc_label(a)}", f"acc_del:{i}")])
    rows.append([btn("‹ Меню", "start")])
    await cq.message.edit_text("\n".join(lines)[:4000], reply_markup=kb(rows))


@router.callback_query(F.data == "accounts")
async def accounts_cb(cq: CallbackQuery):
    await cq.answer()
    await render_accounts(cq)


@router.callback_query(F.data.startswith("acc_switch:"))
async def acc_switch_cb(cq: CallbackQuery):
    index = int(cq.data.split(":", 1)[1])
    ok = await storage.switch_account(cq.from_user.id, index)
    await cq.answer("Переключено ✅" if ok else "Ошибка")
    await render_accounts(cq)


@router.callback_query(F.data == "acc_transfer")
async def acc_transfer_cb(cq: CallbackQuery):
    await cq.answer()
    accs = await storage.list_accounts(cq.from_user.id)
    rows = []
    for i, a in enumerate(accs):
        rows.append([btn(f"С аккаунта: {_acc_label(a)}", f"acc_tf_from:{i}")])
    rows.append([btn("‹ Аккаунты", "accounts")])
    await cq.message.edit_text(
        "🔁 <b>Перенести настройки</b>\n\nВыберите аккаунт, С которого копировать настройки:",
        reply_markup=kb(rows),
    )


@router.callback_query(F.data.startswith("acc_tf_from:"))
async def acc_tf_from_cb(cq: CallbackQuery):
    src = int(cq.data.split(":", 1)[1])
    await cq.answer()
    accs = await storage.list_accounts(cq.from_user.id)
    rows = []
    for i, a in enumerate(accs):
        if i == src:
            continue
        rows.append([btn(f"На аккаунт: {_acc_label(a)}", f"acc_tf_to:{src}:{i}")])
    rows.append([btn("‹ Аккаунты", "accounts")])
    await cq.message.edit_text(
        f"🔁 <b>Перенести настройки</b>\n\nИсточник: <b>{esc(_acc_label(accs[src]))}</b>\n\n"
        "Выберите аккаунт, НА который скопировать настройки (его текущие настройки будут заменены):",
        reply_markup=kb(rows),
    )


@router.callback_query(F.data.startswith("acc_tf_to:"))
async def acc_tf_to_cb(cq: CallbackQuery):
    _, src, dst = cq.data.split(":")
    ok = await storage.transfer_settings(cq.from_user.id, int(src), int(dst))
    await cq.answer("Настройки перенесены ✅" if ok else "Ошибка")
    await render_accounts(cq)


@router.callback_query(F.data.startswith("acc_del:"))
async def acc_del_cb(cq: CallbackQuery):
    index = int(cq.data.split(":", 1)[1])
    await cq.answer()
    accs = await storage.list_accounts(cq.from_user.id)
    if index >= len(accs):
        await render_accounts(cq)
        return
    await cq.message.edit_text(
        f"🗑 Удалить аккаунт <b>{esc(_acc_label(accs[index]))}</b>?\n\n"
        "Будут удалены cookies и настройки этого аккаунта",
        reply_markup=kb([
            [btn("🗑 Да, удалить", f"acc_del_yes:{index}")],
            [btn("‹ Нет", "accounts")],
        ]),
    )


@router.callback_query(F.data.startswith("acc_del_yes:"))
async def acc_del_yes_cb(cq: CallbackQuery):
    index = int(cq.data.split(":", 1)[1])
    removed_pid = await storage.delete_account(cq.from_user.id, index)
    if removed_pid:
        await storage.unbind_playerok(removed_pid)
    _cache.clear()
    await cq.answer("Удалён 🗑")
    await render_accounts(cq)


# ================= Дашборд =================

async def render_dashboard(message: Message, user_id: int, edit: bool = False):
    client = await _get_client(user_id)
    if not client:
        await message.answer("Сначала подключите аккаунт во вкладке «Аккаунты»")
        return
    try:
        viewer = await client.get_viewer()
    except AuthError as exc:
        await message.answer(f"⚠️ {esc(exc)}\nОбновите сессию во вкладке «Аккаунты»")
        return
    except Exception as exc:
        await message.answer(f"⚠️ Ошибка: {esc(exc)}")
        return
    finally:
        await client.close()

    rec = await storage.load(user_id)
    s = rec["settings"]
    auto_on = s["automation"]["enabled"]
    relist_on = s["relist"]["enabled"]

    text = (
        f"🏠 <b>Дашборд</b>\n\n"
        f"Аккаунт: <b>{esc(viewer['username'] or '—')}</b>\n"
        f"Баланс: <b>{money(viewer['balance'])}</b>\n"
        f"Непрочитанных сообщений: <b>{viewer['unread_chats_counter']}</b>\n"
        f"Отзывов: {viewer['testimonial_counter']}\n\n"
        f"Чат-автоматизация: {'✅ вкл' if auto_on else '⏸ выкл'}\n"
        f"Перевыставление: {'✅ вкл' if relist_on else '⏸ выкл'}"
    )
    kbd = kb([
        [btn("💬 Чаты", "chats")],
        [btn("📦 Лоты", "lots")],
        [btn("✅ Прочитать все", "readall")],
        [btn("♻️ Перевыставить лоты", "runrelist_confirm")],
        [btn("‹ Меню", "start")],
    ])
    if edit:
        await message.edit_text(text, reply_markup=kbd)
    else:
        await message.answer(text, reply_markup=kbd)


@router.callback_query(F.data == "dashboard")
async def dashboard_cb(cq: CallbackQuery):
    await cq.answer()
    await render_dashboard(cq.message, cq.from_user.id, edit=True)


# ================= Чаты =================

async def fetch_chats(user_id: int, max_pages: int = 5):
    key = f"chats:{user_id}:{max_pages}"
    cached = cache_get(key)
    if cached:
        return cached
    client = await _get_client(user_id)
    if not client:
        return None
    try:
        data = await client.get_chats(max_pages=max_pages)
    finally:
        await client.close()
    cache_set(key, data)
    return data


async def render_chats(message: Message, user_id: int, page: int = 0, edit: bool = False):
    try:
        data = await fetch_chats(user_id)
    except AuthError as exc:
        await message.answer(f"⚠️ {esc(exc)}\nОбновите сессию во вкладке «Аккаунты»")
        return
    except Exception as exc:
        await message.answer(f"⚠️ Ошибка загрузки чатов: {esc(exc)}")
        return
    if data is None:
        await message.answer("Сначала подключите аккаунт во вкладке «Аккаунты»")
        return
    viewer = data["viewer"]
    chats = data["chats"]
    per_page = 8
    total_pages = max(1, (len(chats) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = chats[page * per_page:(page + 1) * per_page]

    if not chats:
        rows = [
            [btn("📋 Шаблоны", "templates")],
            [btn("💬 Команды", "commands")],
            [btn("‹ Меню", "start")],
        ]
        if edit:
            await message.edit_text("💬 Чатов нет", reply_markup=kb(rows))
        else:
            await message.answer("💬 Чатов нет", reply_markup=kb(rows))
        return

    unread_total = sum(1 for c in chats if c["unread_counter"] > 0)
    lines = [f"💬 <b>Чаты</b> · непрочитанных: {unread_total}\n"]
    for c in chunk:
        other = automation.other_participant(c, viewer)
        name = other["username"] if other and other.get("username") else "—"
        unread = c["unread_counter"]
        badge = f"🔵{unread} " if unread else ""
        last = c["last_message"]
        preview = (last["text"] if last else "")[:28]
        lines.append(f"{badge}<b>{esc(name)}</b> · {esc(preview) if preview else '…'}")

    rows = []
    for c in chunk:
        other = automation.other_participant(c, viewer)
        name = other["username"] if other and other.get("username") else "чат"
        rows.append([btn(f"💬 {name}", f"chat:{c['id']}")])
    if total_pages > 1:
        if page > 0:
            rows.append([btn(f"‹ Страница {page+1}/{total_pages}", f"chats_p:{page-1}")])
        if page < total_pages - 1:
            rows.append([btn("Вперёд ›", f"chats_p:{page+1}")])
    rows.append([btn("📋 Шаблоны", "templates")])
    rows.append([btn("💬 Команды", "commands")])
    rows.append([btn("✅ Прочитать все", "readall")])
    rows.append([btn("‹ Меню", "start")])

    text = "\n".join(lines)
    if edit:
        await message.edit_text(text, reply_markup=kb(rows))
    else:
        await message.answer(text, reply_markup=kb(rows))


@router.callback_query(F.data == "chats")
async def chats_cb(cq: CallbackQuery):
    await cq.answer()
    await render_chats(cq.message, cq.from_user.id, edit=True)


@router.callback_query(F.data.startswith("chats_p:"))
async def chats_page_cb(cq: CallbackQuery):
    await cq.answer()
    page = int(cq.data.split(":")[1])
    await render_chats(cq.message, cq.from_user.id, page=page, edit=True)


# ================= Чат (сообщения) =================

async def render_chat(cq: CallbackQuery, chat_id: str):
    await cq.answer()
    client = await _get_client(cq.from_user.id)
    if not client:
        await cq.message.answer("Сначала подключите аккаунт во вкладке «Аккаунты»")
        return
    try:
        viewer = await client.get_viewer()
        res = await client.get_messages(chat_id, 24)
    except AuthError as exc:
        await cq.message.answer(f"⚠️ {esc(exc)}\nОбновите сессию во вкладке «Аккаунты»")
        return
    except Exception as exc:
        await cq.message.answer(f"⚠️ Ошибка: {esc(exc)}")
        return
    finally:
        await client.close()

    msgs = res["messages"]
    msgs.sort(key=lambda m: m["created_at"] or "")
    shown = msgs[-12:]
    lines = [f"💬 <b>Сообщения</b> ({res['total_count']})\n"]
    for m in shown:
        who = (m["user"] or {})
        name = who.get("username") or "—"
        mine = who.get("id") == viewer["id"]
        prefix = "Вы" if mine else name
        body = m["text"] or (f"(событие: {m['event']})" if m.get("event") else "…")
        lines.append(f"[{time_str(m['created_at'])}] <b>{esc(prefix)}</b>: {esc(body[:120])}")

    kbd = kb([
        [btn("✍️ Ответить", f"reply:{chat_id}")],
        [btn("📋 Шаблоны", f"tpl_send_list:{chat_id}")],
        [btn("✅ Прочитан", f"read:{chat_id}")],
        [btn("‹ Чаты", "chats")],
    ])
    await cq.message.edit_text("\n".join(lines)[:4000], reply_markup=kbd)


@router.callback_query(F.data.startswith("chat:"))
async def chat_cb(cq: CallbackQuery):
    chat_id = cq.data.split(":", 1)[1]
    await render_chat(cq, chat_id)


# ================= Ответить (FSM) =================

@router.callback_query(F.data.startswith("reply:"))
async def reply_cb(cq: CallbackQuery, state: FSMContext):
    chat_id = cq.data.split(":", 1)[1]
    await state.set_state(Form.reply)
    await state.update_data(chat_id=chat_id)
    await cq.answer()
    await cq.message.answer(
        "✍️ Введите текст сообщения (поддерживаются переменные <code>{{username}}</code> и <code>{{time}}</code>):"
    )


@router.message(Form.reply)
async def reply_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get("chat_id")
    await state.clear()
    if not chat_id:
        await message.answer("Ошибка: не выбран чат")
        return

    client = await _get_client(message.from_user.id)
    if not client:
        await message.answer("Сначала подключите аккаунт во вкладке «Аккаунты»")
        return
    try:
        viewer = await client.get_viewer()
        chats_data = await client.get_chats(max_pages=5)
        chat = next((c for c in chats_data["chats"] if c["id"] == chat_id), None)
        user = automation.other_participant(chat, viewer) if chat else None
        name = user["username"] if user and user.get("username") else "покупатель"
        text = (message.text or "").replace("{{username}}", name).replace(
            "{{time}}", time.strftime("%H:%M")
        )
        await client.send_message(chat_id, text)
    except AuthError as exc:
        await message.answer(f"⚠️ {esc(exc)}\nОбновите сессию во вкладке «Аккаунты»")
        return
    except Exception as exc:
        await message.answer(f"⚠️ Ошибка: {esc(exc)}")
        return
    finally:
        await client.close()

    await message.answer("✅ Сообщение отправлено", reply_markup=back_kb("chats"))


# ================= Прочитать =================

@router.callback_query(F.data.startswith("read:"))
async def read_one_cb(cq: CallbackQuery):
    chat_id = cq.data.split(":", 1)[1]
    await cq.answer("Отмечаю прочитанным…")
    client = await _get_client(cq.from_user.id)
    if not client:
        await cq.message.answer("Сначала подключите аккаунт во вкладке «Аккаунты»")
        return
    try:
        await client.mark_chat_read(chat_id)
    except Exception as exc:
        await cq.message.answer(f"⚠️ {esc(exc)}")
        return
    finally:
        await client.close()
    await cq.answer("Прочитан ✅")


@router.callback_query(F.data == "readall")
async def read_all_cb(cq: CallbackQuery):
    await cq.answer("Обрабатываю…")
    data = await fetch_chats(cq.from_user.id, max_pages=40)
    if data is None:
        await cq.message.answer("Сначала подключите аккаунт во вкладке «Аккаунты»")
        return
    unread = [c for c in data["chats"] if c["unread_counter"] > 0]
    if not unread:
        await cq.message.answer("Непрочитанных чатов нет")
        return

    client = await _get_client(cq.from_user.id)
    if not client:
        await cq.message.answer("Сначала подключите аккаунт во вкладке «Аккаунты»")
        return
    done = 0
    failed = 0
    try:
        for c in unread:
            try:
                await client.mark_chat_read(c["id"])
                done += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.1)
    finally:
        await client.close()
    _cache.pop(f"chats:{cq.from_user.id}:5", None)
    _cache.pop(f"chats:{cq.from_user.id}:40", None)
    await cq.message.answer(
        f"✅ Все чаты отмечены: {done}" + (f", ошибок: {failed}" if failed else ""),
        reply_markup=back_kb("chats"),
    )


# ================= Лоты =================

async def fetch_items(user_id: int, statuses=None):
    key = f"items:{user_id}:{','.join(statuses or [])}"
    cached = cache_get(key)
    if cached:
        return cached
    client = await _get_client(user_id)
    if not client:
        return None
    try:
        data = await client.get_items(statuses=statuses, max_pages=30)
    finally:
        await client.close()
    cache_set(key, data)
    return data


async def render_lots(cq: CallbackQuery, filter_status: str = "ALL", page: int = 0, edit: bool = True):
    await cq.answer()
    statuses = None if filter_status == "ALL" else [filter_status]
    try:
        data = await fetch_items(cq.from_user.id, statuses)
    except AuthError as exc:
        await cq.message.answer(f"⚠️ {esc(exc)}\nОбновите сессию во вкладке «Аккаунты»")
        return
    except Exception as exc:
        await cq.message.answer(f"⚠️ Ошибка загрузки лотов: {esc(exc)}")
        return
    if data is None:
        await cq.message.answer("Сначала подключите аккаунт во вкладке «Аккаунты»")
        return
    items = data["items"]
    per_page = 6
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = items[page * per_page:(page + 1) * per_page]

    if not items:
        await cq.message.answer("📦 Лотов нет", reply_markup=back_kb())
        return

    lines = [f"📦 <b>Лоты</b> · {len(items)} шт.\n"]
    for it in chunk:
        p = money(it["price"])
        extra = []
        if it.get("priority") == "PREMIUM":
            extra.append("⭐премиум")
        if it.get("views_counter"):
            extra.append(f"👁{it['views_counter']}")
        suffix = " · " + " ".join(extra) if extra else ""
        lines.append(f"• <b>{esc(it['name'])}</b> — {p} [{status_label(it['status'])}]{suffix}")

    rows = []
    for it in chunk:
        if it["status"] == "APPROVED":
            rows.append([btn(f"⬆️ Поднять: {it['name']}", f"lot_bump:{it['id']}")])
        elif it["status"] in ("EXPIRED", "DRAFT"):
            rows.append([btn(f"♻️ Выставить: {it['name']}", f"lot_relist:{it['id']}")])

    rows.append([btn("Активные", "lots_f:APPROVED")])
    rows.append([btn("Истёкшие", "lots_f:EXPIRED")])
    rows.append([btn("Черновики", "lots_f:DRAFT")])
    rows.append([btn("Проданные", "lots_f:SOLD")])
    rows.append([btn("Все", "lots_f:ALL")])
    if total_pages > 1:
        if page > 0:
            rows.append([btn(f"‹ Страница {page+1}/{total_pages}", f"lots_p:{filter_status}:{page-1}")])
        if page < total_pages - 1:
            rows.append([btn("Вперёд ›", f"lots_p:{filter_status}:{page+1}")])
    rows.append([btn("‹ Меню", "start")])

    text = "\n".join(lines)[:4000]
    if edit:
        await cq.message.edit_text(text, reply_markup=kb(rows))
    else:
        await cq.message.answer(text, reply_markup=kb(rows))


@router.callback_query(F.data == "lots")
async def lots_cb(cq: CallbackQuery):
    await render_lots(cq, "ALL")


@router.callback_query(F.data.startswith("lots_f:"))
async def lots_filter_cb(cq: CallbackQuery):
    await render_lots(cq, cq.data.split(":", 1)[1])


@router.callback_query(F.data.startswith("lots_p:"))
async def lots_page_cb(cq: CallbackQuery):
    _, fs, page = cq.data.split(":")
    await render_lots(cq, fs, int(page))


# ================= Ручные поднять/выставить =================

async def pick_priority(client: PlayerokClient, item_id: str, price: float, mode: str):
    statuses = await client.get_priority_statuses(item_id, price)
    free = automation.free_status(statuses)
    paid = sorted([s for s in statuses if s["price"] > 0], key=lambda s: s["price"])
    return statuses, free, paid


def fmt_statuses(statuses, free, paid):
    lines = []
    if free:
        lines.append(f"0 ₽ — бесплатно")
    for s in paid[:8]:
        lines.append(f"{money(s['price'])} — {s['name'] or 'тариф'}")
    return lines


@router.callback_query(F.data.startswith("lot_bump:"))
async def lot_bump_cb(cq: CallbackQuery):
    item_id = cq.data.split(":", 1)[1]
    await cq.answer("Загружаю тарифы…")
    client = await _get_client(cq.from_user.id)
    if not client:
        await cq.message.answer("Сначала подключите аккаунт во вкладке «Аккаунты»")
        return
    try:
        items = (await fetch_items(cq.from_user.id, ["APPROVED"])) or {"items": []}
        item = next((i for i in items["items"] if i["id"] == item_id), None)
        if not item:
            await cq.message.answer("Лот не найден (обновите список)")
            return
        statuses, free, paid = await pick_priority(client, item_id, item["price"], "bump")
        lines = fmt_statuses(statuses, free, paid)
        # для поднятия расширение берёт только бесплатный тариф; платный — опционально
        buttons = []
        if free:
            buttons.append([btn(f"Бесплатно — поднять", f"bump_do:{item_id}:{free['id']}")])
        for s in paid[:6]:
            buttons.append([btn(f"{money(s['price'])} — поднять", f"bump_do:{item_id}:{s['id']}")])
        buttons.append([btn("‹ Лоты", "lots")])
        await cq.message.answer(
            f"⬆️ <b>Поднять «{esc(item['name'])}»</b>\n\nТарифы:\n" + "\n".join(lines),
            reply_markup=kb(buttons),
        )
    except Exception as exc:
        await cq.message.answer(f"⚠️ {esc(exc)}")
    finally:
        await client.close()


@router.callback_query(F.data.startswith("lot_relist:"))
async def lot_relist_cb(cq: CallbackQuery):
    item_id = cq.data.split(":", 1)[1]
    await cq.answer("Загружаю тарифы…")
    client = await _get_client(cq.from_user.id)
    if not client:
        await cq.message.answer("Сначала подключите аккаунт во вкладке «Аккаунты»")
        return
    try:
        items = (await fetch_items(cq.from_user.id)) or {"items": []}
        item = next((i for i in items["items"] if i["id"] == item_id), None)
        if not item:
            await cq.message.answer("Лот не найден (обновите список)")
            return
        statuses, free, paid = await pick_priority(client, item_id, item["price"], "relist")
        lines = fmt_statuses(statuses, free, paid)
        buttons = []
        if free:
            buttons.append([btn(f"Бесплатно — выставить", f"relist_do:{item_id}:{free['id']}")])
        for s in paid[:6]:
            buttons.append([btn(f"{money(s['price'])} — выставить", f"relist_do:{item_id}:{s['id']}")])
        buttons.append([btn("‹ Лоты", "lots")])
        await cq.message.answer(
            f"♻️ <b>Выставить «{esc(item['name'])}»</b>\n\nТарифы:\n" + "\n".join(lines),
            reply_markup=kb(buttons),
        )
    except Exception as exc:
        await cq.message.answer(f"⚠️ {esc(exc)}")
    finally:
        await client.close()


@router.callback_query(F.data.startswith("bump_do:"))
async def bump_do_cb(cq: CallbackQuery):
    _, item_id, sid = cq.data.split(":")
    await cq.answer("Поднимаю…")
    client = await _get_client(cq.from_user.id)
    if not client:
        await cq.message.answer("Сначала подключите аккаунт во вкладке «Аккаунты»")
        return
    try:
        res = await client.increase_item_priority(item_id, sid)
        await cq.message.answer(f"✅ Лот «{esc(res['item']['name'])}» поднят", reply_markup=back_kb("lots"))
    except Exception as exc:
        await cq.message.answer(f"⚠️ {esc(exc)}")
    finally:
        await client.close()


@router.callback_query(F.data.startswith("relist_do:"))
async def relist_do_cb(cq: CallbackQuery):
    _, item_id, sid = cq.data.split(":")
    await cq.answer("Выставляю…")
    client = await _get_client(cq.from_user.id)
    if not client:
        await cq.message.answer("Сначала подключите аккаунт во вкладке «Аккаунты»")
        return
    try:
        res = await client.publish_item(item_id, sid)
        await cq.message.answer(f"✅ Лот «{esc(res['item']['name'])}» выставлен", reply_markup=back_kb("lots"))
    except Exception as exc:
        await cq.message.answer(f"⚠️ {esc(exc)}")
    finally:
        await client.close()


# ================= Авто =================

# Темы эмодзи статусов (вкл/выкл). Ключ хранится в settings["emoji_theme"].
EMOJI_THEMES = {
    "green":  ("🟢", "🔴", "Зелёная"),
    "blue":   ("🔵", "🔴", "Синяя"),
    "purple": ("🟣", "⚫", "Фиолетовая"),
    "orange": ("🟠", "⚫", "Оранжевая"),
    "yellow": ("🟡", "⚫", "Жёлтая"),
}


def theme_icons(settings: dict):
    """Возвращает (вкл, выкл) для текущей темы пользователя."""
    key = (settings or {}).get("emoji_theme", "green")
    on, off, _ = EMOJI_THEMES.get(key, EMOJI_THEMES["green"])
    return on, off


def _checked(flag: bool, on: str = "🟢", off: str = "🔴") -> str:
    return on if flag else off


def _auto_toggle(page: str, key: str, checked: bool, label: str, on: str = "🟢", off: str = "🔴"):
    return btn(f"{_checked(checked, on, off)} {label}", f"atoggle:{page}:{key}")


@router.callback_query(F.data == "auto")
async def auto_cb(cq: CallbackQuery):
    await cq.answer()
    rows = [
        [btn("💬 Чат-автоматизация", "auto_chat")],
        [btn("⬆️ Авто-поднятие", "auto_bump")],
        [btn("♻️ Авто-перевыставление", "auto_relist")],
        [btn("↩️ Сообщение при возврате", "auto_return")],
        [btn("⬆️ Поднять сейчас", "runbump")],
        [btn("♻️ Перевыставить сейчас", "runrelist_confirm")],
        [btn("📜 Журнал", "auto_log")],
        [btn("‹ Меню", "start")],
    ]
    await cq.message.edit_text("⚙️ <b>Авто</b>\nВыберите функцию для настройки:", reply_markup=kb(rows))


@router.callback_query(F.data == "auto_chat")
async def auto_chat_cb(cq: CallbackQuery):
    await cq.answer()
    rec = await storage.load(cq.from_user.id)
    s = rec["settings"]["automation"]
    on, off = theme_icons(rec["settings"])
    text = (
        "💬 <b>Чат-автоматизация</b>\n\n"
        f"Задержка автоответа: <b>{s['delay_seconds']}с</b>\n"
        f"Лимит в час: <b>{s['hourly_limit']}</b>\n"
        f"Период опроса: <b>{s['poll_seconds']}с</b>"
    )
    rows = [
        [_auto_toggle("auto_chat", "automation.enabled", s["enabled"], "Включено", on, off)],
        [_auto_toggle("auto_chat", "automation.keyword_enabled", s["keyword_enabled"], "Ответы по командам", on, off)],
        [_auto_toggle("auto_chat", "automation.auto_read", s["auto_read"], "Авто-прочтение", on, off)],
        [btn("⚙️ Задержка автоответа", "set:auto_chat:delay_seconds")],
        [btn("⚙️ Лимит в час", "set:auto_chat:hourly_limit")],
        [btn("⚙️ Период опроса", "set:auto_chat:poll_seconds")],
        [btn("‹ Авто", "auto")],
    ]
    await cq.message.edit_text(text, reply_markup=kb(rows))


@router.callback_query(F.data == "auto_bump")
async def auto_bump_cb(cq: CallbackQuery):
    await cq.answer()
    rec = await storage.load(cq.from_user.id)
    s = rec["settings"]["bump"]
    on, off = theme_icons(rec["settings"])
    text = "⬆️ <b>Авто-поднятие</b>\n\nБесплатно поднимает активные лоты"
    rows = [
        [_auto_toggle("auto_bump", "bump.enabled", s["enabled"], "Включено", on, off)],
        [btn("⬆️ Поднять сейчас", "runbump")],
        [btn("‹ Авто", "auto")],
    ]
    await cq.message.edit_text(text, reply_markup=kb(rows))


@router.callback_query(F.data == "auto_relist")
async def auto_relist_cb(cq: CallbackQuery):
    await cq.answer()
    rec = await storage.load(cq.from_user.id)
    s = rec["settings"]["relist"]
    on, off = theme_icons(rec["settings"])
    text = (
        "♻️ <b>Авто-перевыставление</b>\n\n"
        f"Макс. сумма: <b>{money(s['max_price_per_item'])}</b>\n"
        f"Мин. баланс: <b>{money(s['min_balance'])}</b>\n"
        f"Задержка: <b>{s['delay_minutes']} мин</b>"
    )
    rows = [
        [_auto_toggle("auto_relist", "relist.enabled", s["enabled"], "Включено", on, off)],
        [_auto_toggle("auto_relist", "relist.paid_enabled", s["paid_enabled"], "Платный тариф", on, off)],
        [btn("⚙️ Макс. сумма", "set:auto_relist:max_price_per_item")],
        [btn("⚙️ Мин. баланс", "set:auto_relist:min_balance")],
        [btn("⚙️ Задержка, мин", "set:auto_relist:delay_minutes")],
        [btn("♻️ Перевыставить сейчас", "runrelist_confirm")],
        [btn("‹ Авто", "auto")],
    ]
    await cq.message.edit_text(text, reply_markup=kb(rows))


@router.callback_query(F.data == "auto_return")
async def auto_return_cb(cq: CallbackQuery):
    await cq.answer()
    rec = await storage.load(cq.from_user.id)
    s = rec["settings"]
    on, off = theme_icons(rec["settings"])
    text = "↩️ <b>Сообщение при возврате</b>"
    rows = [
        [_auto_toggle("auto_return", "return_message.enabled", s["return_message"]["enabled"], "Включено", on, off)],
        [btn("✉️ Текст сообщения", "set:auto_return:return_text")],
        [btn("‹ Авто", "auto")],
    ]
    await cq.message.edit_text(text, reply_markup=kb(rows))


# тумблер (двухуровневые ключи вида section.field)
@router.callback_query(F.data.startswith("atoggle:"))
async def atoggle_cb(cq: CallbackQuery):
    _, page, key = cq.data.split(":", 2)
    section, field = key.split(".")

    def mutate(rec):
        rec["settings"][section][field] = not rec["settings"][section][field]
        if rec["settings"][section][field]:
            rec["settings"][section]["accepted"] = True

    await storage.update(cq.from_user.id, mutate)
    await cq.answer("Сохранено ✅")
    if page == "auto_chat":
        await auto_chat_cb(cq)
    elif page == "auto_bump":
        await auto_bump_cb(cq)
    elif page == "auto_relist":
        await auto_relist_cb(cq)
    elif page == "auto_return":
        await auto_return_cb(cq)


# тумблер верхнего уровня (deal_confirm / deal_complete / reviews_reply)
@router.callback_query(F.data.startswith("toggle:"))
async def toggle_cb(cq: CallbackQuery):
    key = cq.data.split(":", 1)[1]

    def mutate(rec):
        rec["settings"][key]["enabled"] = not rec["settings"][key]["enabled"]
        if rec["settings"][key]["enabled"]:
            rec["settings"][key]["accepted"] = True

    await storage.update(cq.from_user.id, mutate)
    await cq.answer("Сохранено ✅")
    await deals_cb(cq)


@router.callback_query(F.data == "runbump")
async def runbump_cb(cq: CallbackQuery):
    await cq.answer("Запускаю цикл поднятия…")
    await cq.message.answer("⬆️ Запускаю ручной цикл поднятия…")
    await automation.bump_cycle(cq.from_user.id, manual=True)
    await cq.message.answer("Готово. См. /start → Авто → Журнал", reply_markup=back_kb("auto"))


@router.callback_query(F.data == "runrelist_confirm")
async def runrelist_confirm_cb(cq: CallbackQuery):
    rec = await storage.load(cq.from_user.id)
    paid = rec["settings"]["relist"]["paid_enabled"]
    await cq.answer()
    if paid:
        await cq.message.answer(
            "♻️ Запустить перевыставление? Включён платный тариф — деньги спишутся с баланса",
            reply_markup=yesno_kb("runrelist"),
        )
    else:
        await cq.message.answer("♻️ Запускаю перевыставление (только бесплатный тариф)…")
        await automation.relist_cycle(cq.from_user.id, manual=True, force_paid_ok=True)
        await cq.message.answer("Готово. См. Журнал", reply_markup=back_kb("auto"))


@router.callback_query(F.data == "runrelist")
async def runrelist_cb(cq: CallbackQuery):
    await cq.answer()
    await cq.message.answer("♻️ Запускаю перевыставление…")
    await automation.relist_cycle(cq.from_user.id, manual=True, force_paid_ok=True)
    await cq.message.answer("Готово. См. Журнал", reply_markup=back_kb("auto"))


@router.callback_query(F.data == "auto_log")
async def auto_log_cb(cq: CallbackQuery):
    await cq.answer()
    rec = await storage.load(cq.from_user.id)
    log = rec["runtime"].get("automation_log", [])
    if not log:
        await cq.message.answer("Журнал пуст", reply_markup=back_kb("auto"))
        return
    lines = ["📜 <b>Журнал автоматизации</b>\n"]
    for e in log[-30:]:
        t = time.strftime("%H:%M", time.localtime(e["at"] / 1000))
        icon = {"error": "❌", "warn": "⚠️"}.get(e.get("kind"), "•")
        lines.append(f"{t} {icon} {esc(e['message'])[:140]}")
    await cq.message.edit_text("\n".join(lines)[:4000], reply_markup=back_kb("auto"))


# ================= Настройки (шаблоны, команды, бэкап) =================

@router.callback_query(F.data == "settings")
async def settings_cb(cq: CallbackQuery):
    await cq.answer()
    text = "🔧 <b>Настройки</b>\n\nВыберите действие:"
    kbd = kb([
        [btn("🎨 Тема эмодзи", "theme")],
        [btn("💾 Резервная копия", "backup")],
        [btn("‹ Меню", "start")],
    ])
    await cq.message.edit_text(text, reply_markup=kbd)


# ---- тема эмодзи ----

@router.callback_query(F.data == "theme")
async def theme_cb(cq: CallbackQuery):
    await cq.answer()
    rec = await storage.load(cq.from_user.id)
    cur = rec["settings"].get("emoji_theme", "green")
    rows = []
    for key, (on, off, name) in EMOJI_THEMES.items():
        mark = "🟢" if key == cur else ""
        rows.append([btn(f"{mark} {name} ({on}/{off})", f"theme_set:{key}")])
    rows.append([btn("‹ Настройки", "settings")])
    await cq.message.edit_text("🎨 <b>Тема эмодзи</b>\nВыберите цветовую тему статусов (вкл/выкл):", reply_markup=kb(rows))


@router.callback_query(F.data.startswith("theme_set:"))
async def theme_set_cb(cq: CallbackQuery):
    key = cq.data.split(":", 1)[1]
    if key not in EMOJI_THEMES:
        await cq.answer()
        return
    await storage.update(cq.from_user.id, lambda r: r["settings"].update(emoji_theme=key))
    await cq.answer("Сохранено ✅")
    await theme_cb(cq)


# ---- Шаблоны ----

@router.callback_query(F.data == "templates")
async def templates_cb(cq: CallbackQuery):
    await cq.answer()
    rec = await storage.load(cq.from_user.id)
    tpls = rec["settings"]["templates"]
    lines = ["📋 <b>Шаблоны</b>\n"]
    rows = []
    for t in tpls:
        lines.append(f"• <b>{esc(t['title'])}</b>: {esc(t['text'][:60])}")
        rows.append([btn(f"🗑 {esc(t['title'])}", f"tpl_del:{t['id']}")])
    if not tpls:
        lines.append("(пусто)")
    rows.append([btn("➕ Добавить", "tpl_add")])
    rows.append([btn("‹ Чаты", "chats")])
    await cq.message.edit_text("\n".join(lines)[:4000], reply_markup=kb(rows))


@router.callback_query(F.data == "tpl_add")
async def tpl_add_cb(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Form.add_template)
    await cq.answer()
    await cq.message.answer(
        "➕ Введите шаблон в формате:\n<code>Название | текст сообщения</code>\n\n"
        "Пример:\n<code>Доставка | Отправим в течение {{time}}</code>"
    )


@router.message(Form.add_template)
async def tpl_add_receive(message: Message, state: FSMContext):
    await state.clear()
    raw = message.text or ""
    if "|" not in raw:
        await message.answer("Неверный формат. Нужно: <code>Название | текст</code>")
        return
    title, text = raw.split("|", 1)
    title = title.strip()
    text = text.strip()
    if not title or not text:
        await message.answer("Название и текст не должны быть пустыми")
        return
    tpl = {"id": f"t{uuid.uuid4().hex[:8]}", "title": title[:40], "text": text[:4000]}
    await storage.update(message.from_user.id, lambda r: r["settings"]["templates"].append(tpl))
    await message.answer(f"✅ Шаблон «{esc(title)}» добавлен", reply_markup=back_kb("templates"))


@router.callback_query(F.data.startswith("tpl_del:"))
async def tpl_del_cb(cq: CallbackQuery):
    tpl_id = cq.data.split(":", 1)[1]
    await storage.update(
        cq.from_user.id,
        lambda r: r["settings"].update(
            templates=[t for t in r["settings"]["templates"] if t["id"] != tpl_id]
        ),
    )
    await cq.answer("Удалён 🗑")
    await templates_cb(cq)


# ---- Отправка шаблона в чат ----

@router.callback_query(F.data.startswith("tpl_send_list:"))
async def tpl_send_list_cb(cq: CallbackQuery):
    chat_id = cq.data.split(":", 1)[1]
    await cq.answer()
    rec = await storage.load(cq.from_user.id)
    tpls = rec["settings"]["templates"]
    rows = [
        [btn(f"📋 {t['title']}", f"tpl_send:{t['id']}:{chat_id}")]
        for t in tpls
    ]
    rows.append([btn("‹ Чат", f"chat:{chat_id}")])
    await cq.message.answer("Выберите шаблон для отправки:", reply_markup=kb(rows))


@router.callback_query(F.data.startswith("tpl_send:"))
async def tpl_send_cb(cq: CallbackQuery):
    _, tpl_id, chat_id = cq.data.split(":", 2)
    await cq.answer()
    rec = await storage.load(cq.from_user.id)
    tpl = next((t for t in rec["settings"]["templates"] if t["id"] == tpl_id), None)
    if not tpl:
        await cq.message.answer("Шаблон не найден")
        return
    client = await _get_client(cq.from_user.id)
    if not client:
        await cq.message.answer("Сначала подключите аккаунт во вкладке «Аккаунты»")
        return
    try:
        viewer = await client.get_viewer()
        chats_data = await client.get_chats(max_pages=5)
        chat = next((c for c in chats_data["chats"] if c["id"] == chat_id), None)
        user = automation.other_participant(chat, viewer) if chat else None
        name = user["username"] if user and user.get("username") else "покупатель"
        text = (tpl["text"] or "").replace("{{username}}", name).replace(
            "{{time}}", time.strftime("%H:%M")
        )
        await client.send_message(chat_id, text)
    except AuthError as exc:
        await cq.message.answer(f"⚠️ {esc(exc)}\nОбновите сессию во вкладке «Аккаунты»")
        return
    except Exception as exc:
        await cq.message.answer(f"⚠️ {esc(exc)}")
        return
    finally:
        await client.close()
    await cq.message.answer("✅ Шаблон отправлен", reply_markup=back_kb(f"chat:{chat_id}"))


# ---- Команды ----

@router.callback_query(F.data == "commands")
async def commands_cb(cq: CallbackQuery):
    await cq.answer()
    rec = await storage.load(cq.from_user.id)
    rules = rec["settings"]["automation"]["rules"]
    on_icon, off_icon = theme_icons(rec["settings"])
    lines = ["💬 <b>Команды</b> (ответ при начале сообщения с команды)\n"]
    rows = []
    for i, r in enumerate(rules):
        on = on_icon if r.get("enabled") is not False else off_icon
        lines.append(f"{on} <b>{esc(r.get('keyword',''))}</b> → {esc(r.get('response','')[:50])}")
        rows.append([
            btn(f"{on} {esc(r.get('keyword',''))}", f"cmd_toggle:{i}"),
        ])
        rows.append([btn("🗑 Удалить", f"cmd_del:{i}")])
    if not rules:
        lines.append("(пусто)")
    rows.append([btn("➕ Добавить", "cmd_add")])
    rows.append([btn("‹ Чаты", "chats")])
    await cq.message.edit_text("\n".join(lines)[:4000], reply_markup=kb(rows))


@router.callback_query(F.data == "cmd_add")
async def cmd_add_cb(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Form.add_command)
    await cq.answer()
    await cq.message.answer(
        "➕ Введите команду в формате:\n<code>ключевое слово | ответ</code>\n\n"
        "Пример:\n<code>как купить | Напишите в чат, и я вышлю инструкцию</code>\n\n"
        "В ответе можно использовать {{username}} и {{time}}"
    )


@router.message(Form.add_command)
async def cmd_add_receive(message: Message, state: FSMContext):
    await state.clear()
    raw = message.text or ""
    if "|" not in raw:
        await message.answer("Неверный формат. Нужно: <code>команда | ответ</code>")
        return
    keyword, response = raw.split("|", 1)
    keyword = keyword.strip()
    response = response.strip()
    if not keyword or not response:
        await message.answer("Команда и ответ не должны быть пустыми")
        return
    await storage.update(
        message.from_user.id,
        lambda r: r["settings"]["automation"]["rules"].append(
            {"keyword": keyword[:80], "response": response[:4000], "enabled": True}
        ),
    )
    await message.answer(f"✅ Команда «{esc(keyword)}» добавлена", reply_markup=back_kb("commands"))


@router.callback_query(F.data.startswith("cmd_del:"))
async def cmd_del_cb(cq: CallbackQuery):
    idx = int(cq.data.split(":", 1)[1])
    await storage.update(
        cq.from_user.id,
        lambda r: (r["settings"]["automation"]["rules"].pop(idx)
                   if idx < len(r["settings"]["automation"]["rules"]) else None),
    )
    await cq.answer("Удалена 🗑")
    await commands_cb(cq)


@router.callback_query(F.data.startswith("cmd_toggle:"))
async def cmd_toggle_cb(cq: CallbackQuery):
    idx = int(cq.data.split(":", 1)[1])
    def mutate(rec):
        rules = rec["settings"]["automation"]["rules"]
        if idx < len(rules):
            rules[idx]["enabled"] = rules[idx].get("enabled") is False
    await storage.update(cq.from_user.id, mutate)
    await cq.answer()
    await commands_cb(cq)


# ---- Параметры (числовые) ----

PARAM_KEYS = {
    "delay_seconds": ("automation", "delay_seconds", "Задержка автоответа, сек (2–60)"),
    "hourly_limit": ("automation", "hourly_limit", "Лимит автоответов в час (1–60)"),
    "poll_seconds": ("automation", "poll_seconds", "Период опроса чатов, сек (10–300)"),
    "max_price_per_item": ("relist", "max_price_per_item", "Макс. сумма за перевыставление, ₽ (0 = без лимита)"),
    "min_balance": ("relist", "min_balance", "Мин. баланс, ₽"),
    "delay_minutes": ("relist", "delay_minutes", "Задержка перевыставления, минут (1–120)"),
}


@router.callback_query(F.data.startswith("set:"))
async def set_cb(cq: CallbackQuery, state: FSMContext):
    _, page, key = cq.data.split(":", 2)
    await cq.answer()
    await state.set_state(Form.set_value)
    await state.update_data(set_key=key, set_page=page)
    if key == "return_text":
        rec = await storage.load(cq.from_user.id)
        cur = rec["settings"]["return_message"]["text"]
        await cq.message.answer(
            "↩️ Текст сообщения при возврате ({{username}}, {{item}}, {{time}}):\n\n"
            f"Текущий:\n<code>{esc(cur)}</code>\n\nОтправьте новый текст:"
        )
        return
    if key not in PARAM_KEYS:
        return
    section, field, desc = PARAM_KEYS[key]
    rec = await storage.load(cq.from_user.id)
    cur = rec["settings"][section][field]
    await cq.message.answer(f"⚙️ {esc(desc)}\nТекущее значение: <b>{esc(cur)}</b>\nОтправьте новое число:")


@router.message(Form.set_value)
async def set_value_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("set_key")
    page = data.get("set_page") or "auto"
    await state.clear()
    raw = (message.text or "").strip()

    if key == "return_text":
        if not raw:
            await message.answer("Текст не может быть пустым")
            return
        await storage.update(
            message.from_user.id,
            lambda r: r["settings"]["return_message"].update(text=raw[:4000]),
        )
        await message.answer("✅ Текст сохранён", reply_markup=back_kb(page))
        return

    if key not in PARAM_KEYS:
        return
    section, field, desc = PARAM_KEYS[key]
    try:
        val = float(raw.replace(",", "."))
    except ValueError:
        await message.answer("Введите число")
        return
    val = int(val)
    limits = {
        "delay_seconds": (2, 60),
        "hourly_limit": (1, 60),
        "poll_seconds": (10, 300),
        "max_price_per_item": (0, 100000),
        "min_balance": (0, 1000000),
        "delay_minutes": (1, 120),
    }
    lo, hi = limits.get(key, (0, 10**9))
    val = max(lo, min(hi, val))
    await storage.update(
        message.from_user.id,
        lambda r: r["settings"][section].update({field: val}),
    )
    await message.answer(f"✅ {esc(desc)}: <b>{val}</b>", reply_markup=back_kb(page))


# ---- Резервная копия ----

@router.callback_query(F.data == "backup")
async def backup_cb(cq: CallbackQuery):
    await cq.answer()
    rec = await storage.load(cq.from_user.id)
    payload = {
        "settings": rec["settings"],
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # cookies не экспортируем
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    await cq.message.answer_document(
        BufferedInputFile(data.encode("utf-8"), filename="playerok-tools-backup.json"),
        caption="💾 Резервная копия настроек (без cookies)",
    )


# ================= Сделки и отзывы =================

def _toggle_list(lst: list, item_id: str) -> None:
    if item_id in lst:
        lst.remove(item_id)
    else:
        lst.append(item_id)


@router.callback_query(F.data == "deals")
async def deals_cb(cq: CallbackQuery):
    await cq.answer()
    rec = await storage.load(cq.from_user.id)
    s = rec["settings"]
    on, off = theme_icons(s)
    dc = s["deal_confirm"]; dm = s["deal_complete"]; rr = s["reviews_reply"]
    n_sel = len(dc.get("items") or [])
    n_excl = len(dm.get("exclude_items") or [])
    text = (
        "🤝 <b>Сделки и отзывы</b>\n\n"
        f"{on if dc['enabled'] else off} <b>Автоподтверждение</b> — товаров: {n_sel or 'все'}\n"
        f"{on if dm['enabled'] else off} <b>Сообщение после выполнения</b> — исключений: {n_excl}\n"
        f"{on if rr['enabled'] else off} <b>Ответ на отзывы</b>\n"
    )
    kbd = kb([
        [btn(f"{on if dc['enabled'] else off} Автоподтверждение", "toggle:deal_confirm")],
        [btn("🛒 Товары для подтверждения", "ci_list")],
        [btn("✉️ Сообщение после подтверждения", "msg:confirm")],
        [btn(f"{on if dm['enabled'] else off} Сообщение после выполнения", "toggle:deal_complete")],
        [btn("🚫 Исключения", "xi_list")],
        [btn("✉️ Сообщение после выполнения", "msg:complete")],
        [btn(f"{on if rr['enabled'] else off} Ответ на отзывы", "toggle:reviews_reply")],
        [btn("⭐ Сообщения по оценкам", "review_msgs")],
        [btn("‹ Меню", "start")],
    ])
    await cq.message.edit_text(text, reply_markup=kbd)


# --- выбор товаров (multi-select) ---

async def render_item_picker(cq: CallbackQuery, mode: str, page: int = 0):
    try:
        data = await fetch_items(cq.from_user.id)
    except AuthError as exc:
        await cq.message.answer(f"⚠️ {esc(exc)}\nОбновите сессию во вкладке «Аккаунты»")
        return
    except Exception as exc:
        await cq.message.answer(f"⚠️ Ошибка загрузки товаров: {esc(exc)}")
        return
    if data is None:
        await cq.message.answer("Сначала подключите аккаунт во вкладке «Аккаунты»")
        return
    items = [i for i in data["items"] if i["status"] in ("APPROVED", "EXPIRED", "DRAFT", "SOLD")]
    rec = await storage.load(cq.from_user.id)
    if mode == "confirm":
        sel = set(rec["settings"]["deal_confirm"].get("items") or [])
        title = "🛒 <b>Автоподтверждение</b> — выберите товары (пусто = все)"
        prefix = "ci"
        done = "ci_done"
    else:
        sel = set(rec["settings"]["deal_complete"].get("exclude_items") or [])
        title = "🚫 <b>Исключения</b> — товары, которым НЕ слать сообщение после выполнения"
        prefix = "xi"
        done = "xi_done"

    if not items:
        await cq.message.edit_text(title + "\n\nТоваров нет", reply_markup=back_kb("deals"))
        return

    per = 15
    total_pages = max(1, (len(items) + per - 1) // per)
    page = max(0, min(page, total_pages - 1))
    chunk = items[page * per:(page + 1) * per]

    on_icon, off_icon = theme_icons(rec["settings"])
    lines = [title, "", f"Выбрано: {len(sel)}\n"]
    rows = []
    for it in chunk:
        mark = on_icon if it["id"] in sel else off_icon
        rows.append([btn(f"{mark} {it['name']}", f"{prefix}:{it['id']}:{page}")])
    if total_pages > 1:
        if page > 0:
            rows.append([btn(f"‹ Страница {page+1}/{total_pages}", f"{prefix}_p:{page-1}")])
        if page < total_pages - 1:
            rows.append([btn("Вперёд ›", f"{prefix}_p:{page+1}")])
    rows.append([btn("✅ Готово", done)])
    await cq.message.edit_text("\n".join(lines)[:4000], reply_markup=kb(rows))


@router.callback_query(F.data == "ci_list")
async def ci_list_cb(cq: CallbackQuery):
    await cq.answer()
    await render_item_picker(cq, "confirm", 0)


@router.callback_query(F.data == "xi_list")
async def xi_list_cb(cq: CallbackQuery):
    await cq.answer()
    await render_item_picker(cq, "exclude", 0)


@router.callback_query(F.data.startswith("ci_p:"))
async def ci_page_cb(cq: CallbackQuery):
    await render_item_picker(cq, "confirm", int(cq.data.split(":")[1]))


@router.callback_query(F.data.startswith("xi_p:"))
async def xi_page_cb(cq: CallbackQuery):
    await render_item_picker(cq, "exclude", int(cq.data.split(":")[1]))


@router.callback_query(F.data.startswith("ci:"))
async def ci_toggle_cb(cq: CallbackQuery):
    _, item_id, page = cq.data.split(":")
    await storage.update(cq.from_user.id, lambda r: _toggle_list(r["settings"]["deal_confirm"].setdefault("items", []), item_id))
    await render_item_picker(cq, "confirm", int(page))


@router.callback_query(F.data.startswith("xi:"))
async def xi_toggle_cb(cq: CallbackQuery):
    _, item_id, page = cq.data.split(":")
    await storage.update(cq.from_user.id, lambda r: _toggle_list(r["settings"]["deal_complete"].setdefault("exclude_items", []), item_id))
    await render_item_picker(cq, "exclude", int(page))


@router.callback_query(F.data == "ci_done")
async def ci_done_cb(cq: CallbackQuery):
    await cq.answer()
    await deals_cb(cq)


@router.callback_query(F.data == "xi_done")
async def xi_done_cb(cq: CallbackQuery):
    await cq.answer()
    await deals_cb(cq)


# --- сообщения по сделкам ---

@router.callback_query(F.data.startswith("msg:"))
async def msg_cb(cq: CallbackQuery, state: FSMContext):
    key = cq.data.split(":", 1)[1]
    await cq.answer()
    await state.set_state(Form.set_deal_message)
    await state.update_data(msg_key=key)
    rec = await storage.load(cq.from_user.id)
    cur = (rec["settings"]["deal_confirm"]["message"] if key == "confirm"
           else rec["settings"]["deal_complete"]["message"])
    await cq.message.answer(
        "✉️ Текст сообщения. Переменные: <code>{{username}}</code>, <code>{{item}}</code>.\n\n"
        f"Текущий:\n<code>{esc(cur)}</code>\n\nОтправьте новый текст:"
    )


@router.message(Form.set_deal_message)
async def msg_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("msg_key")
    await state.clear()
    raw = (message.text or "").strip()
    if key == "confirm":
        await storage.update(message.from_user.id, lambda r: r["settings"]["deal_confirm"].update(message=raw[:4000]))
    else:
        await storage.update(message.from_user.id, lambda r: r["settings"]["deal_complete"].update(message=raw[:4000]))
    await message.answer("✅ Сообщение сохранено", reply_markup=back_kb("deals"))


# --- ответ на отзывы ---

@router.callback_query(F.data == "review_msgs")
async def review_msgs_cb(cq: CallbackQuery):
    await cq.answer()
    rec = await storage.load(cq.from_user.id)
    msgs = rec["settings"]["reviews_reply"].get("messages") or {}
    rows = []
    for r in range(1, 6):
        state_txt = "✅ задано" if msgs.get(str(r)) else "— не задано"
        rows.append([btn(f"⭐ {r} — {state_txt}", f"rev_msg:{r}")])
    rows.append([btn("‹ Сделки", "deals")])
    await cq.message.edit_text("⭐ <b>Ответ на отзывы</b>\nВыберите оценку для настройки:", reply_markup=kb(rows))


@router.callback_query(F.data.startswith("rev_msg:"))
async def rev_msg_cb(cq: CallbackQuery, state: FSMContext):
    rating = cq.data.split(":", 1)[1]
    await cq.answer()
    await state.set_state(Form.set_review_message)
    await state.update_data(rev_rating=rating)
    rec = await storage.load(cq.from_user.id)
    cur = (rec["settings"]["reviews_reply"].get("messages") or {}).get(rating) or ""
    await cq.message.answer(
        f"⭐ Текст ответа на отзыв с оценкой <b>{rating}</b>. "
        f"Переменные: <code>{{{{username}}}}</code>, <code>{{{{rating}}}}</code>.\n\n"
        f"Текущий:\n<code>{esc(cur) or '—'}</code>\n\n"
        f"Отправьте новый текст (или слово <code>удалить</code>):"
    )


@router.message(Form.set_review_message)
async def rev_msg_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    rating = data.get("rev_rating")
    await state.clear()
    raw = (message.text or "").strip()

    def mutate(r):
        m = r["settings"]["reviews_reply"].setdefault("messages", {})
        if raw.lower() in ("удалить", "delete", "del"):
            m.pop(rating, None)
        else:
            m[rating] = raw[:4000]

    await storage.update(message.from_user.id, mutate)
    await message.answer("✅ Сообщение сохранено", reply_markup=back_kb("review_msgs"))


# ================= Админ-панель (владелец) =================

@router.callback_query(F.data == "admin")
async def admin_cb(cq: CallbackQuery):
    if not await storage.is_owner(cq.from_user.id):
        await cq.answer("⛔ Только для владельца", show_alert=True)
        return
    await cq.answer()
    await render_admin(cq.message, edit=True)


async def render_admin(message: Message, edit: bool):
    data = await storage.load_admin()
    users = data["users"]
    active = sum(1 for u in users.values() if u.get("status") == "active")
    owner_id = data["owner_id"]

    lines = [f"👑 <b>Админ-панель</b>\n", f"Владелец: <code>{owner_id}</code>", f"Пользователей: {len(users)} · активных: {active}\n"]

    kbd = kb([
        [btn("➕ Выдать доступ", "grant_start")],
        [btn("🚫 Забрать доступ", "revoke_list")],
        [btn("👥 Список пользователей", "user_list")],
        [btn("💰 Цены подписок", "prices")],
        [btn("🎟 Промокоды", "promos")],
        [btn("🧾 Последние платежи", "payments")],
        [btn("‹ Меню", "start")],
    ])
    text = "\n".join(lines)
    if edit:
        await message.edit_text(text, reply_markup=kbd)
    else:
        await message.answer(text, reply_markup=kbd)


# ---- выдача доступа ----

def split_ref_duration(text: str):
    """Разделяет '@user 7d' на ('@user', '7d')."""
    parts = (text or "").split()
    ref = parts[0] if parts else ""
    dur = " ".join(parts[1:]) if len(parts) > 1 else ""
    return ref, dur


def grant_cancel_kb():
    return kb([[btn("❌ Отменить", "grant_cancel")]])


GRANT_BAD_INPUT = "Неправильные данные. Отправьте <b>@username</b> или ID"


async def _resolve_and_grant(message: Message, state: FSMContext, ref: str, dur_text: str):
    await state.clear()
    try:
        seconds, ok = storage.parse_duration(dur_text)
        if dur_text and not ok:
            await message.answer("Неверный формат", reply_markup=grant_cancel_kb())
            return
        if not ref:
            await message.answer(GRANT_BAD_INPUT, reply_markup=grant_cancel_kb())
            return
        user_id, err = await storage.resolve_user(ref)
        if user_id is None:
            await message.answer(GRANT_BAD_INPUT, reply_markup=grant_cancel_kb())
            return
        owner_id = (await storage.load_admin())["owner_id"]
        if user_id == int(owner_id):
            await message.answer("Это владелец — ему и так всё доступно")
            return
        until = int(time.time()) + seconds if seconds is not None else None
        await storage.grant_access(user_id, until, message.from_user.id)
        uname = f"@{ref[1:]}" if ref.startswith("@") else f"ID {user_id}"
        await message.answer(f"✅ Доступ выдан пользователю {esc(uname)} на {storage.duration_human(seconds)}")
        try:
            await bot.send_message(
                user_id,
                f"✅ Вам выдан доступ к Playerok Tools (срок: {storage.duration_human(seconds)})!\n"
                f"Подключите аккаунт во вкладке «Аккаунты»",
            )
        except Exception:
            pass
    except Exception as exc:
        await message.answer(f"❌ Ошибка при выдаче доступа: {esc(exc)}")


async def _handle_grant_input(message: Message, state: FSMContext, text: str):
    """Обрабатывает ввод: либо 'ID время' (выдаём сразу), либо 'ID' (спрашиваем срок)."""
    ref, dur = split_ref_duration(text)
    if not ref:
        await message.answer(GRANT_BAD_INPUT, reply_markup=grant_cancel_kb())
        return
    user_id, err = await storage.resolve_user(ref)
    if user_id is None:
        await message.answer(GRANT_BAD_INPUT, reply_markup=grant_cancel_kb())
        return
    if dur:
        await _resolve_and_grant(message, state, ref, dur)
    else:
        await state.set_state(Form.grant_duration)
        await state.update_data(grant_ref=ref)
        await message.answer(
            "Укажите срок",
            reply_markup=grant_cancel_kb(),
        )


@router.callback_query(F.data == "grant_cancel")
async def grant_cancel_cb(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.answer("Отменено")
    if await storage.is_owner(cq.from_user.id):
        await cq.message.edit_text("📋 Меню", reply_markup=owner_menu_kb())
    else:
        await cq.message.edit_text("📋 Меню", reply_markup=menu_kb())


@router.callback_query(F.data == "grant_start")
async def grant_start_cb(cq: CallbackQuery, state: FSMContext):
    if not await storage.is_owner(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    await cq.answer()
    await state.set_state(Form.grant)
    await cq.message.answer(
        "➕ Отправьте <b>@username</b> или <b>ID</b>",
        reply_markup=grant_cancel_kb(),
    )


@router.message(Form.grant)
async def _grant_receive(message: Message, state: FSMContext):
    await _handle_grant_input(message, state, (message.text or "").strip())


@router.message(Form.grant_duration)
async def _grant_duration_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    ref = data.get("grant_ref", "")
    dur = (message.text or "").strip()
    await _resolve_and_grant(message, state, ref, dur)


# ---- отзыв доступа ----

@router.callback_query(F.data == "revoke_list")
async def revoke_list_cb(cq: CallbackQuery):
    if not await storage.is_owner(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    await cq.answer()
    await render_revoke_list(cq.message, edit=True)


async def render_revoke_list(message: Message, edit: bool):
    data = await storage.load_admin()
    users = data["users"]
    rows = []
    lines = ["🚫 <b>Отозвать доступ</b>\n"]
    for uid, u in users.items():
        if u.get("status") in ("active",) :
            name = data["known"].get(uid) or ""
            label = f"@{name}" if name else f"ID {uid}"
            rows.append([btn(f"🚫 {label}", f"revoke:{uid}")])
    if not rows:
        lines.append("Активных пользователей нет")
    rows.append([btn("‹ Админ", "admin")])
    text = "\n".join(lines)
    if edit:
        await message.edit_text(text, reply_markup=kb(rows))
    else:
        await message.answer(text, reply_markup=kb(rows))


@router.callback_query(F.data.startswith("revoke:"))
async def revoke_one_cb(cq: CallbackQuery):
    if not await storage.is_owner(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    uid = cq.data.split(":", 1)[1]
    await storage.revoke_access(int(uid))
    await cq.answer("Доступ отозван 🚫")
    await render_revoke_list(cq.message, edit=True)


# ---- список пользователей ----

@router.callback_query(F.data == "user_list")
async def user_list_cb(cq: CallbackQuery):
    if not await storage.is_owner(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    await cq.answer()
    data = await storage.load_admin()
    users = data["users"]
    now = int(time.time())
    lines = ["👥 <b>Активные пользователи</b>\n"]
    count = 0
    for uid, u in users.items():
        if u.get("status") != "active":
            continue
        until = u.get("until")
        # без срока (навсегда) либо ещё не истёк
        if until and int(until) <= now:
            continue
        count += 1
        name = data["known"].get(uid) or ""
        label = f"@{name}" if name else f"ID {uid}"
        if until:
            left = max(0, int(until) - now)
            extra = f" — осталось {storage.duration_human(left)}"
        else:
            extra = " — навсегда"
        lines.append(f"✅ <b>{esc(label)}</b>{extra}")
    if count == 0:
        lines.append("Активных пользователей нет")
    await cq.message.edit_text("\n".join(lines)[:4000], reply_markup=back_kb("admin"))


# ---- цены ----

@router.callback_query(F.data == "prices")
async def prices_cb(cq: CallbackQuery):
    if not await storage.is_owner(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    await cq.answer()
    await render_prices(cq.message, edit=True)


async def render_prices(message: Message, edit: bool):
    data = await storage.load_admin()
    durations = data["prices"]["durations"]
    lines = ["💰 <b>Цены подписок</b>\n"]
    rows = []
    for d in durations:
        lines.append(f"• <b>{esc(d['label'])}</b> — {d['stars']} ⭐")
        rows.append([btn(f"✏️ {d['label']} — {d['stars']} ⭐", f"price_set:{d['key']}")])
    rows.append([btn("‹ Админ", "admin")])
    kbd = kb(rows)
    if edit:
        await message.edit_text("\n".join(lines), reply_markup=kbd)
    else:
        await message.answer("\n".join(lines), reply_markup=kbd)


@router.callback_query(F.data.startswith("price_set:"))
async def price_set_cb(cq: CallbackQuery, state: FSMContext):
    if not await storage.is_owner(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    key = cq.data.split(":", 1)[1]
    d = await storage.get_duration(key)
    if not d:
        await cq.answer()
        return
    await cq.answer()
    await state.set_state(Form.set_price)
    await state.update_data(price_key=key)
    await cq.message.answer(
        f"✏️ <b>{esc(d['label'])}</b>\nТекущая цена: <b>{d['stars']} ⭐</b>\n"
        f"Отправьте новую цену в звёздах (число):"
    )


@router.message(Form.set_price)
async def price_receive(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("price_key")
    await state.clear()
    raw = (message.text or "").strip()
    try:
        val = int(raw)
    except ValueError:
        await message.answer("Введите число")
        return
    val = max(1, min(val, 10**7))
    await storage.set_duration_price(key, val)
    await message.answer(f"✅ Цена обновлена: {val} ⭐")
    await render_prices(message, edit=False)


# ---- промокоды ----

@router.callback_query(F.data == "promos")
async def promos_cb(cq: CallbackQuery):
    if not await storage.is_owner(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    await cq.answer()
    data = await storage.load_admin()
    promos = data.get("promos", {})
    lines = ["🎟 <b>Промокоды</b>\n"]
    rows = []
    for code, p in promos.items():
        used = int(p.get("used", 0))
        max_uses = int(p.get("max_uses", 0))
        if max_uses > 0:
            uses_txt = f" · использовано {used}/{max_uses}"
        else:
            uses_txt = f" · использовано {used} (без лимита)"
        dur = str(p.get("durations", "all"))
        dur_txt = " · все сроки" if dur == "all" else f" · только {dur}"
        lines.append(f"• <b>{esc(code)}</b> — скидка {p['discount']}%{uses_txt}{dur_txt}")
        rows.append([btn(f"🗑 {code} (−{p['discount']}%)", f"promo_del:{code}")])
    if not promos:
        lines.append("(пусто)")
    rows.append([btn("➕ Добавить", "promo_add_start")])
    rows.append([btn("‹ Админ", "admin")])
    await cq.message.edit_text("\n".join(lines)[:4000], reply_markup=kb(rows))


@router.callback_query(F.data == "promo_add_start")
async def promo_add_start_cb(cq: CallbackQuery, state: FSMContext):
    if not await storage.is_owner(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    await cq.answer()
    await state.set_state(Form.promo_add)
    await cq.message.answer(
        "➕ Введите промокод в формате:\n<code>КОД скидка [кол-во] [длительность]</code>\n\n"
        "Длительность: <code>all</code>, <code>1d</code>, <code>7d</code>, <code>30d</code>, <code>180d</code> (по умолчанию all)\n\n"
        "Примеры:\n"
        "<code>SKIDKA 20</code> — скидка 20%, без лимита, на все сроки\n"
        "<code>SKIDKA 20 10</code> — скидка 20%, максимум 10 использований\n"
        "<code>SKIDKA 20 10 7d</code> — скидка 20%, 10 использований, только на неделю"
    )


@router.message(Form.promo_add)
async def promo_add_receive(message: Message, state: FSMContext):
    await state.clear()
    raw = (message.text or "").strip()
    parts = raw.split()
    if len(parts) < 2:
        await message.answer("Неверный формат. Нужно: <code>КОД скидка [кол-во] [длительность]</code>")
        return
    code = parts[0]
    try:
        discount = int(parts[1])
    except ValueError:
        await message.answer("Скидка должна быть числом (процент)")
        return
    max_uses = 0
    durations = "all"
    rest = parts[2:]
    if rest:
        # кол-во использований — первое число; длительность — последний токен (если не число)
        if rest[0].isdigit():
            max_uses = max(0, int(rest[0]))
            rest = rest[1:]
        if rest:
            durations = rest[0].lower()
    if durations not in ("all", "1d", "7d", "30d", "180d"):
        await message.answer("Длительность должна быть: <code>all</code>, <code>1d</code>, <code>7d</code>, <code>30d</code>, <code>180d</code>")
        return
    if not code.isalnum():
        await message.answer("Код должен состоять из латинских букв и цифр")
        return
    discount = max(1, min(discount, 100))
    await storage.add_promo(code, discount, max_uses, durations)
    lim = f", максимум {max_uses} использований" if max_uses > 0 else ", без лимита"
    if durations == "all":
        dur_txt = ", все сроки"
    else:
        dur_txt = f", только {durations}"
    await message.answer(
        f"✅ Промокод <b>{esc(code)}</b> (−{discount}%{lim}{dur_txt}) добавлен",
        reply_markup=back_kb("promos"),
    )


@router.callback_query(F.data.startswith("promo_del:"))
async def promo_del_cb(cq: CallbackQuery):
    if not await storage.is_owner(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    code = cq.data.split(":", 1)[1]
    await storage.delete_promo(code)
    await cq.answer("Удалён 🗑")
    await promos_cb(cq)


# ---- платежи ----

@router.callback_query(F.data == "payments")
async def payments_cb(cq: CallbackQuery):
    if not await storage.is_owner(cq.from_user.id):
        await cq.answer("⛔", show_alert=True)
        return
    await cq.answer()
    data = await storage.load_admin()
    pays = data.get("payments", [])
    if not pays:
        await cq.message.edit_text("🧾 Платежей пока нет", reply_markup=back_kb("admin"))
        return
    lines = ["🧾 <b>Последние платежи</b>\n"]
    for p in pays[-15:]:
        kind = "⭐" if p["kind"] == "stars" else "💳"
        t = time.strftime("%d.%m %H:%M", time.localtime(p["ts"]))
        name = data["known"].get(str(p["user_id"])) or f"ID {p['user_id']}"
        lines.append(f"{t} {kind} <b>{esc(name)}</b> — {p['amount']} · {p['status']}")
    await cq.message.edit_text("\n".join(lines)[:4000], reply_markup=back_kb("admin"))


# ================= Покупка доступа =================

def _apply_discount(stars: int, discount) -> int:
    if not discount:
        return int(stars)
    return max(1, round(int(stars) * (100 - int(discount)) / 100))


async def _render_buy(message: Message, edit: bool, promo_code: str = "", promo_discount: int = 0, promo_durations: str = "all"):
    data = await storage.load_admin()
    durations = data["prices"]["durations"]
    lines = ["⭐ <b>Покупка доступа</b>\n\nВыберите срок подписки:\n"]
    if promo_code:
        lines.append(f"🎟 Промокод <b>{esc(promo_code)}</b> (−{promo_discount}%) применён\n")
    # если промокод ограничен конкретной длительностью — показываем только её
    if promo_code and promo_durations != "all":
        durations = [d for d in durations if d["key"] == promo_durations]
    rows = []
    for d in durations:
        stars = _apply_discount(d["stars"], promo_discount)
        if promo_code and stars != d["stars"]:
            label = f"{d['label']} — {stars} ⭐ (−{promo_discount}%)"
        else:
            label = f"{d['label']} — {stars} ⭐"
        lines.append(f"• <b>{esc(d['label'])}</b> — {stars} ⭐")
        rows.append([btn(label, f"buy_dur:{d['key']}")])
    rows.append([btn("🎟 Промокод", "promo_enter")])
    rows.append([btn("❌ Отменить", "buy_cancel")])
    text = "\n".join(lines)
    kbd = kb(rows)
    if edit:
        await message.edit_text(text, reply_markup=kbd)
    else:
        await message.answer(text, reply_markup=kbd)


@router.callback_query(F.data == "buy")
async def buy_cb(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    data = await state.get_data()
    await _render_buy(
        cq.message, edit=True,
        promo_code=data.get("promo_code", ""),
        promo_discount=data.get("promo_discount", 0),
        promo_durations=data.get("promo_durations", "all"),
    )


@router.callback_query(F.data == "buy_cancel")
async def buy_cancel_cb(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await cq.answer("Отменено")
    if await storage.is_owner(cq.from_user.id):
        await cq.message.edit_text("📋 Меню", reply_markup=owner_menu_kb())
    elif await storage.is_allowed(cq.from_user.id):
        await cq.message.edit_text("📋 Меню", reply_markup=menu_kb())
    else:
        await cq.message.edit_text(
            "👋 <b>Playerok Tools</b> — бот-помощник продавца Playerok\n\n"
            "Бот <b>платный</b>. Чтобы получить доступ:\n"
            "Оплата звёздами Telegram\n\n"
            "После оплаты откроются дашборд, чаты, лоты, авто-поднятие, "
            "перевыставление, команды, сделки и возвраты\n\n"
            "👮 Используя бот, вы принимаете <a href=\"" + POLICY_URL + "\">политику конфиденциальности</a>",
            reply_markup=kb([
                [btn("⭐ Купить доступ", "buy")],
                [url_btn("👮 Политика конфиденциальности", POLICY_URL)],
            ]),
            disable_web_page_preview=True,
        )


# ---- промокод ----

@router.callback_query(F.data == "promo_enter")
async def promo_enter_cb(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await state.set_state(Form.promo)
    await cq.message.answer("🎟 Введите промокод:")


@router.message(Form.promo)
async def promo_receive(message: Message, state: FSMContext):
    code = (message.text or "").strip()
    promo = await storage.get_promo(code)
    if not promo:
        await state.set_state(None)
        await message.answer(
            "Промокод не найден",
            reply_markup=kb([[btn("‹ К покупке", "buy")]]),
        )
        return
    used = int(promo.get("used", 0))
    max_uses = int(promo.get("max_uses", 0))
    if max_uses > 0 and used >= max_uses:
        await state.set_state(None)
        await message.answer(
            "Промокод больше недействителен (лимит использований исчерпан)",
            reply_markup=kb([[btn("‹ К покупке", "buy")]]),
        )
        return
    durations = str(promo.get("durations", "all"))
    await state.update_data(
        promo_code=code,
        promo_discount=promo["discount"],
        promo_durations=durations,
    )
    await state.set_state(None)
    await _render_buy(
        message, edit=False,
        promo_code=code, promo_discount=promo["discount"], promo_durations=durations,
    )


# ---- оплата звёздами ----

@router.callback_query(F.data.startswith("buy_dur:"))
async def buy_dur_cb(cq: CallbackQuery, state: FSMContext):
    key = cq.data.split(":", 1)[1]
    d = await storage.get_duration(key)
    if not d:
        await cq.answer()
        return
    data = await state.get_data()
    discount = data.get("promo_discount", 0)
    promo_code = data.get("promo_code", "")
    promo_durations = data.get("promo_durations", "all")
    # проверка лимита использований перед отправкой инвойса
    if promo_code:
        promo = await storage.get_promo(promo_code)
        if not promo:
            await cq.answer("Промокод не найден", show_alert=True)
            return
        if promo_durations != "all" and promo_durations != key:
            await cq.answer("Промокод не действует на этот срок", show_alert=True)
            return
        used = int(promo.get("used", 0))
        max_uses = int(promo.get("max_uses", 0))
        if max_uses > 0 and used >= max_uses:
            await cq.answer("Лимит использований промокода исчерпан", show_alert=True)
            return
    stars = _apply_discount(d["stars"], discount)
    price = LabeledPrice(label="Доступ к Playerok Tools", amount=stars)
    payload = f"sub:{key}"
    if promo_code:
        payload += f":{promo_code}"
    await bot.send_invoice(
        chat_id=cq.from_user.id,
        title="Playerok Tools — доступ",
        description=f"Подписка: {d['label']}",
        payload=payload,
        currency="XTR",
        prices=[price],
    )
    await cq.answer()


@router.pre_checkout_query()
async def pre_checkout(pcq: PreCheckoutQuery):
    await pcq.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message):
    amount = message.successful_payment.total_amount
    user_id = message.from_user.id
    payload = message.successful_payment.invoice_payload or ""
    parts = payload.split(":")
    key = parts[1] if len(parts) > 1 and parts[0] == "sub" else ""
    promo_code = parts[2] if len(parts) > 2 else ""
    d = await storage.get_duration(key)
    days = d["days"] if d else None
    until = int(time.time()) + days * 86400 if days else None
    await storage.grant_access(user_id, until, None)
    if promo_code:
        await storage.consume_promo(promo_code)
    await storage.log_payment(user_id, "stars", amount, "paid")
    dur_text = storage.duration_human(days * 86400) if days else "навсегда"
    await message.answer(
        f"✅ Оплата получена! Доступ активирован на {dur_text}.\n\n"
        "Подключите аккаунт Playerok во вкладке «Аккаунты»",
        reply_markup=menu_kb(),
    )
    owner_id = (await storage.load_admin())["owner_id"]
    name = message.from_user.username or f"ID {user_id}"
    promo_note = f" (промокод {promo_code})" if promo_code else ""
    try:
        await bot.send_message(owner_id, f"💰 Оплата звёздами: {amount} ⭐ от @{name} (срок {dur_text}{promo_note})")
    except Exception:
        pass


# ================= меню =================

@router.callback_query(F.data == "start")
async def start_cb(cq: CallbackQuery):
    await cq.answer()
    if await storage.is_owner(cq.from_user.id):
        await cq.message.edit_text("📋 Меню", reply_markup=owner_menu_kb())
    else:
        await cq.message.edit_text("📋 Меню", reply_markup=menu_kb())


# ================= запуск =================

# Кнопка меню у поля ввода (MenuButtonCommands) + список команд при вводе «/».
MAIN_COMMANDS = [
    BotCommand(command="start", description="Меню"),
    BotCommand(command="policy", description="Политика конфиденциальности"),
    BotCommand(command="bug", description="Заявка на вознаграждение за баг"),
]

OWNER_COMMANDS = MAIN_COMMANDS


async def setup_bot_ui():
    """Устанавливает кнопку меню (по умолчанию) и команды для всех чатов."""
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    await bot.set_my_commands(MAIN_COMMANDS)

    # владельцу — расширенный список команд
    try:
        await bot.set_my_commands(
            OWNER_COMMANDS,
            scope=BotCommandScopeChat(chat_id=config.OWNER_ID),
        )
    except Exception:
        pass


@dp.errors()
async def on_error(event, exception):
    logging.exception("Ошибка при обработке обновления", exc_info=exception)
    return True


async def main():
    global bot
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot = make_bot()

    dp.message.middleware(AccessMiddleware())
    dp.callback_query.middleware(AccessMiddleware())
    dp.include_router(router)

    await setup_bot_ui()

    # notifier для автоматизации
    async def notifier(user_id: int, text: str):
        try:
            await bot.send_message(user_id, text)
        except Exception:
            pass

    automation.set_notifier(notifier)
    asyncio.create_task(automation.scheduler_loop())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
