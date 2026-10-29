"""
SHADOW X — Telegram-бот: /start с кнопкой «Играть», админ-панель /apanel (всё кнопками),
выдача SHX и ★-звёзд игрокам, покупка PRO Battle Pass и API для index.html.

Установка:   pip install aiogram aiohttp
Запуск:      python bot.py
Все настройки (токен, адрес игры, ID админов, порт) — в блоке НАСТРОЙКИ.
Свой ID можно узнать у бота командой /id.

Как это работает:
  • /apanel — только для ADMINS. Кнопки: выдать SHX, выдать/забрать звёзды, PRO, карточка игрока
    (бан, refund), рассылка, написать игроку, статистика.
  • SHX хранится в браузере игрока, поэтому выдача SHX — это очередь на сервере: игра раз в
    несколько секунд спрашивает /sync, забирает начисления и подтверждает их через /ack.
  • «Звёзды» (★) — баланс в базе бота. Админ их выдаёт, игрок тратит в index.html на PRO Battle Pass
    (кнопка «Купить за ★ с баланса»). Обычная оплата настоящими Telegram Stars тоже работает.
"""
import asyncio, hashlib, hmac, html, json, os, random, re, sqlite3, time
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BotCommand, BotCommandScopeChat, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, LabeledPrice, MenuButtonWebApp, Message,
                           PreCheckoutQuery, WebAppInfo)

# ===== НАСТРОЙКИ — правьте только здесь =====
BOT_TOKEN = "8658898988:AAFfoufMY5mDNgD81ExqtQQR1YRhzSa-2HU"   # токен в кавычках
WEBAPP_URL = "https://shadowxx.netlify.app"        # где лежит index.html (Netlify)
ADMINS = {8681404945}                             # Telegram ID админов через запятую
ADMIN_USERNAMES = {"jqzqp"}                       # админы по @username (без @, через запятую)
HOST_IP = "91.219.149.18"                         # IP сервера (только для подсказки в консоли)
PORT = "2344"                                   # порт API мини-приложения
# =============================================
if "ВСТАВЬТЕ" in BOT_TOKEN:
    raise SystemExit("Впишите токен бота в строку BOT_TOKEN (вверху файла bot.py)")
_u = urlparse(WEBAPP_URL)
ORIGIN = f"{_u.scheme}://{_u.netloc}"            # для CORS
WELCOME = Path(__file__).with_name("welcome.jpg")
STARS = 15                                        # цена PRO (в ★), должна совпадать с PASS.stars в index.html
SEASON = "halloween26"                            # должен совпадать с SEASON.id в index.html
SEASONS = {SEASON}
MAX_SHX = 1_000_000_000
MAX_STARS = 1_000_000

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
db = sqlite3.connect("shadowx.db", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
  joined INTEGER, banned INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pro(uid INTEGER, season TEXT, charge_id TEXT UNIQUE, stars INTEGER, ts INTEGER,
  PRIMARY KEY(uid, season));
CREATE TABLE IF NOT EXISTS bal(uid INTEGER PRIMARY KEY, stars INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS grants(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, shx INTEGER,
  ts INTEGER, acked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS duels(uid INTEGER PRIMARY KEY, name TEXT, wins INTEGER DEFAULT 0, games INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS grants_uid ON grants(uid, acked);
""")
db.commit()


# ---------- вспомогательное ----------
def is_admin(u):
    if not u:
        return False
    if u.id in ADMINS:
        return True
    if u.username and u.username.lower() in {x.lower().lstrip("@") for x in ADMIN_USERNAMES}:
        ADMINS.add(u.id)  # запоминаем ID, пока бот работает
        return True
    return False


def fmt(n):
    return f"{n:,}".replace(",", " ")


def play_kb(with_balance=True):
    rows = [[InlineKeyboardButton(text="🎮 Играть", web_app=WebAppInfo(url=WEBAPP_URL))]]
    if with_balance:
        rows.append([InlineKeyboardButton(text="⭐ Мой баланс", callback_data="bal")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def add_user(u):
    db.execute("INSERT OR IGNORE INTO users(uid,username,first_name,joined) VALUES(?,?,?,?)",
               (u.id, u.username, u.first_name, int(time.time())))
    db.execute("UPDATE users SET username=?, first_name=?, blocked=0 WHERE uid=?", (u.username, u.first_name, u.id))
    db.commit()


def is_banned(uid):
    r = db.execute("SELECT banned FROM users WHERE uid=?", (uid,)).fetchone()
    return bool(r and r[0])


def to_uid(arg):
    try:
        return int(arg)
    except (TypeError, ValueError):
        return None


def get_stars(uid):
    r = db.execute("SELECT stars FROM bal WHERE uid=?", (uid,)).fetchone()
    return r[0] if r else 0


def add_stars(uid, delta):
    db.execute("INSERT OR IGNORE INTO bal(uid,stars) VALUES(?,0)", (uid,))
    db.execute("UPDATE bal SET stars=MAX(0, stars+?) WHERE uid=?", (delta, uid))
    db.commit()
    return get_stars(uid)


def has_pro(uid, season=SEASON):
    return bool(db.execute("SELECT 1 FROM pro WHERE uid=? AND season=?", (uid, season)).fetchone())


def give_pro(uid):
    """True — выдан, False — у игрока уже есть PRO (оплаченный не перезаписываем)."""
    cur = db.execute("INSERT OR IGNORE INTO pro VALUES(?,?,?,?,?)",
                     (uid, SEASON, f"admin-{int(time.time())}-{uid}", 0, int(time.time())))
    db.commit()
    return cur.rowcount > 0


def label(uid):
    u = db.execute("SELECT username, first_name FROM users WHERE uid=?", (uid,)).fetchone()
    if not u:
        return f"<code>{uid}</code> (нет в базе бота)"
    return f"{html.escape(u[1] or '—')}{' @' + u[0] if u[0] else ''} · <code>{uid}</code>"


async def notify(uid, text):
    try:
        await bot.send_message(uid, text, reply_markup=play_kb(), parse_mode="HTML")
        return True
    except Exception:
        return False


# ---------- команды для всех ----------
@dp.message(CommandStart())
async def start(m: Message):
    add_user(m.from_user)
    if is_banned(m.from_user.id):
        return
    caption = (f"<b>Добро пожаловать в SHADOW X, {html.escape(m.from_user.first_name)}!</b>\n\n"
               "Кейсы, дуэли, обмен NFT и сезонный Battle Pass — жми кнопку и заходи в игру.")
    if WELCOME.exists():
        await m.answer_photo(FSInputFile(WELCOME), caption=caption, reply_markup=play_kb(), parse_mode="HTML")
    else:
        await m.answer(caption, reply_markup=play_kb(), parse_mode="HTML")


@dp.message(Command("play"))
async def play(m: Message):
    if not is_banned(m.from_user.id):
        await m.answer("Заходи и играй 👇", reply_markup=play_kb())


@dp.message(Command("balance"))
async def balance(m: Message):
    if not is_banned(m.from_user.id):
        await m.answer(f"⭐ Ваш баланс: <b>{fmt(get_stars(m.from_user.id))} ★</b>\n"
                       f"Звёзды можно потратить в игре на PRO Battle Pass.",
                       reply_markup=play_kb(False), parse_mode="HTML")


@dp.callback_query(F.data == "bal")
async def cb_balance(c: CallbackQuery):
    await c.answer(f"⭐ Баланс: {fmt(get_stars(c.from_user.id))} ★\nТратятся в игре на PRO Battle Pass.", show_alert=True)


@dp.message(Command("id"))
async def my_id(m: Message):
    await m.answer(f"Ваш Telegram ID: {m.from_user.id}")


@dp.message(Command("help"))
async def help_(m: Message):
    text = "/start — приветствие\n/play — открыть игру\n/balance — баланс звёзд\n/id — узнать свой ID"
    if is_admin(m.from_user):
        text += ("\n\nАдминам:\n/apanel — админ-панель (кнопки)\n"
                 "Быстрые команды: /addshx ID сумма, /addstars ID сумма, /grantpro ID, /revokepro ID, "
                 "/refund ID, /ban ID, /unban ID, /whois ID, /say ID текст, /broadcast текст, /stats, /ping")
    await m.answer(text)


# ================= АДМИН-ПАНЕЛЬ (только ADMINS) =================
async def admin(event) -> bool:
    return is_admin(event.from_user)


r = Router()
r.message.filter(admin)
r.callback_query.filter(admin)


class AP(StatesGroup):
    target = State()   # ждём ID / @username / пересланное сообщение
    amount = State()   # ждём сумму
    text = State()     # ждём текст (рассылка / сообщение игроку)


TITLES = {"shx": "💰 Выдать SHX", "stars": "⭐ Выдать звёзды", "unstars": "➖ Забрать звёзды",
          "pro": "👑 PRO Battle Pass", "user": "👤 Игрок", "say": "✉️ Написать игроку"}
PRESETS = {"shx": [1_000, 10_000, 100_000, 1_000_000], "stars": [5, 15, 50, 100], "unstars": [5, 15, 50, 100]}


def menu_text():
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    pro = db.execute("SELECT COUNT(*) FROM pro WHERE season=?", (SEASON,)).fetchone()[0]
    return f"🛠 <b>Админ-панель SHADOW X</b>\nИгроков: {users} · PRO: {pro}\n\nВыбери действие:"


def menu_kb():
    return kb([[("💰 Выдать SHX", "ap:shx"), ("⭐ Выдать звёзды", "ap:stars")],
               [("➖ Забрать звёзды", "ap:unstars"), ("👑 PRO Battle Pass", "ap:pro")],
               [("👤 Игрок", "ap:user"), ("📊 Статистика", "ap:stats")],
               [("📢 Рассылка", "ap:bc"), ("✉️ Написать игроку", "ap:say")],
               [("❌ Закрыть", "ap:close")]])


BACK = kb([[("⬅️ В меню", "ap:menu")]])


async def show(x, text, markup=None):
    """Для кнопки — правит сообщение, для обычного сообщения — отправляет новое."""
    if isinstance(x, CallbackQuery):
        try:
            await x.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except TelegramBadRequest:
            pass
    else:
        await x.answer(text, reply_markup=markup, parse_mode="HTML")


def card_text(uid):
    u = db.execute("SELECT username, first_name, joined, banned, blocked FROM users WHERE uid=?", (uid,)).fetchone()
    p = db.execute("SELECT stars FROM pro WHERE uid=? AND season=?", (uid, SEASON)).fetchone()
    pend = db.execute("SELECT COALESCE(SUM(shx),0) FROM grants WHERE uid=? AND acked=0", (uid,)).fetchone()[0]
    lines = [f"👤 {label(uid)}"]
    if u:
        lines.append(f"Первый вход: {time.strftime('%d.%m.%Y %H:%M', time.localtime(u[2] or 0))}")
        lines.append(f"Бан: {'да' if u[3] else 'нет'} · заблокировал бота: {'да' if u[4] else 'нет'}")
    lines.append(f"⭐ Баланс: {fmt(get_stars(uid))} ★")
    lines.append(f"👑 PRO: {'да' + (' (куплен за ' + str(p[0]) + ' ★)' if p and p[0] else ' (выдан)') if p else 'нет'}")
    if pend:
        lines.append(f"⏳ SHX ждут игрока: {fmt(pend)}")
    return "\n".join(lines)


def card_kb(uid):
    return kb([[("💰 +SHX", f"ap:q:shx:{uid}"), ("⭐ +★", f"ap:q:stars:{uid}"), ("➖ −★", f"ap:q:unstars:{uid}")],
               [("👑 Выдать PRO", f"ap:pg:{uid}"), ("🚫 Забрать PRO", f"ap:pr:{uid}")],
               [("💸 Refund", f"ap:rf:{uid}")],
               [("⛔ Бан", f"ap:bn:{uid}"), ("✅ Разбан", f"ap:ub:{uid}")],
               [("⬅️ В меню", "ap:menu")]])


def resolve(m: Message):
    fo = getattr(m, "forward_origin", None)
    su = getattr(fo, "sender_user", None)
    if su:
        return su.id
    t = (m.text or "").strip()
    if t.lstrip("-").isdigit():
        return int(t)
    if t.startswith("@"):
        row = db.execute("SELECT uid FROM users WHERE lower(username)=?", (t[1:].lower(),)).fetchone()
        return row[0] if row else None
    return None


async def do_amount(uid, act, n):
    """Выполняет выдачу и возвращает текст-отчёт."""
    if act == "shx":
        db.execute("INSERT INTO grants(uid,shx,ts) VALUES(?,?,?)", (uid, n, int(time.time())))
        db.commit()
        sent = await notify(uid, f"💰 Вам начислено <b>{fmt(n)} SHX</b>. Откройте игру — монеты зачислятся сами.")
        return (f"✅ {fmt(n)} SHX → {label(uid)}\nЗачислятся, как только игрок откроет игру."
                f"{'' if sent else chr(10) + '⚠️ Уведомить в чате не вышло (игрок не запускал бота).'}")
    total = add_stars(uid, n if act == "stars" else -n)
    if act == "stars":
        sent = await notify(uid, f"⭐ Вам начислено <b>{fmt(n)} ★</b>. Баланс: {fmt(total)} ★.\n"
                                 f"Тратьте в игре на PRO Battle Pass.")
        return (f"✅ {fmt(n)} ★ → {label(uid)}\nБаланс: {fmt(total)} ★"
                f"{'' if sent else chr(10) + '⚠️ Уведомить в чате не вышло.'}")
    return f"✅ −{fmt(n)} ★ у {label(uid)}\nБаланс: {fmt(total)} ★"


def parse_amount(act, s):
    try:
        n = int(str(s).replace(" ", "").replace("_", "").replace(",", ""))
    except ValueError:
        return None
    lim = MAX_SHX if act == "shx" else MAX_STARS
    return n if 1 <= n <= lim else None


def amount_kb(act, uid):
    p = PRESETS[act]
    return kb([[(fmt(n), f"ap:amt:{n}") for n in p[:2]], [(fmt(n), f"ap:amt:{n}") for n in p[2:]],
               [("👤 Карточка", f"ap:u:{uid}"), ("⬅️ В меню", "ap:menu")]])


async def ask_amount(x, state, act, uid):
    await state.set_state(AP.amount)
    await state.update_data(act=act, uid=uid)
    await show(x, f"{TITLES[act]}\nИгрок: {label(uid)}\n\nВыбери сумму кнопкой или отправь число:", amount_kb(act, uid))


# ---- быстрые команды ----
@r.message(Command("apanel"))
async def apanel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(menu_text(), reply_markup=menu_kb(), parse_mode="HTML")


@r.message(Command("ping"))
async def ping(m: Message):
    await m.answer("pong")


@r.message(Command("stats"))
async def stats(m: Message):
    await m.answer(stats_text(), parse_mode="HTML")


def stats_text():
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    blocked = db.execute("SELECT COUNT(*) FROM users WHERE blocked=1").fetchone()[0]
    banned = db.execute("SELECT COUNT(*) FROM users WHERE banned=1").fetchone()[0]
    pro, stars = db.execute("SELECT COUNT(*), COALESCE(SUM(stars),0) FROM pro WHERE season=?", (SEASON,)).fetchone()
    held = db.execute("SELECT COALESCE(SUM(stars),0) FROM bal").fetchone()[0]
    pend = db.execute("SELECT COALESCE(SUM(shx),0) FROM grants WHERE acked=0").fetchone()[0]
    return (f"📊 <b>Статистика</b>\nПользователей: {users} (заблокировали бота: {blocked}, в бане: {banned})\n"
            f"PRO в сезоне {SEASON}: {pro}\nНастоящих Stars получено: {stars} ★\n"
            f"★ на балансах игроков: {fmt(held)}\nSHX ждут игроков: {fmt(pend)}")


@r.message(Command("addshx"))
async def addshx(m: Message, command: CommandObject):
    a = (command.args or "").split()
    uid, n = (to_uid(a[0]), parse_amount("shx", a[1])) if len(a) == 2 else (None, None)
    if uid is None or n is None:
        return await m.answer("Использование: /addshx ID сумма")
    await m.answer(await do_amount(uid, "shx", n), parse_mode="HTML")


@r.message(Command("addstars"))
async def addstars(m: Message, command: CommandObject):
    a = (command.args or "").split()
    uid = to_uid(a[0]) if len(a) == 2 else None
    neg = len(a) == 2 and a[1].startswith("-")
    n = parse_amount("stars", a[1].lstrip("-")) if len(a) == 2 else None
    if uid is None or n is None:
        return await m.answer("Использование: /addstars ID сумма (минус — забрать)")
    await m.answer(await do_amount(uid, "unstars" if neg else "stars", n), parse_mode="HTML")


async def do_broadcast(chat_id, src_id):
    rows = db.execute("SELECT uid FROM users WHERE banned=0 AND blocked=0").fetchall()
    ok = fail = 0
    for (uid,) in rows:
        for _ in range(2):
            try:
                await bot.copy_message(uid, chat_id, src_id)
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                db.execute("UPDATE users SET blocked=1 WHERE uid=?", (uid,))
                db.commit()
                fail += 1
                break
            except Exception:
                fail += 1
                break
        await asyncio.sleep(0.05)
    return ok, fail


@r.message(Command("broadcast"))
async def broadcast(m: Message, command: CommandObject):
    src = m.reply_to_message or (m if command.args else None)
    if not src:
        return await m.answer("Использование: /broadcast текст — или ответьте командой на нужное сообщение. "
                              "Удобнее через /apanel → 📢 Рассылка")
    if src is m:  # текст после команды — шлём как обычный текст
        n = db.execute("SELECT COUNT(*) FROM users WHERE banned=0 AND blocked=0").fetchone()[0]
        await m.answer(f"Рассылка на {n} пользователей…")
        ok = fail = 0
        for (uid,) in db.execute("SELECT uid FROM users WHERE banned=0 AND blocked=0").fetchall():
            try:
                await bot.send_message(uid, command.args)
                ok += 1
            except TelegramForbiddenError:
                db.execute("UPDATE users SET blocked=1 WHERE uid=?", (uid,))
                db.commit()
                fail += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.05)
    else:
        await m.answer("Рассылка пошла…")
        ok, fail = await do_broadcast(m.chat.id, src.message_id)
    await m.answer(f"Готово. Доставлено: {ok}, не доставлено: {fail}")


@r.message(Command("say"))
async def say(m: Message, command: CommandObject):
    parts = (command.args or "").split(maxsplit=1)
    uid = to_uid(parts[0]) if parts else None
    if uid is None or len(parts) < 2:
        return await m.answer("Использование: /say ID текст")
    try:
        await bot.send_message(uid, parts[1])
        await m.answer("Отправлено")
    except Exception as e:
        await m.answer(f"Не удалось отправить: {e}")


async def do_refund(uid):
    row = db.execute("SELECT charge_id FROM pro WHERE uid=? AND season=? AND stars>0", (uid, SEASON)).fetchone()
    if not row:
        return "Оплаченного PRO (настоящими Stars) у этого игрока нет"
    try:
        await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=row[0])
    except Exception as e:
        return f"Возврат не удался: {e}"
    db.execute("DELETE FROM pro WHERE uid=? AND season=?", (uid, SEASON))
    db.commit()
    return "Stars возвращены, PRO отозван"


def set_ban(uid, v):
    db.execute("INSERT OR IGNORE INTO users(uid,joined) VALUES(?,?)", (uid, int(time.time())))
    db.execute("UPDATE users SET banned=? WHERE uid=?", (v, uid))
    db.commit()


def revoke_pro(uid):
    db.execute("DELETE FROM pro WHERE uid=? AND season=?", (uid, SEASON))
    db.commit()


def one_id(name):
    async def h(m: Message, command: CommandObject):
        uid = to_uid(command.args)
        if uid is None:
            return await m.answer(f"Использование: /{name} ID")
        if name == "grantpro":
            await m.answer("PRO выдан" if give_pro(uid) else "У игрока уже есть PRO")
        elif name == "revokepro":
            revoke_pro(uid)
            await m.answer("PRO отозван")
        elif name == "refund":
            await m.answer(await do_refund(uid))
        elif name == "ban":
            set_ban(uid, 1)
            await m.answer(f"{uid} в бане")
        elif name == "unban":
            set_ban(uid, 0)
            await m.answer(f"{uid} разбанен")
        elif name == "whois":
            await m.answer(card_text(uid), reply_markup=card_kb(uid), parse_mode="HTML")
    return h


for _n in ("grantpro", "revokepro", "refund", "ban", "unban", "whois"):
    r.message.register(one_id(_n), Command(_n))


# ---- кнопки панели ----
@r.callback_query(F.data == "ap:menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, menu_text(), menu_kb())
    await c.answer()


@r.callback_query(F.data == "ap:close")
async def cb_close(c: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await c.message.delete()
    except TelegramBadRequest:
        pass
    await c.answer()


@r.callback_query(F.data == "ap:stats")
async def cb_stats(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, stats_text(), kb([[("🔄 Обновить", "ap:stats"), ("⬅️ В меню", "ap:menu")]]))
    await c.answer()


@r.callback_query(F.data.in_({"ap:shx", "ap:stars", "ap:unstars", "ap:pro", "ap:user", "ap:say"}))
async def cb_pick(c: CallbackQuery, state: FSMContext):
    act = c.data[3:]
    await state.set_state(AP.target)
    await state.update_data(act=act)
    await show(c, f"{TITLES[act]}\n\nОтправь <b>ID</b> игрока, <b>@username</b> (если он есть в базе бота) "
                  f"или перешли сообщение игрока.", BACK)
    await c.answer()


@r.callback_query(F.data == "ap:bc")
async def cb_bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(AP.text)
    await state.update_data(act="bc")
    await show(c, "📢 <b>Рассылка</b>\n\nОтправь сообщение (текст, фото, что угодно) — оно уйдёт всем игрокам.", BACK)
    await c.answer()


@r.message(AP.target)
async def got_target(m: Message, state: FSMContext):
    uid = resolve(m)
    if uid is None:
        return await m.answer("Не понял. Нужен числовой ID, @username из базы бота или пересланное сообщение.",
                              reply_markup=BACK)
    act = (await state.get_data()).get("act")
    if act in ("shx", "stars", "unstars"):
        return await ask_amount(m, state, act, uid)
    if act == "say":
        await state.set_state(AP.text)
        await state.update_data(uid=uid)
        return await m.answer(f"✉️ Игрок: {label(uid)}\n\nОтправь текст сообщения:", reply_markup=BACK, parse_mode="HTML")
    await state.clear()
    await m.answer(card_text(uid), reply_markup=card_kb(uid), parse_mode="HTML")


@r.message(AP.amount)
async def got_amount(m: Message, state: FSMContext):
    d = await state.get_data()
    n = parse_amount(d["act"], m.text or "")
    if n is None:
        return await m.answer("Нужно целое число больше нуля (без лишних символов).")
    await state.clear()
    await m.answer(await do_amount(d["uid"], d["act"], n), parse_mode="HTML",
                   reply_markup=kb([[("➕ Ещё", f"ap:q:{d['act']}:{d['uid']}"), ("👤 Карточка", f"ap:u:{d['uid']}")],
                                    [("⬅️ В меню", "ap:menu")]]))


@r.callback_query(F.data.startswith("ap:amt:"))
async def cb_amount(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if "uid" not in d or "act" not in d:
        return await c.answer("Сессия устарела, открой /apanel", show_alert=True)
    n = parse_amount(d["act"], c.data.split(":")[2])
    await state.clear()
    if n is None:
        return await c.answer("Неверная сумма", show_alert=True)
    await show(c, await do_amount(d["uid"], d["act"], n),
               kb([[("➕ Ещё", f"ap:q:{d['act']}:{d['uid']}"), ("👤 Карточка", f"ap:u:{d['uid']}")],
                   [("⬅️ В меню", "ap:menu")]]))
    await c.answer("Готово")


@r.callback_query(F.data.startswith("ap:q:"))
async def cb_quick(c: CallbackQuery, state: FSMContext):
    _, _, act, uid = c.data.split(":")
    await ask_amount(c, state, act, int(uid))
    await c.answer()


@r.callback_query(F.data.startswith("ap:u:"))
async def cb_card(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = int(c.data.split(":")[2])
    await show(c, card_text(uid), card_kb(uid))
    await c.answer()


@r.callback_query(F.data.regexp(r"^ap:(pg|pr|rf|bn|ub):-?\d+$"))
async def cb_card_action(c: CallbackQuery):
    _, act, uid = c.data.split(":")
    uid = int(uid)
    if act == "pg":
        msg = "PRO выдан ✅" if give_pro(uid) else "У игрока уже есть PRO"
    elif act == "pr":
        revoke_pro(uid)
        msg = "PRO отозван"
    elif act == "rf":
        msg = await do_refund(uid)
    elif act == "bn":
        set_ban(uid, 1)
        msg = "Игрок в бане ⛔"
    else:
        set_ban(uid, 0)
        msg = "Игрок разбанен ✅"
    await show(c, card_text(uid), card_kb(uid))
    await c.answer(msg, show_alert=len(msg) > 40)


@r.message(AP.text)
async def got_text(m: Message, state: FSMContext):
    d = await state.get_data()
    if d.get("act") == "bc":
        n = db.execute("SELECT COUNT(*) FROM users WHERE banned=0 AND blocked=0").fetchone()[0]
        await state.update_data(src=m.message_id)
        return await m.answer(f"Отправить это сообщение <b>{n}</b> игрокам?", parse_mode="HTML",
                              reply_markup=kb([[("✅ Отправить", "ap:bcgo"), ("⬅️ Отмена", "ap:menu")]]))
    uid = d.get("uid")
    await state.clear()
    try:
        await bot.copy_message(uid, m.chat.id, m.message_id)
        await m.answer(f"✅ Отправлено: {label(uid)}", parse_mode="HTML", reply_markup=BACK)
    except Exception as e:
        await m.answer(f"Не удалось отправить: {html.escape(str(e))}", reply_markup=BACK)


@r.callback_query(F.data == "ap:bcgo")
async def cb_bcgo(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    src = d.get("src")
    await state.clear()
    if not src:
        return await c.answer("Сессия устарела", show_alert=True)
    await show(c, "📢 Рассылка пошла…")
    await c.answer()
    ok, fail = await do_broadcast(c.message.chat.id, src)
    await c.message.answer(f"Готово. Доставлено: {ok}, не доставлено: {fail}", reply_markup=BACK)


dp.include_router(r)

# «Отказ» вешаем на отдельный роутер, подключённый ПОСЛЕ админского:
# в aiogram 3 обработчики самого dp срабатывают раньше вложенных роутеров,
# из-за чего админу тоже приходило «Только для администраторов».
denied_router = Router()


@denied_router.message(Command("apanel"))
async def apanel_denied(m: Message):
    await m.answer("⛔ Только для администраторов.")


dp.include_router(denied_router)


# ---------- оплата PRO настоящими Stars ----------
@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    ok = False
    try:
        kind, season, uid = q.invoice_payload.split(":")
        ok = (kind == "pro" and season in SEASONS and int(uid) == q.from_user.id
              and q.total_amount == STARS and not is_banned(q.from_user.id))
    except Exception:
        pass
    await q.answer(ok=ok, error_message=None if ok else "Платёж недоступен")


@dp.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment
    _, season, uid = sp.invoice_payload.split(":")
    db.execute("INSERT OR IGNORE INTO pro VALUES(?,?,?,?,?)",
               (int(uid), season, sp.telegram_payment_charge_id, sp.total_amount, int(time.time())))
    db.commit()
    await m.answer("PRO Battle Pass активирован ✅", reply_markup=play_kb())


# ---------- HTTP API для мини-приложения ----------
def check_init_data(init_data, max_age=86400):
    """Проверка подписи Telegram WebApp initData; возвращает user_id или None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        data = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, data.encode(), hashlib.sha256).hexdigest(), got):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return int(json.loads(pairs["user"])["id"])
    except Exception:
        return None


def uid_from(request):
    auth = request.headers.get("Authorization", "")
    return check_init_data(auth[4:]) if auth.startswith("tma ") else None


async def body(request):
    try:
        d = await request.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


@web.middleware
async def cors(request, handler):
    resp = web.Response() if request.method == "OPTIONS" else await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


async def pro_invoice(request):
    uid = uid_from(request)
    if not uid or is_banned(uid):
        return web.json_response({"error": "auth"}, status=401)
    season = (await body(request)).get("season")
    if season not in SEASONS:
        return web.json_response({"error": "season"}, status=400)
    link = await bot.create_invoice_link(
        title="PRO Battle Pass", description="PRO-награды на всех 50 уровнях сезона",
        payload=f"pro:{season}:{uid}", currency="XTR",
        prices=[LabeledPrice(label="PRO Battle Pass", amount=STARS)])
    return web.json_response({"link": link})


async def pro_status(request):
    uid = uid_from(request)
    if not uid:
        return web.json_response({"error": "auth"}, status=401)
    return web.json_response({"pro": has_pro(uid, request.query.get("season", ""))})


async def sync(request):
    """Одним запросом: PRO, баланс ★ и ожидающие начисления SHX."""
    uid = uid_from(request)
    if not uid or is_banned(uid):
        return web.json_response({"error": "auth"}, status=401)
    SEEN[uid] = time.time()
    rows = db.execute("SELECT id, shx FROM grants WHERE uid=? AND acked=0 ORDER BY id LIMIT 50", (uid,)).fetchall()
    return web.json_response({"pro": has_pro(uid, request.query.get("season", "")), "stars": get_stars(uid),
                              "grants": [{"id": i, "shx": s} for i, s in rows]})


async def ack(request):
    uid = uid_from(request)
    if not uid:
        return web.json_response({"error": "auth"}, status=401)
    ids = [int(i) for i in (await body(request)).get("ids", []) if str(i).lstrip("-").isdigit()][:100]
    db.executemany("UPDATE grants SET acked=1 WHERE id=? AND uid=?", [(i, uid) for i in ids])
    db.commit()
    return web.json_response({"ok": True})


async def pro_buy_stars(request):
    """Покупка PRO за ★ с баланса в боте."""
    uid = uid_from(request)
    if not uid or is_banned(uid):
        return web.json_response({"error": "auth"}, status=401)
    season = (await body(request)).get("season")
    if season not in SEASONS:
        return web.json_response({"error": "season"}, status=400)
    if has_pro(uid, season):
        return web.json_response({"pro": True, "stars": get_stars(uid)})
    cur = db.execute("UPDATE bal SET stars=stars-? WHERE uid=? AND stars>=?", (STARS, uid, STARS))
    if cur.rowcount == 0:
        db.rollback()
        return web.json_response({"error": "no_stars", "stars": get_stars(uid)}, status=402)
    db.execute("INSERT OR IGNORE INTO pro VALUES(?,?,?,?,?)", (uid, season, f"bal-{int(time.time())}-{uid}", 0, int(time.time())))
    db.commit()
    return web.json_response({"pro": True, "stars": get_stars(uid)})



# ---------- ОНЛАЙН: дуэли игрок-против-игрока, рейтинг, списание ★ ----------
STAKES = {1000, 5000, 20000}
WAIT = {}   # ставка -> (uid, имя, время)  — игрок ждёт соперника
RES = {}    # uid -> (время, результат)    — готовый результат для того, кто ждал
SEEN = {}   # uid -> когда последний раз был в игре
BOT_NICKS = ["Убитый", "ваня шур", "Тень", "Фантом", "Некромант", "Ghost", "Вампир", "NightOwl", "Ведьмак", "Skull"]


def clean(n):
    return re.sub(r"[^\w .\-]", "", n or "")[:20] or "Игрок"


def who(request):
    uid = uid_from(request)
    if not uid or is_banned(uid):
        return None, None
    try:
        u = json.loads(dict(parse_qsl(request.headers["Authorization"][4:]))["user"])
        name = clean(u.get("first_name") or u.get("username"))
    except Exception:
        name = "Игрок"
    SEEN[uid] = time.time()
    return uid, name


def record(uid, name, win):
    db.execute("INSERT OR IGNORE INTO duels(uid,name) VALUES(?,?)", (uid, name))
    db.execute("UPDATE duels SET name=?, wins=wins+?, games=games+1 WHERE uid=?", (name, int(win), uid))
    db.commit()


def bot_result():
    return {"win": random.random() < .5, "name": random.choice(BOT_NICKS), "pvp": False}


async def duel_join(request):
    uid, name = who(request)
    if not uid:
        return web.json_response({"error": "auth"}, status=401)
    st = (await body(request)).get("stake")
    if st not in STAKES:
        return web.json_response({"error": "stake"}, status=400)
    now = time.time()
    for k in [k for k, v in RES.items() if now - v[0] > 60]:
        RES.pop(k, None)
    for k in [k for k, v in WAIT.items() if v[0] == uid or now - v[2] > 10]:
        WAIT.pop(k, None)
    w = WAIT.pop(st, None)
    if w and w[0] != uid:
        win = random.random() < .5          # исход решает сервер — один раз для обоих
        RES[w[0]] = (now, {"win": not win, "name": name, "pvp": True})
        record(w[0], w[1], not win)
        record(uid, name, win)
        return web.json_response({"win": win, "name": w[1], "pvp": True})
    WAIT[st] = (uid, name, now)
    return web.json_response({"wait": True})


async def duel_status(request):
    uid, _ = who(request)
    if not uid:
        return web.json_response({"error": "auth"}, status=401)
    r = RES.pop(uid, None)
    if r:
        return web.json_response(r[1])
    for st, w in list(WAIT.items()):
        if w[0] == uid:
            if time.time() - w[2] > 6:     # никто не пришёл — играем с ботом
                WAIT.pop(st, None)
                return web.json_response(bot_result())
            return web.json_response({"wait": True})
    return web.json_response(bot_result())


async def top(request):
    uid, _ = who(request)
    if not uid:
        return web.json_response({"error": "auth"}, status=401)
    rows = db.execute("SELECT name, wins, games FROM duels ORDER BY wins DESC, games ASC LIMIT 10").fetchall()
    online = sum(1 for t in SEEN.values() if time.time() - t < 40)
    return web.json_response({"online": online, "top": [{"name": n, "wins": w, "games": g} for n, w, g in rows]})


async def spend(request):
    """Списание ★ с баланса (докупка уровня Battle Pass)."""
    uid, _ = who(request)
    if not uid:
        return web.json_response({"error": "auth"}, status=401)
    amt = (await body(request)).get("amount")
    if not isinstance(amt, int) or not 1 <= amt <= 50:
        return web.json_response({"error": "amount"}, status=400)
    cur = db.execute("UPDATE bal SET stars=stars-? WHERE uid=? AND stars>=?", (amt, uid, amt))
    if cur.rowcount == 0:
        db.rollback()
        return web.json_response({"error": "no_stars", "stars": get_stars(uid)}, status=402)
    db.commit()
    return web.json_response({"stars": get_stars(uid)})


# ---------- запуск ----------
async def setup_bot():
    await bot.set_my_commands([BotCommand(command="start", description="Приветствие"),
                               BotCommand(command="play", description="Открыть игру"),
                               BotCommand(command="balance", description="Баланс звёзд"),
                               BotCommand(command="id", description="Узнать свой ID"),
                               BotCommand(command="help", description="Помощь")])
    for a in ADMINS:
        try:
            await bot.set_my_commands(
                [BotCommand(command=c, description=d) for c, d in [
                    ("apanel", "Админ-панель"), ("start", "Приветствие"), ("play", "Открыть игру"),
                    ("balance", "Баланс звёзд"), ("stats", "Статистика"), ("help", "Помощь")]],
                scope=BotCommandScopeChat(chat_id=a))
        except Exception:
            pass
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Играть", web_app=WebAppInfo(url=WEBAPP_URL)))


async def main():
    app = web.Application(middlewares=[cors])
    app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response())
    app.router.add_post("/pro/invoice", pro_invoice)
    app.router.add_get("/pro/status", pro_status)
    app.router.add_post("/pro/buy_stars", pro_buy_stars)
    app.router.add_get("/sync", sync)
    app.router.add_post("/ack", ack)
    app.router.add_post("/duel/join", duel_join)
    app.router.add_get("/duel/status", duel_status)
    app.router.add_get("/top", top)
    app.router.add_post("/spend", spend)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await setup_bot()
    print(f"Бот запущен. API слушает порт {PORT}.\nДля Telegram нужен HTTPS-адрес: cloudflared tunnel --url http://localhost:{PORT}\nполученную ссылку https://....trycloudflare.com вставьте в index.html → API_URL")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
