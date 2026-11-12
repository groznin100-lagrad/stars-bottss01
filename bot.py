import asyncio
import html
import json
import logging
import math
import os
import re
import sys
import time

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from i18n import LANG_NAMES, t

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------- Настройки (переменные окружения на Render) ----------
def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


BOT_TOKEN = env("BOT_TOKEN")
ADMIN_ID_RAW = env("ADMIN_ID")
ADMIN_USERNAME = env("ADMIN_USERNAME").lstrip("@").lower()
PRICE_PER_STAR = env("PRICE_PER_STAR", "1.7")
CRYPTO_PAY_TOKEN = env("CRYPTO_PAY_TOKEN")
CRYPTO_FIAT = env("CRYPTO_FIAT", "RUB")
MANUAL_REQUISITES = env("MANUAL_REQUISITES")
PUBLIC_URL = env("PUBLIC_URL")
PORT = int(env("PORT", "8080"))
DATA_FILE = env("DATA_FILE", "data.json")
LANGS = [x for x in env("LANGUAGES", "ru,en").lower().replace(" ", "").split(",") if x in LANG_NAMES] or ["ru"]

MIN_STARS, MAX_STARS = 50, 10000
PACKS = [50, 100, 250, 500, 1000, 2500, 5000]

problems = []
if not BOT_TOKEN:
    problems.append("BOT_TOKEN (токен от @BotFather)")
try:
    ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else 0
except ValueError:
    ADMIN_ID = 0
    problems.append("ADMIN_ID должен быть числом (ваш Id из @userinfobot)")
if not ADMIN_ID and not ADMIN_USERNAME:
    problems.append("ADMIN_ID (ваш Telegram ID) или ADMIN_USERNAME")
try:
    PRICE = float(PRICE_PER_STAR.replace(",", "."))
except ValueError:
    PRICE = 0
if PRICE <= 0:
    problems.append("PRICE_PER_STAR должен быть числом, например 1.7")
if problems:
    print("ОШИБКА: не заданы переменные окружения на Render (Environment):", file=sys.stderr)
    for p in problems:
        print(" - " + p, file=sys.stderr)
    sys.exit(1)

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---------- Хранилище (файл; на бесплатном Render стирается при перезапуске) ----------
state = {"counter": 0, "orders": {}, "langs": {}, "admin_chat": None}
pending = {}   # user_id -> что бот ждёт от пользователя
drafts = {}    # user_id -> {"stars":..., "recipient":...}


def load_state():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            state.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception:
        logging.exception("Не удалось прочитать %s", DATA_FILE)


def save_state():
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except Exception:
        logging.exception("Не удалось сохранить %s", DATA_FILE)


def lang_of(uid: int) -> str:
    return state["langs"].get(str(uid)) or LANGS[0]


def tr(uid: int, key: str, **kw) -> str:
    return t(lang_of(uid), key, **kw)


def get_order(oid: int):
    return state["orders"].get(str(oid))


def admin_chat():
    return ADMIN_ID or state.get("admin_chat")


def is_admin(user) -> bool:
    if ADMIN_ID:
        return user.id == ADMIN_ID
    return bool(ADMIN_USERNAME) and (user.username or "").lower() == ADMIN_USERNAME


def rub(stars: int) -> int:
    return math.ceil(round(stars * PRICE, 2))


def price_str() -> str:
    return f"{PRICE:g}"


def kb(rows) -> InlineKeyboardMarkup:
    """rows: список строк, в строке кнопки (текст, callback_data) или (текст, None, url)."""
    out = []
    for row in rows:
        line = []
        for b in row:
            if len(b) == 3:
                line.append(InlineKeyboardButton(text=b[0], url=b[2]))
            else:
                line.append(InlineKeyboardButton(text=b[0], callback_data=b[1]))
        out.append(line)
    return InlineKeyboardMarkup(inline_keyboard=out)


async def render(ev, text: str, markup=None):
    """Для кнопки редактирует сообщение, для обычного сообщения отправляет новое."""
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=markup)
            return
        except Exception:
            await ev.message.answer(text, reply_markup=markup)
    else:
        await ev.answer(text, reply_markup=markup)


def has_methods() -> bool:
    return bool(CRYPTO_PAY_TOKEN or MANUAL_REQUISITES)


# ---------- Crypto Pay ----------
async def crypto_api(method: str, payload=None):
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"https://pay.crypt.bot/api/{method}",
            json=payload or {},
            headers={"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as r:
            data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error")))
    return data["result"]


def items_of(result):
    return result.get("items", []) if isinstance(result, dict) else result


_me = {}


async def bot_username() -> str:
    if "u" not in _me:
        _me["u"] = (await bot.get_me()).username
    return _me["u"]


# ---------- Экраны ----------
async def show_menu(ev, uid: int):
    rows = [[(tr(uid, "btn_buy"), "buy")]]
    if len(LANGS) > 1:
        rows.append([(tr(uid, "btn_lang"), "lang")])
    await render(ev, tr(uid, "menu", price=price_str()), kb(rows))


def lang_kb():
    return kb([[(LANG_NAMES[code], f"setlang:{code}")] for code in LANGS])


async def show_amounts(ev, uid: int):
    rows, row = [], []
    for n in PACKS:
        row.append((f"{n} ⭐ · {rub(n)} ₽", f"amt:{n}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([(tr(uid, "btn_custom"), "amt:custom")])
    rows.append([(tr(uid, "btn_menu"), "menu")])
    await render(ev, tr(uid, "choose_amount", min=MIN_STARS, max=MAX_STARS, price=price_str()), kb(rows))


async def show_recipient_choice(ev, uid: int):
    d = drafts[uid]
    await render(ev, tr(uid, "choose_recipient", stars=d["stars"]), kb([
        [(tr(uid, "btn_self"), "rcp:self"), (tr(uid, "btn_gift"), "rcp:gift")],
        [(tr(uid, "btn_back"), "buy")],
    ]))


async def show_methods(ev, uid: int):
    d = drafts[uid]
    if not has_methods():
        await render(ev, tr(uid, "no_methods"), kb([[(tr(uid, "btn_menu"), "menu")]]))
        return
    rows = []
    if CRYPTO_PAY_TOKEN:
        rows.append([(tr(uid, "btn_crypto"), "pay:crypto")])
    if MANUAL_REQUISITES:
        rows.append([(tr(uid, "btn_sbp"), "pay:sbp")])
    rows.append([(tr(uid, "btn_back"), "buy")])
    await render(ev, tr(uid, "choose_method", stars=d["stars"], recipient=d["recipient"], rub=rub(d["stars"])), kb(rows))


# ---------- Покупатель ----------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    uid = m.from_user.id
    pending.pop(uid, None)
    drafts.pop(uid, None)
    if not ADMIN_ID and is_admin(m.from_user):
        state["admin_chat"] = m.chat.id
        save_state()
        await m.answer("Вы назначены администратором: заказы и чеки будут приходить сюда.")
    if str(uid) not in state["langs"]:
        if len(LANGS) > 1:
            await m.answer(t("ru", "choose_lang"), reply_markup=lang_kb())
            return
        state["langs"][str(uid)] = LANGS[0]
        save_state()
    await show_menu(m, uid)


@dp.callback_query(F.data == "lang")
async def cb_lang(c: CallbackQuery):
    await render(c, t("ru", "choose_lang"), lang_kb())
    await c.answer()


@dp.callback_query(F.data.startswith("setlang:"))
async def cb_setlang(c: CallbackQuery):
    code = c.data.split(":")[1]
    if code in LANGS:
        state["langs"][str(c.from_user.id)] = code
        save_state()
    await show_menu(c, c.from_user.id)
    await c.answer(tr(c.from_user.id, "lang_set"))


@dp.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    pending.pop(c.from_user.id, None)
    await show_menu(c, c.from_user.id)
    await c.answer()


@dp.callback_query(F.data == "buy")
async def cb_buy(c: CallbackQuery):
    pending.pop(c.from_user.id, None)
    await show_amounts(c, c.from_user.id)
    await c.answer()


@dp.callback_query(F.data.startswith("amt:"))
async def cb_amount(c: CallbackQuery):
    uid = c.from_user.id
    val = c.data.split(":")[1]
    if val == "custom":
        pending[uid] = {"t": "amount"}
        await render(c, tr(uid, "ask_custom", min=MIN_STARS, max=MAX_STARS))
        await c.answer()
        return
    n = int(val)
    if not (MIN_STARS <= n <= MAX_STARS):
        await c.answer()
        return
    drafts[uid] = {"stars": n}
    await show_recipient_choice(c, uid)
    await c.answer()


@dp.callback_query(F.data.startswith("rcp:"))
async def cb_recipient(c: CallbackQuery):
    uid = c.from_user.id
    if uid not in drafts:
        await c.answer(tr(uid, "draft_missing"), show_alert=True)
        return
    if c.data.endswith("self") and c.from_user.username:
        drafts[uid]["recipient"] = "@" + c.from_user.username
        await show_methods(c, uid)
    elif c.data.endswith("self"):
        pending[uid] = {"t": "recipient"}
        await render(c, tr(uid, "ask_username_self"))
    else:
        pending[uid] = {"t": "recipient"}
        await render(c, tr(uid, "ask_username_gift"))
    await c.answer()


def new_order(user, d: dict, method: str) -> dict:
    state["counter"] += 1
    oid = state["counter"]
    o = {
        "id": oid,
        "user_id": user.id,
        "username": user.username,
        "name": user.full_name,
        "stars": d["stars"],
        "recipient": d["recipient"],
        "rub": rub(d["stars"]),
        "method": method,
        "status": "new",
        "ext_id": None,
        "lang": lang_of(user.id),
        "created": int(time.time()),
    }
    state["orders"][str(oid)] = o
    save_state()
    return o


@dp.callback_query(F.data.startswith("pay:"))
async def cb_pay(c: CallbackQuery):
    uid = c.from_user.id
    d = drafts.get(uid)
    if not d or "recipient" not in d:
        await c.answer(tr(uid, "draft_missing"), show_alert=True)
        return
    method = c.data.split(":")[1]

    if method == "crypto" and CRYPTO_PAY_TOKEN:
        o = new_order(c.from_user, d, "crypto")
        try:
            inv = await crypto_api("createInvoice", {
                "currency_type": "fiat",
                "fiat": CRYPTO_FIAT,
                "amount": f"{o['rub']:.2f}",
                "description": f"Order {o['id']}: {o['stars']} Telegram Stars",
                "payload": str(o["id"]),
                "paid_btn_name": "openBot",
                "paid_btn_url": f"https://t.me/{await bot_username()}",
                "allow_comments": False,
                "expires_in": 1800,
            })
        except Exception:
            logging.exception("createInvoice failed")
            state["orders"].pop(str(o["id"]), None)
            save_state()
            await c.answer(tr(uid, "crypto_error"), show_alert=True)
            return
        o["ext_id"] = inv["invoice_id"]
        o["status"] = "await_crypto"
        save_state()
        await render(c, tr(uid, "crypto_invoice", id=o["id"], stars=o["stars"], recipient=o["recipient"], rub=o["rub"]), kb([
            [(tr(uid, "btn_pay_crypto"), None, inv["bot_invoice_url"])],
            [(tr(uid, "btn_check"), f"chk:{o['id']}")],
        ]))
        drafts.pop(uid, None)
        await c.answer()
        return

    if method == "sbp" and MANUAL_REQUISITES:
        o = new_order(c.from_user, d, "sbp")
        o["status"] = "await_paid_click"
        save_state()
        await render(c, tr(uid, "sbp_instruction", id=o["id"], stars=o["stars"], recipient=o["recipient"],
                           rub=o["rub"], requisites=MANUAL_REQUISITES), kb([
            [(tr(uid, "btn_paid"), f"paid:{o['id']}")],
            [(tr(uid, "btn_cancel"), f"cancel:{o['id']}")],
        ]))
        drafts.pop(uid, None)
        await c.answer()
        return

    await c.answer(tr(uid, "no_methods"), show_alert=True)


@dp.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(c: CallbackQuery):
    uid = c.from_user.id
    o = get_order(int(c.data.split(":")[1]))
    if o and o["user_id"] == uid and o["status"] in ("new", "await_paid_click"):
        o["status"] = "cancelled"
        save_state()
    pending.pop(uid, None)
    await render(c, tr(uid, "cancelled"))
    await c.answer()


@dp.callback_query(F.data.startswith("paid:"))
async def cb_paid(c: CallbackQuery):
    uid = c.from_user.id
    o = get_order(int(c.data.split(":")[1]))
    if not o or o["user_id"] != uid:
        await c.answer(tr(uid, "order_missing"), show_alert=True)
        return
    pending[uid] = {"t": "receipt", "oid": o["id"]}
    await c.message.answer(tr(uid, "ask_receipt", id=o["id"]))
    await c.answer()


# ---------- Сообщения: своё количество, @username, чек ----------
USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")


def admin_card(o: dict, title: str) -> str:
    uname = f"@{o['username']}" if o.get("username") else "без @username"
    link = f'<a href="tg://user?id={o["user_id"]}">{html.escape(o["name"])}</a>'
    method = "СБП (вручную)" if o["method"] == "sbp" else "Крипта (CryptoBot)"
    return (
        f"{title} №{o['id']}\n"
        f"⭐ Отправить звёзд: {o['stars']}\n"
        f"🎯 Получатель: {html.escape(o['recipient'])}\n"
        f"💰 Сумма: {o['rub']} ₽ ({method})\n"
        f"💬 Комментарий к переводу: {o['id']}\n"
        f"👤 Клиент: {link} ({html.escape(uname)})\n"
        f"🆔 {o['user_id']}"
    )


@dp.message(Command("orders"))
async def cmd_orders(m: Message):
    if not is_admin(m.from_user):
        return
    items = sorted(state["orders"].values(), key=lambda x: x["id"], reverse=True)[:15]
    if not items:
        await m.answer("Заказов пока нет.")
        return
    names = {"new": "новый", "await_paid_click": "ждёт оплаты", "await_crypto": "ждёт крипту",
             "paid_wait": "чек на проверке", "paid": "оплачен, отправить звёзды", "done": "выполнен",
             "rejected": "отклонён", "cancelled": "отменён", "expired": "истёк"}
    lines = [f"№{o['id']} · {o['stars']}⭐ → {o['recipient']} · {o['rub']}₽ · {names.get(o['status'], o['status'])}"
             for o in items]
    await m.answer("Последние заказы:\n" + "\n".join(lines))


@dp.message()
async def on_message(m: Message):
    uid = m.from_user.id
    p = pending.get(uid)
    if not p:
        await m.answer(tr(uid, "hint"))
        return

    if p["t"] == "amount":
        txt = (m.text or "").strip()
        if txt.isdigit() and MIN_STARS <= int(txt) <= MAX_STARS:
            pending.pop(uid, None)
            drafts[uid] = {"stars": int(txt)}
            await show_recipient_choice(m, uid)
        else:
            await m.answer(tr(uid, "bad_amount", min=MIN_STARS, max=MAX_STARS))
        return

    if p["t"] == "recipient":
        txt = (m.text or "").strip().replace("https://t.me/", "").replace("t.me/", "")
        mt = USERNAME_RE.match(txt)
        if not mt or uid not in drafts:
            await m.answer(tr(uid, "bad_username") if uid in drafts else tr(uid, "draft_missing"))
            return
        pending.pop(uid, None)
        drafts[uid]["recipient"] = "@" + mt.group(1)
        await show_methods(m, uid)
        return

    if p["t"] == "receipt":
        o = get_order(p["oid"])
        if not o:
            pending.pop(uid, None)
            await m.answer(tr(uid, "order_missing"))
            return
        if not (m.photo or m.document):
            await m.answer(tr(uid, "bad_receipt"))
            return
        chat = admin_chat()
        if not chat:
            await m.answer(tr(uid, "admin_missing"))
            logging.warning("Нет чата админа: откройте бота со своего аккаунта и нажмите /start")
            return
        o["status"] = "paid_wait"
        save_state()
        markup = kb([[("✅ Деньги пришли", f"ok:{o['id']}"), ("❌ Не пришли", f"no:{o['id']}")]])
        card = admin_card(o, "🧾 Чек по заказу")
        try:
            if m.photo:
                await bot.send_photo(chat, m.photo[-1].file_id, caption=card, reply_markup=markup, parse_mode="HTML")
            else:
                await bot.send_document(chat, m.document.file_id, caption=card, reply_markup=markup, parse_mode="HTML")
        except Exception:
            logging.exception("Не удалось отправить чек админу")
            await m.answer(tr(uid, "admin_missing"))
            return
        pending.pop(uid, None)
        await m.answer(tr(uid, "receipt_sent"))


# ---------- Продавец (кнопки под чеком) ----------
async def admin_edit(c: CallbackQuery, extra: str, markup=None):
    base = c.message.html_text or ""
    new = base + "\n\n" + extra
    try:
        if c.message.photo or c.message.document:
            await c.message.edit_caption(caption=new, reply_markup=markup, parse_mode="HTML")
        else:
            await c.message.edit_text(new, reply_markup=markup, parse_mode="HTML")
    except Exception:
        logging.exception("admin_edit failed")


async def tell_client(o: dict, key: str):
    try:
        await bot.send_message(o["user_id"], t(o["lang"], key, id=o["id"], stars=o["stars"], recipient=o["recipient"]))
    except Exception:
        logging.exception("Не удалось написать клиенту %s", o["user_id"])


@dp.callback_query(F.data.regexp(r"^(ok|no|dn):\d+$"))
async def admin_action(c: CallbackQuery):
    if not is_admin(c.from_user):
        await c.answer("Нет доступа", show_alert=True)
        return
    act, sid = c.data.split(":")
    o = get_order(int(sid))
    if not o:
        await c.answer("Заказ не найден (бот перезапускался)", show_alert=True)
        return
    if act == "ok":
        o["status"] = "paid"
        save_state()
        await admin_edit(c, f"✅ Деньги пришли. Отправьте {o['stars']} ⭐ на {html.escape(o['recipient'])}, затем нажмите «Выполнено».",
                         kb([[("📦 Выполнено", f"dn:{o['id']}")]]))
        await tell_client(o, "paid_ok")
        await c.answer("Подтверждено")
    elif act == "no":
        o["status"] = "rejected"
        save_state()
        await admin_edit(c, "❌ Деньги не пришли. Заказ отклонён.")
        await tell_client(o, "rejected")
        await c.answer("Отклонено")
    else:
        o["status"] = "done"
        save_state()
        await admin_edit(c, "📦 Выполнено.")
        await tell_client(o, "done")
        await c.answer("Готово")


# ---------- Автопроверка крипты ----------
async def settle_crypto(o: dict, status: str):
    if o.get("status") != "await_crypto":
        return
    if status == "paid":
        o["status"] = "paid"
        save_state()
        chat = admin_chat()
        if chat:
            await bot.send_message(
                chat,
                admin_card(o, "✅ ОПЛАЧЕНО (CryptoBot), заказ") + f"\n\nОтправьте {o['stars']} ⭐ на {html.escape(o['recipient'])}, затем нажмите «Выполнено».",
                reply_markup=kb([[("📦 Выполнено", f"dn:{o['id']}")]]),
                parse_mode="HTML",
            )
        await tell_client(o, "paid_ok")
    elif status == "expired":
        o["status"] = "expired"
        save_state()
        await tell_client(o, "expired")


async def crypto_status(o: dict) -> str:
    res = await crypto_api("getInvoices", {"invoice_ids": str(o["ext_id"])})
    for inv in items_of(res):
        if inv.get("status") in ("paid", "expired"):
            return inv["status"]
    return "active"


async def poller():
    while True:
        await asyncio.sleep(15)
        for o in list(state["orders"].values()):
            if o.get("status") != "await_crypto":
                continue
            try:
                st = await crypto_status(o)
                if st != "active":
                    await settle_crypto(o, st)
            except Exception:
                logging.exception("poller error, order %s", o.get("id"))


@dp.callback_query(F.data.startswith("chk:"))
async def cb_check(c: CallbackQuery):
    uid = c.from_user.id
    o = get_order(int(c.data.split(":")[1]))
    if not o or o["user_id"] != uid:
        await c.answer(tr(uid, "order_missing"), show_alert=True)
        return
    if o["status"] == "await_crypto":
        try:
            st = await crypto_status(o)
            if st != "active":
                await settle_crypto(o, st)
        except Exception:
            logging.exception("check error")
    if o["status"] == "await_crypto":
        await c.answer(tr(uid, "not_paid"), show_alert=True)
    else:
        await c.answer("✅")


# ---------- Запуск ----------
async def health(request: web.Request):
    return web.Response(text="ok")


async def keep_alive():
    """Раз в 10 минут обращается к своему адресу, чтобы бесплатный Render не засыпал."""
    while True:
        await asyncio.sleep(600)
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(PUBLIC_URL, timeout=aiohttp.ClientTimeout(total=20))
        except Exception:
            logging.warning("keep_alive: не удалось обратиться к %s", PUBLIC_URL)


async def main():
    load_state()
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Сервер запущен на порту %s", PORT)
    logging.info("Языки: %s | CryptoBot: %s | СБП вручную: %s | Админ: %s",
                 LANGS, bool(CRYPTO_PAY_TOKEN), bool(MANUAL_REQUISITES), ADMIN_ID or ("@" + ADMIN_USERNAME))
    if not has_methods():
        logging.warning("Не задан ни CRYPTO_PAY_TOKEN, ни MANUAL_REQUISITES: оплата недоступна")
    if CRYPTO_PAY_TOKEN:
        asyncio.create_task(poller())
    if PUBLIC_URL:
        asyncio.create_task(keep_alive())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
