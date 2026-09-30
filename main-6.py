import asyncio
import logging
import os
import re
import aiosqlite
from datetime import datetime, timedelta
from urllib.parse import unquote
from aiogram import Bot, Dispatcher, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)

# Token va admin ID endi muhit o'zgaruvchilaridan olinadi (kodga yozilmaydi).
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "7825563654"))
ADMIN_USERNAME = (
    os.getenv("TELEGRAM_ADMIN_USERNAME")
    or os.getenv("ADMIN_USERNAME", "Bexr7zz")
).lstrip("@")
PREMIUM_DAYS = 30
NOTIFY_BEFORE_DAYS = 3
PAGE_SIZE = 10
BOT_USERNAME = ""

API_TIMEOUT = 10
DB_PATH = "bot_data.db"

if not BOT_TOKEN:
    raise ValueError(
        "TELEGRAM_BOT_TOKEN topilmadi! Bot tokenini xavfsiz Secrets bo'limiga qo'shing."
    )
if ADMIN_ID <= 0:
    raise ValueError("TELEGRAM_ADMIN_ID topilmadi yoki noto'g'ri.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ─── ASOSIY MENYU TUGMALARINI HAR DOIM DARHOL ISHLATISH ──────────────────────
# Muammo: admin/foydalanuvchi biror bosqichda (masalan "📣 Xabar yuborish"
# yoki "👥 Premium berish" kutayotgan holatda) turib, fikridan qaytib boshqa
# asosiy menyu tugmasini bossa, matn ESKI bosqich handleriga tushib qolib
# noto'g'ri ishlanardi — shu sabab "qotib qolgandek" bo'lib, ikkinchi marta
# bosish kerak bo'lardi. Quyidagi middleware bu tugmalar bosilganda eskirgan
# holatni DARHOL tozalaydi, shunda tugma birinchi bosishdayoq to'g'ri ishlaydi.
MENU_INTERRUPT_TEXTS = {
    "🔍 Izlash", "👤 Profil", "👤 Mening profilim",
    "💬 Suhbatlashish",
    "📝 Profil qo'shish",
    "🗑 Profilni o'chirish",
    "🧹 Voyaga yetmagan profillarni o'chirish",
    "📢 Kanal qo'shish", "🗑 Kanal o'chirish", "📋 Kanallar ro'yxati",
    "👥 Premium berish", "❌ Premium olish", "📊 Premium ro'yxati",
    "📊 Statistika", "📣 Xabar yuborish", "💰 Premium narxini o'zgartirish",
    "💳 Karta raqamini o'zgartirish", "👨‍💼 Adminlar", "🗄 Zaxira olish",
    "❌ Suhbatni tugatish",
    "❌ Bekor qilish",
    "🏠 Bosh menyu",
}
# ("🌟 Premium" atayin bu ro'yxatga kiritilmagan — u ro'yxatdan o'tish
# bosqichida ham xuddi shu matn bilan ishlatiladi, holatni majburan
# tozalash o'sha bosqichni buzib qo'yishi mumkin edi.)

# Botda HAQIQATDA ro'yxatga olingan buyruqlar (pastdagi set_my_commands
# ro'yxatiga mos). "/avto" kabi so'zlar bu yerga KIRMAYDI — ular haqiqiy
# bot buyrug'i emas, balki muayyan bosqichda kutilayotgan maxsus matn
# (masalan AdminChannel.waiting_for_backup_link), shu sabab ularni
# umumiy "/" bilan boshlanuvchi buyruq deb hisoblab bo'lmaydi — aks holda
# holat noto'g'ri tozalanib, o'sha maxsus so'z ishlamay qolardi.
REAL_BOT_COMMANDS = {
    "/start", "/profil", "/izlash",
    "/premium", "/admin", "/bekor",
}

@dp.message.outer_middleware()
async def menu_interrupt_middleware(handler, event: types.Message, data: dict):
    text = event.text or ""
    command_word = text.split()[0].lower() if text else ""
    if text in MENU_INTERRUPT_TEXTS or command_word in REAL_BOT_COMMANDS:
        key = StorageKey(bot_id=bot.id, chat_id=event.chat.id, user_id=event.from_user.id)
        state = FSMContext(storage=dp.storage, key=key)
        if await state.get_state() is not None:
            await state.clear()
    return await handler(event, data)

_db: aiosqlite.Connection = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
    return _db


async def init_db():
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row

    await _db.execute("""
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT,
        title TEXT,
        link TEXT,
        type TEXT DEFAULT 'telegram'
    )""")

    await _db.execute("""
    CREATE TABLE IF NOT EXISTS manual_confirmations (
        user_id INTEGER,
        channel_id TEXT,
        confirmed_at TEXT,
        PRIMARY KEY (user_id, channel_id)
    )""")

    await _db.execute("""
    CREATE TABLE IF NOT EXISTS premium_users (
        user_id INTEGER PRIMARY KEY,
        expire_date TEXT,
        notified INTEGER DEFAULT 0
    )""")

    await _db.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        first_seen TEXT
    )""")

    await _db.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")

    await _db.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        added_at TEXT
    )""")

    # ── TANISHUV BOT PROFILLARI ──
    await _db.execute("""
    CREATE TABLE IF NOT EXISTS profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE,
        gender TEXT,
        name TEXT,
        age INTEGER,
        photo_id TEXT,
        bio TEXT,
        telegram_contact TEXT
    )""")

    # Admin qo'shgan katalog profillari. Oddiy foydalanuvchining `profiles`
    # yozuvidan alohida saqlanadi, shuning uchun admin bir xil Telegram
    # akkauntiga bog'lanmagan istalgan miqdorda profil yarata oladi.
    await _db.execute("""
    CREATE TABLE IF NOT EXISTS admin_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_by INTEGER NOT NULL,
        gender TEXT NOT NULL,
        name TEXT NOT NULL,
        age INTEGER NOT NULL,
        photo_id TEXT NOT NULL,
        bio TEXT NOT NULL,
        telegram_contact TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")

    # Foydalanuvchi allaqachon ko'rgan profillar (qidiruvda takrorlanmasligi uchun)
    await _db.execute("""
    CREATE TABLE IF NOT EXISTS seen_profiles (
        viewer_id INTEGER,
        profile_id INTEGER,
        PRIMARY KEY (viewer_id, profile_id)
    )""")

    # Bot ichidagi suhbat so'rovlari. Profil egasining username'ini
    # oshkor qilmasdan, ikkala foydalanuvchini bot orqali bog'laydi.
    await _db.execute("""
    CREATE TABLE IF NOT EXISTS chat_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        requester_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL
    )""")

    # Faol suhbat ikki qator bilan saqlanadi: har bir ishtirokchi uchun
    # sherigining Telegram ID'si yoziladi.
    await _db.execute("""
    CREATE TABLE IF NOT EXISTS active_chats (
        user_id INTEGER PRIMARY KEY,
        partner_id INTEGER NOT NULL,
        started_at TEXT NOT NULL
    )""")

    await _db.commit()

    migrations = [
        "ALTER TABLE premium_users ADD COLUMN expire_date TEXT",
        "ALTER TABLE premium_users ADD COLUMN notified INTEGER DEFAULT 0",
        "ALTER TABLE channels ADD COLUMN type TEXT DEFAULT 'telegram'",
    ]
    for sql in migrations:
        try:
            await _db.execute(sql)
            await _db.commit()
        except Exception:
            pass

    try:
        async with _db.execute("SELECT value FROM settings WHERE key='premium_price'") as cur:
            old_price = await cur.fetchone()
        if old_price:
            await _db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES ('premium_price_30', ?)",
                (old_price[0],)
            )
            await _db.commit()
    except Exception:
        pass

    # ── DUBLIKAT KANAL YOZUVLARINI TOZALASH ──
    # `channels` jadvalida ilgari channel_id ustida UNIQUE cheklov yo'q edi,
    # shu sababli bir xil kanal bir necha marta qo'shilsa (masalan, avval
    # eskirgan havola bilan, keyin tuzatilgandan keyin yana), ESKI qator
    # o'chmasdan, bazada bir nechta yozuv qolib ketardi — va foydalanuvchiga
    # ba'zan aynan o'sha ESKI (eskirgan) havola ko'rsatilib turardi. Bu yerda
    # har bir channel_id bo'yicha faqat ENG OXIRGI (eng yangi, id'i eng
    # katta) qatorni qoldirib, qolganlarini butunlay o'chiramiz, so'ng shu
    # ustunga UNIQUE indeks qo'yamiz — shunda kelajakda dublikat umuman
    # yaratilmaydi.
    try:
        await _db.execute("""
            DELETE FROM channels
            WHERE id NOT IN (
                SELECT MAX(id) FROM channels GROUP BY channel_id
            )
        """)
        await _db.commit()
        await _db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_channel_id "
            "ON channels(channel_id)"
        )
        await _db.commit()
    except Exception as e:
        logging.warning(f"channels jadvalini tozalab bo'lmadi: {e}")


# ─── FSM HOLATLARI ───────────────────────────────────────────────────────────

class UserRegistration(StatesGroup):
    waiting_for_gender = State()
    waiting_for_age = State()
    waiting_for_photo = State()
    waiting_for_bio = State()
    waiting_for_contact = State()

class AdminChannel(StatesGroup):
    waiting_for_type = State()
    waiting_for_id = State()
    waiting_for_manual_title = State()
    waiting_for_manual_link = State()
    waiting_for_invite_title = State()
    waiting_for_invite_resolve = State()
    waiting_for_backup_link = State()

class AdminPremium(StatesGroup):
    waiting_user_id = State()

class AdminBroadcast(StatesGroup):
    waiting_for_message = State()

class AdminPriceChange(StatesGroup):
    waiting_for_plan = State()
    waiting_for_price = State()

class PaymentReceipt(StatesGroup):
    waiting_for_receipt = State()

class AdminCardChange(StatesGroup):
    waiting_for_number = State()
    waiting_for_holder = State()

class AdminManage(StatesGroup):
    waiting_for_add_id = State()
    waiting_for_remove_id = State()

class AdminProfileAdd(StatesGroup):
    waiting_for_gender = State()
    waiting_for_name = State()
    waiting_for_age = State()
    waiting_for_photo = State()
    waiting_for_bio = State()
    waiting_for_contact = State()

class Conversation(StatesGroup):
    waiting_for_message = State()

class AdminProfileDelete(StatesGroup):
    waiting_for_query = State()

class ProfileEdit(StatesGroup):
    waiting_name = State()
    waiting_age = State()
    waiting_photo = State()
    waiting_bio = State()


# ─── YORDAMCHI FUNKSIYALAR ───────────────────────────────────────────────────

async def is_premium_user(user_id: int) -> bool:
    db = await get_db()
    async with db.execute(
        "SELECT user_id, expire_date FROM premium_users WHERE user_id=?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return False
    if row["expire_date"]:
        expire = datetime.fromisoformat(row["expire_date"])
        if datetime.now() > expire:
            await db.execute("DELETE FROM premium_users WHERE user_id=?", (user_id,))
            await db.commit()
            return False
    return True

async def get_expire_date(user_id: int):
    db = await get_db()
    async with db.execute(
        "SELECT expire_date FROM premium_users WHERE user_id=?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row or not row["expire_date"]:
        return None
    expire = datetime.fromisoformat(row["expire_date"])
    if datetime.now() > expire:
        await db.execute("DELETE FROM premium_users WHERE user_id=?", (user_id,))
        await db.commit()
        return None
    return expire

async def remove_premium(user_id: int):
    db = await get_db()
    await db.execute("DELETE FROM premium_users WHERE user_id=?", (user_id,))
    await db.commit()

async def safe_get_chat_member(chat_id, user_id: int):
    try:
        return await asyncio.wait_for(
            bot.get_chat_member(chat_id=chat_id, user_id=user_id),
            timeout=API_TIMEOUT
        )
    except asyncio.TimeoutError:
        logging.warning(f"get_chat_member timeout: chat={chat_id}, user={user_id}")
        return None
    except Exception as e:
        logging.error(f"get_chat_member xato ({chat_id}): {e}")
        return None

async def safe_get_chat(chat_id):
    try:
        return await asyncio.wait_for(
            bot.get_chat(chat_id),
            timeout=API_TIMEOUT
        )
    except asyncio.TimeoutError:
        logging.warning(f"get_chat timeout: {chat_id}")
        return None
    except Exception as e:
        logging.error(f"get_chat xato ({chat_id}): {e}")
        return None

async def get_channel_link(ch_id: str, link: str) -> str:
    """
    Kanal uchun havola qaytaradi.
    - Ochiq kanal (@username): doim https://t.me/username dan foydalanadi (eskirmaydi)
    - Yopiq kanal: kanal QO'SHILGANDA bir marta yaratilgan (member_limit va
      expire_date berilmagan) taklif havolasi qaytariladi. Bunday havolalar
      Telegram tomonidan o'z-o'zidan eskirmaydi, shuning uchun har bir
      tekshiruvda YANGI havola yaratishga hojat yo'q — aksincha, doimiy
      qayta-yaratish urinishlari Telegram tezlik cheklovi (rate limit)ga
      urilib, tasodifiy xatoliklarga sabab bo'lishi mumkin edi.
    """
    if str(ch_id).startswith("@"):
        username = ch_id.lstrip("@")
        return f"https://t.me/{username}"
    return link

def is_super_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

async def is_bot_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    db = await get_db()
    async with db.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)) as cur:
        return (await cur.fetchone()) is not None

def md_escape(text) -> str:
    """Telegram Markdown v1 uchun maxsus belgilarni escape qiladi."""
    if text is None:
        return ""
    text = str(text)
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text

def build_user_contact(user: types.User) -> tuple[str, str]:
    """
    Foydalanuvchining haqiqiy Telegram akkauntiga aloqa havolasini yaratadi.
    Kiritilgan username qabul qilinmaydi: username akkauntning o'zidan olinadi.
    """
    if user.username:
        return f"https://t.me/{user.username}", f"@{user.username}"
    return f"tg://user?id={user.id}", "username yo'q — profil havolasi avtomatik yaratildi"

async def add_admin(user_id: int, username: str = ""):
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO admins (user_id, username, added_at) VALUES (?, ?, ?)",
        (user_id, username, datetime.now().strftime("%d.%m.%Y %H:%M"))
    )
    await db.commit()

async def remove_admin(user_id: int):
    db = await get_db()
    await db.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    await db.commit()

async def get_admins_list():
    db = await get_db()
    async with db.execute("SELECT user_id, username, added_at FROM admins ORDER BY added_at") as cur:
        return await cur.fetchall()

async def get_card_info():
    db = await get_db()
    async with db.execute("SELECT value FROM settings WHERE key='card_number'") as cur:
        row = await cur.fetchone()
    card_number = row["value"] if row else "Karta raqami hali kiritilmagan"
    async with db.execute("SELECT value FROM settings WHERE key='card_holder'") as cur:
        row2 = await cur.fetchone()
    card_holder = row2["value"] if row2 else ""
    return card_number, card_holder

async def set_card_info(card_number=None, card_holder=None):
    db = await get_db()
    if card_number is not None:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('card_number', ?)",
            (card_number,)
        )
    if card_holder is not None:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('card_holder', ?)",
            (card_holder,)
        )
    await db.commit()

async def add_premium(user_id: int, days: int = PREMIUM_DAYS):
    expire = datetime.now() + timedelta(days=days)
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO premium_users (user_id, expire_date, notified) VALUES (?, ?, 0)",
        (user_id, expire.isoformat())
    )
    await db.commit()
    return expire

async def get_premium_price(days: int = 30) -> str:
    db = await get_db()
    async with db.execute(
        "SELECT value FROM settings WHERE key=?", (f"premium_price_{days}",)
    ) as cur:
        row = await cur.fetchone()
    if row:
        return row["value"]
    async with db.execute("SELECT value FROM settings WHERE key='premium_price'") as cur:
        row2 = await cur.fetchone()
    return row2["value"] if row2 else "50,000"

async def set_premium_price(price: str, days: int = 30):
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (f"premium_price_{days}", price)
    )
    await db.commit()

async def get_user_state(user_id: int) -> FSMContext:
    """Telegram foydalanuvchisi uchun boshqa handlerdan FSM holati olish."""
    return FSMContext(
        storage=dp.storage,
        key=StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id),
    )

async def get_active_chat(user_id: int):
    db = await get_db()
    async with db.execute(
        "SELECT partner_id FROM active_chats WHERE user_id=?", (user_id,)
    ) as cur:
        return await cur.fetchone()

async def end_chat(user_id: int) -> int | None:
    """Faol suhbatni ikkala tomon uchun ham yopadi va sherik ID'sini qaytaradi."""
    db = await get_db()
    async with db.execute(
        "SELECT partner_id FROM active_chats WHERE user_id=?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    partner_id = row["partner_id"] if row else None
    await db.execute("DELETE FROM active_chats WHERE user_id=?", (user_id,))
    if partner_id is not None:
        await db.execute("DELETE FROM active_chats WHERE user_id=?", (partner_id,))
    await db.commit()
    return partner_id

async def start_chat_for_users(first_id: int, second_id: int):
    db = await get_db()
    now = datetime.now().isoformat()
    await db.execute(
        "INSERT OR REPLACE INTO active_chats (user_id, partner_id, started_at) VALUES (?, ?, ?)",
        (first_id, second_id, now),
    )
    await db.execute(
        "INSERT OR REPLACE INTO active_chats (user_id, partner_id, started_at) VALUES (?, ?, ?)",
        (second_id, first_id, now),
    )
    await db.commit()


# ─── KANAL USERNAME TOZALASH ─────────────────────────────────────────────────

def clean_tme_path(raw: str) -> str:
    """
    t.me havolasidan yoki username inputidan sof username ajratib oladi.
    URL-encoded belgilar (%20, %5F va h.k.), ortiqcha /, ? parametrlar,
    bosh-oxirdagi bo'sh joylar barchasini tozalaydi.
    """
    raw = raw.strip()
    raw = unquote(raw)
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if raw.lower().startswith(prefix.lower()):
            raw = raw[len(prefix):]
            break
    raw = raw.split("?")[0].strip("/").strip()
    return raw

def parse_channel_input(raw: str):
    """
    Admin tomonidan kiritilgan kanal input'ini tahlil qiladi.
    Qaytaradi: (chat_id_for_api, full_link_or_none, is_invite)
    """
    raw = raw.strip()
    decoded = unquote(raw)

    is_tme = any(decoded.lower().startswith(p) for p in (
        "https://t.me/", "http://t.me/", "t.me/"
    ))

    if is_tme:
        path = clean_tme_path(decoded)
        full_link = "https://t.me/" + path

        if path.startswith("+") or path.lower().startswith("joinchat/"):
            return None, full_link, True

        username = "@" + path.lstrip("@")
        return username, full_link, False

    stripped = decoded.lstrip("@").strip()
    if re.match(r'^-?\d+$', stripped):
        return int(stripped), None, False

    username = "@" + stripped.lstrip("@")
    return username, None, False


# ─── TUGMALAR ────────────────────────────────────────────────────────────────

def main_menu(user_id: int = 0):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Izlash")],
            [KeyboardButton(text="💬 Suhbatlashish")],
            [KeyboardButton(text="👤 Profil"), KeyboardButton(text="🌟 Premium")],
        ],
        resize_keyboard=True
    )

def navigation_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⬅️ Ortga"), KeyboardButton(text="❌ Bekor qilish")],
        ],
        resize_keyboard=True
    )

def gender_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🙋‍♂️ Yigit"), KeyboardButton(text="🙋‍♀️ Qiz")],
            [KeyboardButton(text="❌ Bekor qilish")],
        ],
        resize_keyboard=True
    )

def optional_bio_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⏭ Bio'ni o'tkazib yuborish")],
            [KeyboardButton(text="⬅️ Ortga"), KeyboardButton(text="❌ Bekor qilish")],
        ],
        resize_keyboard=True
    )

def chat_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="❌ Suhbatni tugatish")],
            [KeyboardButton(text="/bekor")],
        ],
        resize_keyboard=True
    )

def search_gender_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🙋‍♀️ Qizlar"), KeyboardButton(text="🙋‍♂️ Yigitlar")],
            [KeyboardButton(text="🏠 Bosh menyu")],
        ],
        resize_keyboard=True
    )

def profile_inline_kb(p_id: int, target_gender: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💌 Tanishish", callback_data=f"connect_{p_id}")],
        [InlineKeyboardButton(text="➡️ Yana izlash", callback_data=f"nextp_{target_gender}")]
    ])

def admin_menu(user_id: int = 0):
    rows = [
        [KeyboardButton(text="📝 Profil qo'shish")],
        [KeyboardButton(text="🗑 Profilni o'chirish")],
        [KeyboardButton(text="🧹 Voyaga yetmagan profillarni o'chirish")],
        [KeyboardButton(text="📢 Kanal qo'shish"), KeyboardButton(text="🗑 Kanal o'chirish")],
        [KeyboardButton(text="📋 Kanallar ro'yxati")],
        [KeyboardButton(text="👥 Premium berish"), KeyboardButton(text="❌ Premium olish")],
        [KeyboardButton(text="📊 Premium ro'yxati")],
        [KeyboardButton(text="📊 Statistika")],
        [KeyboardButton(text="📣 Xabar yuborish")],
    ]
    if is_super_admin(user_id):
        rows.append([KeyboardButton(text="💰 Premium narxini o'zgartirish")])
        rows.append([KeyboardButton(text="💳 Karta raqamini o'zgartirish")])
        rows.append([KeyboardButton(text="👨‍💼 Adminlar")])
        rows.append([KeyboardButton(text="🗄 Zaxira olish")])
    rows.append([KeyboardButton(text="🏠 Bosh menyu")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ─── MAJBURIY OBUNA TEKSHIRISH ───────────────────────────────────────────────

async def check_subscriptions(user_id: int):
    """
    Foydalanuvchining barcha kanallarga obunasini tekshiradi.
    Qaytaradi: obuna bo'lmagan kanallar ro'yxati.
    """
    db = await get_db()
    async with db.execute("SELECT channel_id, link, title, type FROM channels") as cur:
        channels = await cur.fetchall()

    unsubscribed = []
    for ch in channels:
        ch_id = ch["channel_id"]
        link = ch["link"]
        title = ch["title"]
        ch_type = ch["type"]

        if ch_type in ("bot", "instagram", "manual"):
            async with db.execute(
                "SELECT 1 FROM manual_confirmations WHERE user_id=? AND channel_id=?",
                (user_id, ch_id)
            ) as cur2:
                confirmed = await cur2.fetchone()
            if not confirmed:
                unsubscribed.append((ch_id, link, title, ch_type))
            continue

        # Telegram kanal/guruh — API orqali tekshirish
        if str(ch_id).lstrip("-").isdigit():
            chat_id = int(ch_id)
        else:
            chat_id = ch_id  # @username ko'rinishida

        member = await safe_get_chat_member(chat_id, user_id)
        is_subscribed = member is not None and member.status not in ("left", "kicked")

        if not is_subscribed:
            # Kanalda "Yangi a'zolarni tasdiqlash" yoqilgan bo'lishi mumkin —
            # bunday holda foydalanuvchi hali rasman a'zo emas (admin
            # tasdiqlashini kutmoqda), lekin qo'shilish SO'ROVINI yuborgan
            # bo'lsa, buni yetarli deb hisoblaymiz.
            async with db.execute(
                "SELECT 1 FROM manual_confirmations WHERE user_id=? AND channel_id=?",
                (user_id, ch_id)
            ) as cur2:
                requested = await cur2.fetchone()
            if requested:
                is_subscribed = True

        if not is_subscribed:
            unsubscribed.append((ch_id, link, title, ch_type))

    return unsubscribed

async def build_subscription_keyboard(unsub) -> InlineKeyboardMarkup:
    """
    Faqat obuna bo'linmagan kanallar ko'rsatiladi.
    Faqat "✅ Tekshirish" va "🌟 Premium" tugmalari qoladi — "Bosdim" yo'q.
    """
    buttons = []
    for ch_id, link, title, ch_type in unsub:
        if ch_type in ("bot", "instagram", "manual"):
            icon = "🤖" if ch_type == "bot" else ("📸" if ch_type == "instagram" else "🔗")
            if link:
                buttons.append([InlineKeyboardButton(text=f"{icon} {title}", url=link)])
            else:
                buttons.append([InlineKeyboardButton(
                    text=f"{icon} {title}", callback_data="noop"
                )])
        else:
            # Telegram kanal: havola olish (public => username URL, private => yangi invite)
            real_link = await get_channel_link(ch_id, link)
            if real_link:
                buttons.append([InlineKeyboardButton(text=f"📢 {title}", url=real_link)])
            else:
                buttons.append([InlineKeyboardButton(
                    text=f"📢 {title}", callback_data="noop"
                )])
    # Faqat "Tekshirish" va "Premium" tugmalari
    buttons.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub")])
    buttons.append([InlineKeyboardButton(
        text="🌟 Premium tarifga obuna bo'lish", callback_data="req_premium"
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─── FON VAZIFALAR ───────────────────────────────────────────────────────────

async def premium_checker():
    while True:
        await asyncio.sleep(12 * 3600)
        try:
            db = await get_db()
            async with db.execute(
                "SELECT user_id, expire_date FROM premium_users"
            ) as cur:
                all_users = await cur.fetchall()
            now = datetime.now()
            notify_threshold = now + timedelta(days=NOTIFY_BEFORE_DAYS)
            for row in all_users:
                uid = row["user_id"]
                expire_str = row["expire_date"]
                if not expire_str:
                    continue
                expire = datetime.fromisoformat(expire_str)
                if now > expire:
                    await db.execute("DELETE FROM premium_users WHERE user_id=?", (uid,))
                    await db.commit()
                    try:
                        await bot.send_message(
                            uid,
                            "⏰ *Premium obunangiz muddati tugadi.*\n\n"
                            "Davom ettirish uchun 🌟 *Premium* bo'limiga o'ting.",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                elif expire <= notify_threshold:
                    async with db.execute(
                        "SELECT notified FROM premium_users WHERE user_id=?", (uid,)
                    ) as cur2:
                        nrow = await cur2.fetchone()
                    if nrow and nrow["notified"] == 0:
                        days_left = (expire - now).days + 1
                        await db.execute(
                            "UPDATE premium_users SET notified=1 WHERE user_id=?", (uid,)
                        )
                        await db.commit()
                        try:
                            await bot.send_message(
                                uid,
                                f"⚠️ *Diqqat!* Premium obunangiz *{days_left} kun* ichida tugaydi.\n\n"
                                f"📅 Tugash sanasi: *{expire.strftime('%d.%m.%Y')}*\n\n"
                                f"Uzaytirish uchun @{md_escape(ADMIN_USERNAME)} bilan bog'laning.",
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass
        except Exception as e:
            logging.error(f"Premium checker xatosi: {e}")

async def backup_scheduler():
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            db = await get_db()
            await db.commit()
            backup_name = f"backup_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.db"
            await bot.send_document(
                chat_id=ADMIN_ID,
                document=types.FSInputFile(DB_PATH, filename=backup_name),
                caption=f"🗄 Avtomatik zaxira nusxa\n📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            )
        except Exception as e:
            logging.error(f"Zaxira yuborishda xato: {e}")


async def refresh_private_channel_links():
    """
    Yopiq (raqamli ID) Telegram kanallar uchun bazada saqlangan taklif
    havolasini YANGI havola bilan almashtiradi. Bu funksiya muntazam
    (avtomatik) chaqiriladi — shunda foydalanuvchilarga hech qachon
    eskirgan yoki bekor qilingan ("Expired Link") havola ko'rsatilmaydi,
    admin qo'lda "🔄 Yangilash" tugmasini bosishini kutish shart bo'lmaydi.
    Ochiq (@username) kanallarga tegilmaydi — ular eskirmaydi.
    """
    db = await get_db()
    async with db.execute(
        "SELECT channel_id FROM channels WHERE type='telegram'"
    ) as cur:
        rows = await cur.fetchall()

    for row in rows:
        ch_id = row["channel_id"]
        if str(ch_id).startswith("@"):
            continue
        try:
            chat_id_int = int(ch_id)
        except ValueError:
            continue
        try:
            invite = await asyncio.wait_for(
                bot.create_chat_invite_link(chat_id_int, creates_join_request=True),
                timeout=API_TIMEOUT
            )
            await db.execute(
                "UPDATE channels SET link=? WHERE channel_id=?",
                (invite.invite_link, ch_id)
            )
            await db.commit()
            logging.info(f"Kanal havolasi avtomatik yangilandi: {ch_id}")
        except Exception as e:
            logging.warning(f"Havolani avtomatik yangilab bo'lmadi ({ch_id}): {e}")

async def link_refresh_scheduler():
    """
    Ishga tushganda darhol, so'ngra har 6 soatda bir marta barcha yopiq
    kanallar havolasini yangilab turadi.
    """
    while True:
        try:
            await refresh_private_channel_links()
        except Exception as e:
            logging.error(f"link_refresh_scheduler xatosi: {e}")
        await asyncio.sleep(6 * 3600)


# ─── ZAXIRA OLISH ────────────────────────────────────────────────────────────

@dp.message(F.text == "🗄 Zaxira olish", StateFilter("*"))
async def manual_backup(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    await state.clear()
    try:
        db = await get_db()
        await db.commit()
        backup_name = f"backup_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.db"
        await message.answer_document(
            document=types.FSInputFile(DB_PATH, filename=backup_name),
            caption=f"🗄 Qo'lda olingan zaxira\n📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        )
    except Exception as e:
        await message.answer(f"❌ Zaxira olishda xato: {e}")


# ─── PROFIL BOR-YO'QLIGINI TEKSHIRISH ────────────────────────────────────────

async def has_profile(user_id: int) -> bool:
    db = await get_db()
    async with db.execute("SELECT 1 FROM profiles WHERE user_id = ?", (user_id,)) as cur:
        return (await cur.fetchone()) is not None

async def enter_app(message: types.Message, state: FSMContext, user_id: int):
    """Obuna/premium tekshiruvidan o'tgach chaqiriladi: profil bo'lsa asosiy
    menyu, bo'lmasa ro'yxatdan o'tishni boshlaydi."""
    if await has_profile(user_id):
        await message.answer("Xush kelibsiz! Asosiy menyu:", reply_markup=main_menu(user_id))
    else:
        await state.clear()
        await message.answer(
            "Xush kelibsiz! Tanishuv botidan foydalanish uchun avval profil "
            "yaratishingiz kerak.\n\nBotdan foydalanish uchun 18 yoshdan katta "
            "bo'lishingiz shart.\n\n"
            "Jinsingizni tanlang.\n\nBekor qilish: /bekor",
            reply_markup=gender_keyboard()
        )
        await state.set_state(UserRegistration.waiting_for_gender)


# ─── START ───────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def start_cmd(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    db = await get_db()

    await db.execute(
        "INSERT OR IGNORE INTO users (user_id, first_seen) VALUES (?, ?)",
        (user_id, datetime.now().isoformat())
    )
    await db.commit()

    if not await is_premium_user(user_id):
        unsub = await check_subscriptions(user_id)
        if unsub:
            kb = await build_subscription_keyboard(unsub)
            await message.answer(
                "⚠️ Botdan foydalanish uchun quyidagi kanal(lar)ga obuna bo'ling:\n\n"
                "ℹ️ _Premium a'zolarga majburiy kanal obunasi talab qilinmaydi!_",
                reply_markup=kb, parse_mode="Markdown"
            )
            return

    await enter_app(message, state, user_id)

@dp.callback_query(F.data == "noop")
async def noop_cb(call: types.CallbackQuery):
    await call.answer("🔒 Bu maxfiy kanal. Admin orqali qo'shiling.", show_alert=True)

@dp.callback_query(F.data == "check_sub")
async def check_sub_cb(call: types.CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    if await is_premium_user(user_id):
        try:
            await call.message.delete()
        except Exception:
            pass
        await enter_app(call.message, state, user_id)
        await call.answer()
        return

    # bot/instagram/manual kanallar uchun manual tasdiq (API orqali tekshirib bo'lmaydi)
    db = await get_db()
    async with db.execute("SELECT channel_id, type FROM channels") as cur:
        all_channels = await cur.fetchall()
    for ch in all_channels:
        if ch["type"] in ("bot", "instagram", "manual"):
            await db.execute(
                "INSERT OR REPLACE INTO manual_confirmations "
                "(user_id, channel_id, confirmed_at) VALUES (?, ?, ?)",
                (user_id, ch["channel_id"], datetime.now().isoformat())
            )
    await db.commit()

    unsub = await check_subscriptions(user_id)
    if unsub:
        # Faqat hali obuna bo'linmagan kanallarni ko'rsat
        kb = await build_subscription_keyboard(unsub)
        try:
            await call.message.edit_text(
                "⚠️ Quyidagi kanal(lar)ga hali obuna bo'lmadingiz:\n\n"
                "ℹ️ _Premium a'zolarga majburiy kanal obunasi talab qilinmaydi!_",
                reply_markup=kb,
                parse_mode="Markdown"
            )
        except Exception:
            await call.message.answer(
                "⚠️ Quyidagi kanal(lar)ga hali obuna bo'lmadingiz:",
                reply_markup=kb
            )
        await call.answer("❌ Hali barcha kanallarga obuna bo'lmadingiz!", show_alert=True)
    else:
        try:
            await call.message.delete()
        except Exception:
            pass
        await enter_app(call.message, state, user_id)
        await call.answer()


# ─── PREMIUM BO'LIM ──────────────────────────────────────────────────────────
_UNUSED_PREMIUM_LIST_START = None
@dp.message(F.text == "🌟 Premium", StateFilter(None))
async def premium_info(message: types.Message):
    user_id = message.from_user.id
    price = await get_premium_price(30)

    if await is_premium_user(user_id):
        expire = await get_expire_date(user_id)
        expire_str = expire.strftime("%d.%m.%Y") if expire else "Noma'lum"
        await message.answer(
            f"🌟 *Siz Premium a'zosiz!*\n📅 Muddat: *{expire_str}* gacha\n\n"
            f"Endi istalgan profil egasi bilan \"💌 Tanishish\" tugmasi orqali "
            f"bevosita bog'lanishingiz mumkin.",
            parse_mode="Markdown"
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💳 Karta orqali to'lash ({price} so'm)",
            callback_data="pay_card"
        )],
        [InlineKeyboardButton(
            text="👨‍💻 Admin bilan bog'lanish",
            url=f"https://t.me/{ADMIN_USERNAME}"
        )],
    ])
    await message.answer(
        f"🌟 *Premium a'zolik*\n\n"
        f"✅ *Afzalliklar:*\n"
        f"• Istalgan profil egasi bilan bevosita bog'lanish (\"💌 Tanishish\")\n"
        f"• Majburiy kanal obunasisiz foydalanish\n\n"
        f"💰 *Narxi:* {price} so'm / {PREMIUM_DAYS} kun\n\n"
        f"👨‍💻 *Admin:* @{md_escape(ADMIN_USERNAME)}\n\n"
        f"To'lov usulini tanlang:",
        parse_mode="Markdown", reply_markup=kb
    )


# ─── TO'LOV ───────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "req_premium")
async def req_premium_cb(call: types.CallbackQuery):
    price = await get_premium_price(30)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💳 Karta orqali to'lash ({price} so'm)",
            callback_data="pay_card"
        )],
        [InlineKeyboardButton(
            text="👨‍💻 Admin bilan bog'lanish",
            url=f"https://t.me/{ADMIN_USERNAME}"
        )],
    ])
    await call.message.answer(
        f"🌟 *Premium a'zolik — {price} so'm / {PREMIUM_DAYS} kun*\n\n"
        f"Qaysi usul orqali to'lamoqchisiz?",
        parse_mode="Markdown", reply_markup=kb
    )
    await call.answer()

@dp.callback_query(F.data == "pay_card")
async def pay_card_cb(call: types.CallbackQuery, state: FSMContext):
    card_number, card_holder = await get_card_info()
    holder_line = f"\n👤 *Karta egasi:* {md_escape(card_holder)}" if card_holder else ""
    price = await get_premium_price(30)
    await state.set_state(PaymentReceipt.waiting_for_receipt)
    await call.message.answer(
        f"💳 *Premium uchun to'lov*\n\n"
        f"💳 Karta raqami: `{card_number}`{holder_line}\n"
        f"💰 Summasi: *{price} so'm* / {PREMIUM_DAYS} kun\n\n"
        f"1️⃣ Yuqoridagi kartaga to'lovni amalga oshiring.\n"
        f"2️⃣ To'lov chekining *rasmini (screenshot)* shu yerga yuboring.\n\n"
        f"⚠️ *Eslatma:* Chekni tashlamasangiz, Premium berilmaydi!",
        parse_mode="Markdown"
    )
    await call.answer()

@dp.message(PaymentReceipt.waiting_for_receipt, F.photo)
async def receive_payment_receipt(message: types.Message, state: FSMContext):
    user = message.from_user
    uname = f"@{md_escape(user.username)}" if user.username else "username yo'q"
    full_name_safe = md_escape(user.full_name)
    sent_time = datetime.now().strftime("%d.%m.%Y %H:%M")
    kb_admin = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Tasdiqlash", callback_data=f"approve_prem_{user.id}"
        ),
        InlineKeyboardButton(
            text="❌ Rad etish", callback_data=f"reject_prem_{user.id}"
        )
    ]])
    try:
        await bot.send_photo(
            chat_id=ADMIN_ID,
            photo=message.photo[-1].file_id,
            caption=(
                f"🧾 *Yangi to'lov cheki!*\n\n"
                f"👤 Ism: {full_name_safe}\n"
                f"🔗 Username: {uname}\n"
                f"🆔 ID: `{user.id}`\n"
                f"🕒 Yuborilgan vaqt: {sent_time}\n\n"
                f"✅ Tasdiqlasangiz *{PREMIUM_DAYS} kun* premium beriladi."
            ),
            reply_markup=kb_admin,
            parse_mode="Markdown"
        )
        await message.answer(
            "✅ Chekingiz qabul qilindi va adminga yuborildi!\nTekshirilgach, Premium tasdiqlanadi."
        )
    except Exception:
        await message.answer(
            f"❌ Xatolik yuz berdi. Iltimos, chekni to'g'ridan-to'g'ri adminga yuboring: "
            f"@{ADMIN_USERNAME}"
        )
    await state.clear()

@dp.message(PaymentReceipt.waiting_for_receipt)
async def receipt_wrong_format(message: types.Message):
    await message.answer(
        "⚠️ Iltimos, to'lov chekining *rasmini (screenshot)* yuboring — "
        "matn qabul qilinmaydi.",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("approve_prem_"))
async def approve_premium(call: types.CallbackQuery):
    target_id = int(call.data.split("_")[2])
    expire = await add_premium(target_id, PREMIUM_DAYS)
    expire_str = expire.strftime("%d.%m.%Y")
    await call.message.edit_text(
        f"✅ Foydalanuvchi `{target_id}` Premium a'zolikka qo'shildi!\n"
        f"📅 Muddat: *{expire_str}* gacha",
        parse_mode="Markdown"
    )
    try:
        await bot.send_message(
            target_id,
            f"🎉 *Tabriklaymiz!* Premium obunangiz tasdiqlandi.\n\n"
            f"📅 *Muddat:* {PREMIUM_DAYS} kun ({expire_str} gacha)\n\n"
            f"🌟 Endi \"💌 Tanishish\" tugmasi orqali bevosita bog'lanishingiz mumkin!",
            parse_mode="Markdown"
        )
    except Exception:
        pass

@dp.callback_query(F.data.startswith("reject_prem_"))
async def reject_premium(call: types.CallbackQuery):
    target_id = int(call.data.split("_")[2])
    await call.message.edit_text(
        f"❌ Foydalanuvchi `{target_id}` so'rovi rad etildi.",
        parse_mode="Markdown"
    )
    try:
        await bot.send_message(
            target_id,
            "❌ Afsuski, to'lovingiz tasdiqlanmadi.\n\n"
            "Muammo bo'lsa, adminimizga murojaat qiling."
        )
    except Exception:
        pass


# ─── RO'YXATDAN O'TISH (FOYDALANUVCHI PROFILI) ───────────────────────────────

# Tanishuv xizmati faqat voyaga yetganlar (18+) uchun.
MIN_AGE = 18
MAX_AGE = 90

@dp.message(UserRegistration.waiting_for_gender, F.text.in_(["🙋‍♂️ Yigit", "🙋‍♀️ Qiz"]))
async def process_gender(message: types.Message, state: FSMContext):
    gender = "male" if "Yigit" in message.text else "female"
    await state.update_data(gender=gender)
    await state.set_state(UserRegistration.waiting_for_age)
    await message.answer(
        "Yoshingizni kiriting (masalan: 20):\n\nBekor qilish: /bekor",
        reply_markup=navigation_keyboard(),
    )

@dp.message(UserRegistration.waiting_for_gender)
async def process_gender_invalid(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Ortga":
        await state.clear()
        await message.answer(
            "🏠 Profil yaratish bekor qilindi.",
            reply_markup=main_menu(message.from_user.id),
        )
        return
    await message.answer("Iltimos, tugmalardan birini tanlang.", reply_markup=gender_keyboard())

@dp.message(UserRegistration.waiting_for_age)
async def process_age(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Ortga":
        await state.set_state(UserRegistration.waiting_for_gender)
        await message.answer("Jinsingizni tanlang:", reply_markup=gender_keyboard())
        return
    if not message.text or not message.text.isdigit():
        await message.answer("❌ Iltimos, faqat raqam kiriting (masalan: 20):")
        return
    age = int(message.text)
    if age < MIN_AGE:
        await message.answer(f"❌ Botdan foydalanish uchun kamida {MIN_AGE} yoshda bo'lishingiz kerak.")
        return
    if age > MAX_AGE:
        await message.answer("❌ Iltimos, to'g'ri yosh kiriting.")
        return
    await state.update_data(age=age)
    await state.set_state(UserRegistration.waiting_for_photo)
    await message.answer(
        "📸 Profilingiz uchun rasmingizni yuboring.\n\nBekor qilish: /bekor",
        reply_markup=navigation_keyboard(),
    )

@dp.message(UserRegistration.waiting_for_photo, F.photo)
async def process_photo(message: types.Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(photo_id=photo_id)
    await state.set_state(UserRegistration.waiting_for_bio)
    await message.answer(
        "📝 O'zingiz haqingizda qisqacha ma'lumot kiriting (Bio) yoki o'tkazib yuboring.\n\n"
        "Bekor qilish: /bekor",
        reply_markup=optional_bio_keyboard()
    )

@dp.message(UserRegistration.waiting_for_photo)
async def process_photo_invalid(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Ortga":
        await state.set_state(UserRegistration.waiting_for_age)
        await message.answer(
            "Yoshingizni kiriting (masalan: 20):",
            reply_markup=navigation_keyboard(),
        )
        return
    await message.answer(
        "❌ Iltimos, rasm (foto) yuboring, matn emas.\n"
        "Ortga qaytish yoki bekor qilish uchun pastdagi tugmalardan foydalaning.",
        reply_markup=navigation_keyboard(),
    )

@dp.message(UserRegistration.waiting_for_bio)
async def process_bio(message: types.Message, state: FSMContext):
    bio = (message.text or "").strip()
    if bio == "⬅️ Ortga":
        await state.set_state(UserRegistration.waiting_for_photo)
        await message.answer(
            "📸 Profilingiz uchun rasm yuboring:",
            reply_markup=navigation_keyboard(),
        )
        return
    if bio in {"⏭ Bio'ni o'tkazib yuborish", "/skip", "skip", "o'tkazib yuborish"}:
        bio = ""
    elif not bio:
        await message.answer(
            "❌ Bio matn bo'lishi kerak yoki quyidagi tugma orqali o'tkazib yuboring:",
            reply_markup=optional_bio_keyboard()
        )
        return
    if len(bio) > 500:
        await message.answer("❌ Bio juda uzun. 500 belgidan qisqaroq yozing:")
        return
    await state.update_data(bio=bio)
    data = await state.get_data()
    name = message.from_user.first_name
    contact, contact_label = build_user_contact(message.from_user)

    db = await get_db()
    await db.execute("""
        INSERT INTO profiles (user_id, gender, name, age, photo_id, bio, telegram_contact)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
        gender=excluded.gender, name=excluded.name, age=excluded.age,
        photo_id=excluded.photo_id, bio=excluded.bio, telegram_contact=excluded.telegram_contact
    """, (message.from_user.id, data['gender'], name, data['age'], data['photo_id'], data['bio'], contact))
    await db.commit()

    await state.clear()
    await message.answer(
        "✅ Profilingiz muvaffaqiyatli yaratildi!\n\n"
        f"🔗 {contact_label.capitalize()}.\n"
        "Bu ma'lumot Telegram akkauntingizdan avtomatik olindi.",
        reply_markup=main_menu(message.from_user.id),
    )

@dp.message(UserRegistration.waiting_for_contact)
async def process_contact(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if message.text == "⬅️ Ortga":
        await state.set_state(UserRegistration.waiting_for_bio)
        await message.answer(
            "📝 Bio yozing yoki o'tkazib yuboring:",
            reply_markup=optional_bio_keyboard(),
        )
        return
    contact, contact_label = build_user_contact(message.from_user)
    name = message.from_user.first_name

    db = await get_db()
    await db.execute("""
        INSERT INTO profiles (user_id, gender, name, age, photo_id, bio, telegram_contact)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
        gender=excluded.gender, name=excluded.name, age=excluded.age,
        photo_id=excluded.photo_id, bio=excluded.bio, telegram_contact=excluded.telegram_contact
    """, (message.from_user.id, data['gender'], name, data['age'], data['photo_id'], data['bio'], contact))
    await db.commit()

    await state.clear()
    await message.answer(
        "✅ Profilingiz muvaffaqiyatli yaratildi!\n\n"
        f"🔗 {contact_label.capitalize()}.\n"
        "Bu ma'lumot Telegram akkauntingizdan avtomatik olindi.",
        reply_markup=main_menu(message.from_user.id),
    )


# ─── MENING PROFILIM ─────────────────────────────────────────────────────────

def profile_view_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Profilni tahrirlash", callback_data="profile_edit")]
    ])

def profile_edit_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Ism", callback_data="profile_edit_name"),
         InlineKeyboardButton(text="🎂 Yosh", callback_data="profile_edit_age")],
        [InlineKeyboardButton(text="📸 Rasm", callback_data="profile_edit_photo"),
         InlineKeyboardButton(text="📝 Izoh (bio)", callback_data="profile_edit_bio")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="profile_edit_back")],
    ])

def cancel_only_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Bekor qilish")]],
        resize_keyboard=True
    )

async def send_my_profile(message: types.Message, user_id: int):
    db = await get_db()
    async with db.execute(
        "SELECT name, age, bio, photo_id, telegram_contact FROM profiles WHERE user_id = ?",
        (user_id,)
    ) as cur:
        profile = await cur.fetchone()

    if not profile:
        await message.answer("Sizda hali profil yo'q. /start bosing.")
        return

    is_prem = "💎 Premium" if await is_premium_user(user_id) else "Oddiy"
    contact = profile["telegram_contact"]
    contact_line = (
        f"🔗 *Aloqa:* [Profilga o'tish]({contact})"
        if contact
        else "🔗 *Aloqa:* mavjud emas"
    )
    bio_text = md_escape(profile["bio"]) if profile["bio"] else "—"
    caption = (
        f"👤 *Ism:* {md_escape(profile['name'])}\n"
        f"🎂 *Yosh:* {profile['age']}\n"
        f"📝 *Bio:* {bio_text}\n"
        f"{contact_line}\n"
        f"🏷 *Status:* {is_prem}"
    )
    await message.answer_photo(
        photo=profile['photo_id'], caption=caption, parse_mode="Markdown",
        reply_markup=profile_view_kb()
    )

@dp.message(F.text.in_(["👤 Profil", "👤 Mening profilim"]), StateFilter(None))
async def my_profile(message: types.Message):
    await send_my_profile(message, message.from_user.id)


# ─── PROFILNI TAHRIRLASH ─────────────────────────────────────────────────────

async def _edit_guard(call: types.CallbackQuery) -> bool:
    """Profil borligini va faol suhbatda emasligini tekshiradi."""
    if not await has_profile(call.from_user.id):
        await call.answer("Sizda hali profil yo'q. /start bosing.", show_alert=True)
        return False
    if await get_active_chat(call.from_user.id):
        await call.answer("Avval suhbatni tugating.", show_alert=True)
        return False
    return True

@dp.callback_query(F.data == "profile_edit")
async def profile_edit_cb(call: types.CallbackQuery):
    if not await _edit_guard(call):
        return
    try:
        await call.message.edit_reply_markup(reply_markup=profile_edit_menu_kb())
    except Exception:
        pass
    await call.answer("Nimani tahrirlaymiz?")

@dp.callback_query(F.data == "profile_edit_back")
async def profile_edit_back_cb(call: types.CallbackQuery):
    try:
        await call.message.edit_reply_markup(reply_markup=profile_view_kb())
    except Exception:
        pass
    await call.answer()

@dp.callback_query(F.data == "profile_edit_name")
async def profile_edit_name_cb(call: types.CallbackQuery, state: FSMContext):
    if not await _edit_guard(call):
        return
    await state.set_state(ProfileEdit.waiting_name)
    await call.message.answer(
        "👤 Yangi ismingizni yuboring:\n\nBekor qilish: /bekor",
        reply_markup=cancel_only_keyboard()
    )
    await call.answer()

@dp.callback_query(F.data == "profile_edit_age")
async def profile_edit_age_cb(call: types.CallbackQuery, state: FSMContext):
    if not await _edit_guard(call):
        return
    await state.set_state(ProfileEdit.waiting_age)
    await call.message.answer(
        f"🎂 Yangi yoshingizni yuboring ({MIN_AGE}–{MAX_AGE}):\n\nBekor qilish: /bekor",
        reply_markup=cancel_only_keyboard()
    )
    await call.answer()

@dp.callback_query(F.data == "profile_edit_photo")
async def profile_edit_photo_cb(call: types.CallbackQuery, state: FSMContext):
    if not await _edit_guard(call):
        return
    await state.set_state(ProfileEdit.waiting_photo)
    await call.message.answer(
        "📸 Yangi rasmingizni yuboring:\n\nBekor qilish: /bekor",
        reply_markup=cancel_only_keyboard()
    )
    await call.answer()

@dp.callback_query(F.data == "profile_edit_bio")
async def profile_edit_bio_cb(call: types.CallbackQuery, state: FSMContext):
    if not await _edit_guard(call):
        return
    await state.set_state(ProfileEdit.waiting_bio)
    await call.message.answer(
        "📝 Yangi izoh (bio) yuboring. Izohni butunlay o'chirish uchun "
        "\"⏭ Bio'ni o'tkazib yuborish\" tugmasini bosing.\n\nBekor qilish: /bekor",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="⏭ Bio'ni o'tkazib yuborish")],
                [KeyboardButton(text="❌ Bekor qilish")],
            ],
            resize_keyboard=True
        )
    )
    await call.answer()

async def _finish_profile_edit(message: types.Message, state: FSMContext, note: str):
    await state.clear()
    await message.answer(note, reply_markup=main_menu(message.from_user.id))
    await send_my_profile(message, message.from_user.id)

@dp.message(ProfileEdit.waiting_name)
async def profile_edit_name_save(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name or name.startswith("/") or len(name) > 100:
        await message.answer("❌ Ism 1–100 belgi bo'lishi kerak. Qaytadan yuboring:")
        return
    db = await get_db()
    await db.execute("UPDATE profiles SET name=? WHERE user_id=?", (name, message.from_user.id))
    await db.commit()
    await _finish_profile_edit(message, state, "✅ Ism yangilandi!")

@dp.message(ProfileEdit.waiting_age)
async def profile_edit_age_save(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("❌ Iltimos, faqat raqam kiriting (masalan: 20):")
        return
    age = int(text)
    if age < MIN_AGE:
        await message.answer(f"❌ Botdan foydalanish uchun kamida {MIN_AGE} yoshda bo'lishingiz kerak.")
        return
    if age > MAX_AGE:
        await message.answer("❌ Iltimos, to'g'ri yosh kiriting.")
        return
    db = await get_db()
    await db.execute("UPDATE profiles SET age=? WHERE user_id=?", (age, message.from_user.id))
    await db.commit()
    await _finish_profile_edit(message, state, "✅ Yosh yangilandi!")

@dp.message(ProfileEdit.waiting_photo, F.photo)
async def profile_edit_photo_save(message: types.Message, state: FSMContext):
    db = await get_db()
    await db.execute(
        "UPDATE profiles SET photo_id=? WHERE user_id=?",
        (message.photo[-1].file_id, message.from_user.id)
    )
    await db.commit()
    await _finish_profile_edit(message, state, "✅ Rasm yangilandi!")

@dp.message(ProfileEdit.waiting_photo)
async def profile_edit_photo_invalid(message: types.Message):
    await message.answer(
        "❌ Iltimos, rasm (foto) yuboring, matn emas.",
        reply_markup=cancel_only_keyboard()
    )

@dp.message(ProfileEdit.waiting_bio)
async def profile_edit_bio_save(message: types.Message, state: FSMContext):
    bio = (message.text or "").strip()
    if bio in {"⏭ Bio'ni o'tkazib yuborish", "/skip", "skip", "o'tkazib yuborish"}:
        bio = ""
    elif not bio:
        await message.answer("❌ Izoh matn bo'lishi kerak.")
        return
    if len(bio) > 500:
        await message.answer("❌ Izoh juda uzun. 500 belgidan qisqaroq yozing:")
        return
    db = await get_db()
    await db.execute("UPDATE profiles SET bio=? WHERE user_id=?", (bio, message.from_user.id))
    await db.commit()
    await _finish_profile_edit(message, state, "✅ Izoh yangilandi!")


# ─── IZLASH TIZIMI ───────────────────────────────────────────────────────────

async def get_next_profile(db, viewer_id: int, target_gender: str):
    """
    Oddiy foydalanuvchi profillari va admin katalog profillaridan
    ko'rilmagan tasodifiy profilni qaytaradi.

    Admin profillarining ID'si manfiy ko'rinishda qaytariladi. Bu oddiy
    `profiles.id` bilan to'qnashmasdan mavjud `seen_profiles` jadvalida
    ishlash va callback orqali qaysi jadvaldan o'qishni ajratish imkonini beradi.
    """
    async with db.execute(
        """SELECT id, name, age, bio, photo_id FROM (
           SELECT id, name, age, bio, photo_id FROM profiles
           WHERE gender = ? AND user_id != ?
           AND id NOT IN (SELECT profile_id FROM seen_profiles WHERE viewer_id = ?)
           UNION ALL
           SELECT -id AS id, name, age, bio, photo_id FROM admin_profiles
           WHERE gender = ?
           AND (-id) NOT IN (SELECT profile_id FROM seen_profiles WHERE viewer_id = ?)
        ) ORDER BY RANDOM() LIMIT 1""",
        (target_gender, viewer_id, viewer_id, target_gender, viewer_id)
    ) as cur:
        profile = await cur.fetchone()

    if not profile:
        await db.execute(
            """DELETE FROM seen_profiles WHERE viewer_id = ? AND profile_id IN
               (SELECT id FROM profiles WHERE gender = ?
                UNION ALL
                SELECT -id FROM admin_profiles WHERE gender = ?)""",
            (viewer_id, target_gender, target_gender)
        )
        await db.commit()
        async with db.execute(
            """SELECT id, name, age, bio, photo_id FROM (
               SELECT id, name, age, bio, photo_id FROM profiles
               WHERE gender = ? AND user_id != ?
               UNION ALL
               SELECT -id AS id, name, age, bio, photo_id FROM admin_profiles
               WHERE gender = ?
            ) ORDER BY RANDOM() LIMIT 1""",
            (target_gender, viewer_id, target_gender)
        ) as cur:
            profile = await cur.fetchone()

    return profile

@dp.message(F.text == "🔍 Izlash", StateFilter(None))
async def search_start(message: types.Message):
    if not await has_profile(message.from_user.id):
        await message.answer("Avval profil yarating: /start")
        return
    await message.answer("Kimni qidiryapsiz?", reply_markup=search_gender_keyboard())

@dp.message(F.text.in_(["🙋‍♀️ Qizlar", "🙋‍♂️ Yigitlar"]), StateFilter(None))
async def show_profile(message: types.Message):
    target_gender = "female" if "Qizlar" in message.text else "male"
    db = await get_db()
    profile = await get_next_profile(db, message.from_user.id, target_gender)

    if not profile:
        await message.answer("Hozircha bu bo'limda profillar mavjud emas.", reply_markup=main_menu(message.from_user.id))
        return

    p_id = profile['id']
    await db.execute(
        "INSERT OR IGNORE INTO seen_profiles (viewer_id, profile_id) VALUES (?, ?)",
        (message.from_user.id, p_id)
    )
    await db.commit()

    caption = f"👤 *Ism:* {md_escape(profile['name'])}\n🎂 *Yosh:* {profile['age']}\n📝 *Haqida:* {md_escape(profile['bio'])}"
    await message.answer_photo(
        photo=profile['photo_id'], caption=caption, parse_mode="Markdown",
        reply_markup=profile_inline_kb(p_id, target_gender)
    )

@dp.callback_query(F.data.startswith("nextp_"))
async def next_profile_cb(call: types.CallbackQuery):
    target_gender = call.data.split("_", 1)[1]
    db = await get_db()
    profile = await get_next_profile(db, call.from_user.id, target_gender)

    if not profile:
        await call.answer("Boshqa profil topilmadi!", show_alert=True)
        return

    p_id = profile['id']
    await db.execute(
        "INSERT OR IGNORE INTO seen_profiles (viewer_id, profile_id) VALUES (?, ?)",
        (call.from_user.id, p_id)
    )
    await db.commit()

    caption = f"👤 *Ism:* {md_escape(profile['name'])}\n🎂 *Yosh:* {profile['age']}\n📝 *Haqida:* {md_escape(profile['bio'])}"
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer_photo(
        photo=profile['photo_id'], caption=caption, parse_mode="Markdown",
        reply_markup=profile_inline_kb(p_id, target_gender)
    )
    await call.answer()

@dp.callback_query(F.data.startswith("connect_"))
async def connect_profile_cb(call: types.CallbackQuery):
    profile_id = int(call.data.split("_", 1)[1])
    user_id = call.from_user.id

    if not await is_premium_user(user_id):
        await call.answer("🔒 Tanishish uchun sizda PREMIUM obuna bo'lishi kerak!", show_alert=True)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌟 Premium olish", callback_data="req_premium")]
        ])
        await call.message.answer(
            "⭐ Profil egasi bilan bog'lanish uchun Premium obunani faollashtiring.",
            reply_markup=kb
        )
        return

    db = await get_db()
    if profile_id < 0:
        contact_query = "SELECT telegram_contact FROM admin_profiles WHERE id = ?"
        contact_id = -profile_id
    else:
        contact_query = "SELECT telegram_contact FROM profiles WHERE id = ?"
        contact_id = profile_id

    async with db.execute(contact_query, (contact_id,)) as cur:
        target = await cur.fetchone()

    if target and target['telegram_contact']:
        await call.message.answer(
            "🎉 Profil egasining aloqa manzili:\n"
            f"👉 [Profilga o'tish]({target['telegram_contact']})",
            parse_mode="Markdown",
        )
    else:
        await call.message.answer("❌ Profil egasi topilmadi.")
    await call.answer()

# ─── BOT ICHIDAGI SUHBAT ───────────────────────────────────────────────────────

@dp.message(F.text == "💬 Suhbatlashish", StateFilter(None))
async def chat_panel(message: types.Message, state: FSMContext):
    active = await get_active_chat(message.from_user.id)
    if not active:
        await message.answer(
            "💬 Hozircha faol suhbatdosh yo'q.\n\n"
            "⏳ Birozdan so'ng yana urinib ko'ring — yangi suhbatdoshlar "
            "tez orada topiladi 😊"
        )
        return

    await state.set_state(Conversation.waiting_for_message)
    await message.answer(
        "💬 Suhbatlashish rejimi yoqildi.\n\n"
        "Yuborgan xabarlaringiz suhbatdoshingizga bot orqali yetkaziladi. "
        "Suhbatni tugatish uchun pastdagi tugmani yoki /bekor buyrug'ini bosing.",
        reply_markup=chat_keyboard(),
    )

@dp.callback_query(F.data.startswith("chat_request_"))
async def chat_request_cb(call: types.CallbackQuery):
    user_id = call.from_user.id
    try:
        profile_id = int(call.data.split("_", 2)[2])
    except (TypeError, ValueError):
        await call.answer("❌ Profil ma'lumotida xato.", show_alert=True)
        return

    # Manfiy ID admin katalog profilini anglatadi; unda Telegram foydalanuvchi
    # ID'si bo'lmagani uchun bot ichida suhbat ochib bo'lmaydi.
    if profile_id < 0:
        await call.answer(
            "Bu katalog profilidir. Bot ichidagi suhbat faqat haqiqiy "
            "foydalanuvchi profillari bilan ishlaydi.",
            show_alert=True,
        )
        return

    db = await get_db()
    async with db.execute(
        "SELECT user_id, name FROM profiles WHERE id=? AND user_id != ?",
        (profile_id, user_id),
    ) as cur:
        target = await cur.fetchone()
    if not target:
        await call.answer("❌ Profil egasi topilmadi.", show_alert=True)
        return

    if await get_active_chat(user_id):
        await call.answer(
            "Avval amaldagi suhbatni tugating yoki 💬 Suhbatlashish bo'limiga o'ting.",
            show_alert=True,
        )
        return
    if await get_active_chat(target["user_id"]):
        await call.answer("Bu foydalanuvchi hozir boshqa suhbatda.", show_alert=True)
        return

    async with db.execute(
        "SELECT id FROM chat_requests "
        "WHERE requester_id=? AND target_id=? AND status='pending' "
        "ORDER BY id DESC LIMIT 1",
        (user_id, target["user_id"]),
    ) as cur:
        existing = await cur.fetchone()
    if existing:
        await call.answer("Suhbat so'rovi allaqachon yuborilgan.", show_alert=True)
        return

    requester_name = call.from_user.full_name or "Foydalanuvchi"
    cursor = await db.execute(
        "INSERT INTO chat_requests "
        "(requester_id, target_id, status, created_at) VALUES (?, ?, 'pending', ?)",
        (user_id, target["user_id"], datetime.now().isoformat()),
    )
    request_id = cursor.lastrowid
    await db.commit()
    if request_id is None:
        await call.answer("❌ Suhbat so'rovini saqlab bo'lmadi.", show_alert=True)
        return

    request_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Qabul qilish", callback_data=f"chat_accept_{request_id}"
        ),
        InlineKeyboardButton(
            text="❌ Rad etish", callback_data=f"chat_decline_{request_id}"
        ),
    ]])
    try:
        await bot.send_message(
            target["user_id"],
            f"💬 *{md_escape(requester_name)}* siz bilan bot ichida suhbatlashmoqchi.\n\n"
            "Qabul qilsangiz, xabarlaringiz bot orqali bir-biringizga yetkaziladi.",
            parse_mode="Markdown",
            reply_markup=request_kb,
        )
    except Exception:
        await db.execute("DELETE FROM chat_requests WHERE id=?", (request_id,))
        await db.commit()
        await call.answer(
            "❌ Foydalanuvchiga xabar yuborib bo'lmadi.", show_alert=True
        )
        return

    await call.message.answer(
        f"✅ {md_escape(target['name'])} ga suhbat so'rovi yuborildi.\n"
        "Qabul qilinishini kuting.",
        parse_mode="Markdown",
    )
    await call.answer("Suhbat so'rovi yuborildi.")

@dp.callback_query(F.data.startswith("chat_accept_"))
async def chat_accept_cb(call: types.CallbackQuery):
    try:
        request_id = int(call.data.split("_", 2)[2])
    except (TypeError, ValueError):
        await call.answer("❌ So'rov ma'lumotida xato.", show_alert=True)
        return

    db = await get_db()
    async with db.execute(
        "SELECT requester_id, target_id FROM chat_requests "
        "WHERE id=? AND status='pending'", (request_id,)
    ) as cur:
        request = await cur.fetchone()
    if not request or request["target_id"] != call.from_user.id:
        await call.answer("Bu so'rov eskirgan yoki sizga tegishli emas.", show_alert=True)
        return

    if await get_active_chat(call.from_user.id) or await get_active_chat(request["requester_id"]):
        await call.answer("Ishtirokchilardan biri boshqa suhbatda.", show_alert=True)
        return

    await db.execute(
        "UPDATE chat_requests SET status='accepted' WHERE id=?", (request_id,)
    )
    await db.commit()
    await start_chat_for_users(request["requester_id"], call.from_user.id)
    requester_state = await get_user_state(request["requester_id"])
    target_state = await get_user_state(call.from_user.id)
    await requester_state.set_state(Conversation.waiting_for_message)
    await target_state.set_state(Conversation.waiting_for_message)

    await call.message.edit_text(
        "✅ Suhbat qabul qilindi!\n\n"
        "Endi yuborgan xabarlaringiz bot orqali suhbatdoshingizga yetkaziladi."
    )
    await call.message.answer(
        "💬 Suhbat boshlandi. Xabar yuborishingiz mumkin.",
        reply_markup=chat_keyboard(),
    )
    try:
        await bot.send_message(
            request["requester_id"],
            "✅ Suhbat so'rovingiz qabul qilindi!\n\n"
            "Endi xabar yuborishingiz mumkin. Suhbatni tugatish uchun /bekor bosing.",
            reply_markup=chat_keyboard(),
        )
    except Exception:
        pass
    await call.answer()

@dp.callback_query(F.data.startswith("chat_decline_"))
async def chat_decline_cb(call: types.CallbackQuery):
    try:
        request_id = int(call.data.split("_", 2)[2])
    except (TypeError, ValueError):
        await call.answer("❌ So'rov ma'lumotida xato.", show_alert=True)
        return

    db = await get_db()
    async with db.execute(
        "SELECT requester_id, target_id FROM chat_requests "
        "WHERE id=? AND status='pending'", (request_id,)
    ) as cur:
        request = await cur.fetchone()
    if not request or request["target_id"] != call.from_user.id:
        await call.answer("Bu so'rov eskirgan yoki sizga tegishli emas.", show_alert=True)
        return

    await db.execute(
        "UPDATE chat_requests SET status='declined' WHERE id=?", (request_id,)
    )
    await db.commit()
    await call.message.edit_text("❌ Suhbat so'rovi rad etildi.")
    try:
        await bot.send_message(
            request["requester_id"],
            "ℹ️ Suhbat so'rovingiz qabul qilinmadi.",
        )
    except Exception:
        pass
    await call.answer()

@dp.message(F.text == "❌ Suhbatni tugatish", StateFilter("*"))
async def end_chat_button(message: types.Message, state: FSMContext):
    partner_id = await end_chat(message.from_user.id)
    await state.clear()
    await message.answer(
        "✅ Suhbat tugatildi.",
        reply_markup=main_menu(message.from_user.id),
    )
    if partner_id:
        partner_state = await get_user_state(partner_id)
        await partner_state.clear()
        try:
            await bot.send_message(
                partner_id,
                "ℹ️ Suhbatdoshingiz suhbatni tugatdi.",
                reply_markup=main_menu(partner_id),
            )
        except Exception:
            pass

@dp.message(Conversation.waiting_for_message)
async def relay_chat_message(message: types.Message, state: FSMContext):
    active = await get_active_chat(message.from_user.id)
    if not active:
        await state.clear()
        await message.answer(
            "ℹ️ Faol suhbat topilmadi.",
            reply_markup=main_menu(message.from_user.id),
        )
        return

    partner_id = active["partner_id"]
    try:
        await bot.send_message(partner_id, "💬 Suhbatdoshingizdan:")
        await message.copy_to(partner_id)
        await message.answer("✅ Xabar yetkazildi.", reply_markup=chat_keyboard())
    except Exception:
        await end_chat(message.from_user.id)
        await state.clear()
        await message.answer(
            "❌ Xabarni yetkazib bo'lmadi. Suhbat yakunlandi.",
            reply_markup=main_menu(message.from_user.id),
        )

# ─── ADMIN PANEL ─────────────────────────────────────────────────────────────

@dp.message(Command("admin"), StateFilter("*"))
async def admin_panel(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "👨‍💻 Admin panelga xush kelibsiz!",
        reply_markup=admin_menu(message.from_user.id)
    )

@dp.message(Command("bekor"), StateFilter("*"))
async def cancel_cmd(message: types.Message, state: FSMContext):
    partner_id = await end_chat(message.from_user.id)
    await state.clear()
    is_adm = await is_bot_admin(message.from_user.id)
    menu = admin_menu(message.from_user.id) if is_adm else main_menu(message.from_user.id)
    await message.answer("❌ Amal bekor qilindi.", reply_markup=menu)
    if partner_id:
        partner_state = await get_user_state(partner_id)
        await partner_state.clear()
        try:
            await bot.send_message(
                partner_id,
                "ℹ️ Suhbatdoshingiz suhbatni tugatdi.",
                reply_markup=main_menu(partner_id),
            )
        except Exception:
            pass

@dp.message(F.text == "❌ Bekor qilish", StateFilter("*"))
async def cancel_button(message: types.Message, state: FSMContext):
    partner_id = await end_chat(message.from_user.id)
    await state.clear()
    is_adm = await is_bot_admin(message.from_user.id)
    menu = admin_menu(message.from_user.id) if is_adm else main_menu(message.from_user.id)
    await message.answer("❌ Amal bekor qilindi.", reply_markup=menu)
    if partner_id:
        partner_state = await get_user_state(partner_id)
        await partner_state.clear()
        try:
            await bot.send_message(
                partner_id,
                "ℹ️ Suhbatdoshingiz suhbatni tugatdi.",
                reply_markup=main_menu(partner_id),
            )
        except Exception:
            pass

@dp.message(F.text == "⬅️ Ortga", StateFilter("*"))
async def back_button(message: types.Message, state: FSMContext):
    current = await state.get_state()
    if not current:
        await message.answer("Bosh menyu:", reply_markup=main_menu(message.from_user.id))
        return

    if current.endswith("waiting_for_age"):
        await state.set_state(
            UserRegistration.waiting_for_gender
            if current.startswith("UserRegistration")
            else AdminProfileAdd.waiting_for_gender
        )
        keyboard = (
            gender_keyboard()
        )
        await message.answer("Jinsingizni tanlang:", reply_markup=keyboard)
        return

    if current.endswith("waiting_for_name"):
        await state.set_state(AdminProfileAdd.waiting_for_gender)
        await message.answer("Jinsini tanlang:", reply_markup=gender_keyboard())
        return

    if current.endswith("waiting_for_photo"):
        if current.startswith("UserRegistration"):
            await state.set_state(UserRegistration.waiting_for_age)
            await message.answer(
                "Yoshingizni kiriting (masalan: 20):",
                reply_markup=navigation_keyboard(),
            )
        else:
            await state.set_state(AdminProfileAdd.waiting_for_age)
            await message.answer(
                "Profil yoshini kiriting:",
                reply_markup=navigation_keyboard(),
            )
        return

    if current.endswith("waiting_for_bio"):
        if current.startswith("UserRegistration"):
            await state.set_state(UserRegistration.waiting_for_photo)
            await message.answer(
                "📸 Profilingiz uchun rasm yuboring:",
                reply_markup=navigation_keyboard(),
            )
        else:
            await state.set_state(AdminProfileAdd.waiting_for_photo)
            await message.answer(
                "📸 Ushbu profil uchun rasm yuboring:",
                reply_markup=navigation_keyboard(),
            )
        return

    if current.endswith("waiting_for_contact"):
        if current.startswith("UserRegistration"):
            await state.set_state(UserRegistration.waiting_for_bio)
            await message.answer(
                "📝 Bio yozing yoki o'tkazib yuboring:",
                reply_markup=optional_bio_keyboard(),
            )
        else:
            await state.set_state(AdminProfileAdd.waiting_for_bio)
            await message.answer(
                "📝 Bio yozing yoki o'tkazib yuboring:",
                reply_markup=optional_bio_keyboard(),
            )
        return

    if current.startswith("UserRegistration"):
        await state.clear()
        await message.answer(
            "Profil yaratish to'xtatildi. /start orqali qayta boshlashingiz mumkin.",
            reply_markup=main_menu(message.from_user.id),
        )
    else:
        await state.clear()
        await message.answer(
            "Amal ortga qaytarildi.",
            reply_markup=admin_menu(message.from_user.id),
        )

@dp.message(F.text == "🏠 Bosh menyu", StateFilter("*"))
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Bosh menyu:", reply_markup=main_menu(message.from_user.id))


# ─── ADMIN PROFILLARI ─────────────────────────────────────────────────────────

@dp.message(F.text == "📝 Profil qo'shish", StateFilter("*"))
async def admin_profile_add_start(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    await state.set_state(AdminProfileAdd.waiting_for_gender)
    await message.answer(
        "📝 *Yangi katalog profilini qo'shish*\n\n"
        "Bu bo'limda xohlaganingizcha profil qo'shishingiz mumkin. "
        "Har bir profil alohida saqlanadi va random qidiruvda ko'rinadi.\n\n"
        "Jinsini tanlang.\n\nBekor qilish: /bekor",
        parse_mode="Markdown",
        reply_markup=gender_keyboard()
    )


@dp.message(AdminProfileAdd.waiting_for_gender, F.text.in_(["🙋‍♂️ Yigit", "🙋‍♀️ Qiz"]))
async def admin_profile_add_gender(message: types.Message, state: FSMContext):
    gender = "male" if "Yigit" in message.text else "female"
    await state.update_data(gender=gender)
    await state.set_state(AdminProfileAdd.waiting_for_name)
    await message.answer(
        "👤 Profil egasining ismini kiriting.\n\nBekor qilish: /bekor",
        reply_markup=navigation_keyboard()
    )


@dp.message(AdminProfileAdd.waiting_for_gender)
async def admin_profile_add_gender_invalid(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Ortga":
        await state.clear()
        await message.answer("🏠 Amal bekor qilindi.", reply_markup=admin_menu(message.from_user.id))
        return
    await message.answer("Iltimos, jinsni tugmalardan tanlang.", reply_markup=gender_keyboard())


@dp.message(AdminProfileAdd.waiting_for_name)
async def admin_profile_add_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if name == "⬅️ Ortga":
        await state.set_state(AdminProfileAdd.waiting_for_gender)
        await message.answer("Jinsini tanlang:", reply_markup=gender_keyboard())
        return
    if not name or len(name) > 100:
        await message.answer("❌ Ism 1–100 belgi bo'lishi kerak. Qaytadan kiriting:")
        return
    await state.update_data(name=name)
    await state.set_state(AdminProfileAdd.waiting_for_age)
    await message.answer(
        f"🎂 {name} profilining yoshini kiriting ({MIN_AGE}–{MAX_AGE}).\n\n"
        "Bekor qilish: /bekor",
        reply_markup=navigation_keyboard(),
    )


@dp.message(AdminProfileAdd.waiting_for_age)
async def admin_profile_add_age(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Ortga":
        await state.set_state(AdminProfileAdd.waiting_for_name)
        await message.answer(
            "👤 Profil egasining ismini kiriting:",
            reply_markup=navigation_keyboard(),
        )
        return
    if not message.text or not message.text.isdigit():
        await message.answer("❌ Iltimos, yoshni faqat raqam bilan kiriting:")
        return
    age = int(message.text)
    if age < MIN_AGE or age > MAX_AGE:
        await message.answer(f"❌ Yosh {MIN_AGE}–{MAX_AGE} oralig'ida bo'lishi kerak:")
        return
    await state.update_data(age=age)
    await state.set_state(AdminProfileAdd.waiting_for_photo)
    await message.answer(
        "📸 Ushbu profil uchun rasm yuboring.\n\nBekor qilish: /bekor",
        reply_markup=navigation_keyboard(),
    )


@dp.message(AdminProfileAdd.waiting_for_photo, F.photo)
async def admin_profile_add_photo(message: types.Message, state: FSMContext):
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AdminProfileAdd.waiting_for_bio)
    await message.answer(
        "📝 Profil haqida qisqacha ma'lumot yozing (Bio) yoki o'tkazib yuboring:",
        reply_markup=optional_bio_keyboard()
    )


@dp.message(AdminProfileAdd.waiting_for_photo)
async def admin_profile_add_photo_invalid(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Ortga":
        await state.set_state(AdminProfileAdd.waiting_for_age)
        await message.answer(
            "Profil yoshini kiriting:",
            reply_markup=navigation_keyboard(),
        )
        return
    await message.answer(
        "❌ Iltimos, rasm (foto) yuboring yoki ortga qayting.",
        reply_markup=navigation_keyboard(),
    )


@dp.message(AdminProfileAdd.waiting_for_bio)
async def admin_profile_add_bio(message: types.Message, state: FSMContext):
    bio = (message.text or "").strip()
    if bio == "⬅️ Ortga":
        await state.set_state(AdminProfileAdd.waiting_for_photo)
        await message.answer(
            "📸 Ushbu profil uchun rasm yuboring:",
            reply_markup=navigation_keyboard(),
        )
        return
    if bio in {"⏭ Bio'ni o'tkazib yuborish", "/skip", "skip", "o'tkazib yuborish"}:
        bio = ""
    elif not bio:
        await message.answer(
            "❌ Bio matn bo'lishi kerak yoki quyidagi tugma orqali o'tkazib yuboring:",
            reply_markup=optional_bio_keyboard()
        )
        return
    if len(bio) > 500:
        await message.answer("❌ Bio 500 belgidan oshmasin. Qaytadan yozing:")
        return
    await state.update_data(bio=bio)
    await state.set_state(AdminProfileAdd.waiting_for_contact)
    await message.answer(
        "🔗 Aloqa username'ini kiriting (masalan: @username):\n"
        "Bu kontakt Premium foydalanuvchiga ko'rsatiladi.\n\n"
        "Bekor qilish: /bekor",
        reply_markup=navigation_keyboard()
    )


@dp.message(AdminProfileAdd.waiting_for_contact)
async def admin_profile_add_finish(message: types.Message, state: FSMContext):
    contact = (message.text or "").strip()
    if contact == "⬅️ Ortga":
        await state.set_state(AdminProfileAdd.waiting_for_bio)
        await message.answer(
            "📝 Bio yozing yoki o'tkazib yuboring:",
            reply_markup=optional_bio_keyboard(),
        )
        return
    if not contact or len(contact) > 255:
        await message.answer("❌ Aloqa username'ini to'g'ri kiriting:")
        return

    data = await state.get_data()
    db = await get_db()
    await db.execute(
        """
        INSERT INTO admin_profiles
            (created_by, gender, name, age, photo_id, bio, telegram_contact, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message.from_user.id,
            data["gender"],
            data["name"],
            data["age"],
            data["photo_id"],
            data["bio"],
            contact,
            datetime.now().isoformat(),
        )
    )
    await db.commit()
    await state.clear()

    await message.answer(
        "✅ Profil random katalogga qo'shildi!\n\n"
        "Yana profil qo'shish uchun \"📝 Profil qo'shish\" tugmasini bosing.",
        reply_markup=admin_menu(message.from_user.id)
    )

# ─── PROFILLARNI ADMIN ORQALI O'CHIRISH ────────────────────────────────────────

async def admin_profiles_page(page: int = 0):
    db = await get_db()
    offset = max(page, 0) * PAGE_SIZE
    async with db.execute(
        """SELECT 'user' AS source, id, name, age, gender, user_id AS owner_id
           FROM profiles
           UNION ALL
           SELECT 'catalog' AS source, id, name, age, gender, created_by AS owner_id
           FROM admin_profiles
           ORDER BY source, id DESC
           LIMIT ? OFFSET ?""",
        (PAGE_SIZE, offset),
    ) as cur:
        rows = await cur.fetchall()
    async with db.execute("SELECT COUNT(*) AS cnt FROM profiles") as cur:
        user_count = (await cur.fetchone())["cnt"]
    async with db.execute("SELECT COUNT(*) AS cnt FROM admin_profiles") as cur:
        catalog_count = (await cur.fetchone())["cnt"]
    return rows, user_count + catalog_count

def admin_profiles_keyboard(rows, page: int, total: int):
    buttons = []
    for row in rows:
        source_icon = "👤" if row["source"] == "user" else "🗂"
        callback_prefix = "user" if row["source"] == "user" else "catalog"
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {source_icon} {row['name']} ({row['age']})",
            callback_data=f"del_profile_{callback_prefix}_{row['id']}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️ Oldingi", callback_data=f"profiles_page_{page - 1}"
        ))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(
            text="Keyingi ➡️", callback_data=f"profiles_page_{page + 1}"
        ))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(
        text="🏠 Admin menyusi", callback_data="profiles_close"
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def render_admin_profiles(page: int = 0):
    rows, total = await admin_profiles_page(page)
    if not rows:
        return (
            "📋 *Profillar ro'yxati*\n\n"
            "Hozircha o'chirish uchun profil mavjud emas.",
            admin_profiles_keyboard([], page, total),
        )
    text = (
        f"📋 *Profillar ro'yxati* — {page + 1}-sahifa\n\n"
        "👤 — foydalanuvchi profili\n"
        "🗂 — admin katalog profili\n\n"
    )
    for index, row in enumerate(rows, start=1):
        source = "Foydalanuvchi" if row["source"] == "user" else "Katalog"
        text += f"{index}. {md_escape(row['name'])} — {row['age']} yosh ({source})\n"
    text += "\nO'chirish uchun kerakli profil tugmasini bosing:"
    return text, admin_profiles_keyboard(rows, page, total)

def normalize_username(raw: str) -> str:
    """@user, t.me/user, https://t.me/user?x=1 kabi kirishlardan sof username (kichik harfda) ajratadi."""
    value = unquote((raw or "").strip())
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.split("?")[0].strip("/").lstrip("@").strip()
    return value.lower()

async def find_profiles_for_admin(query: str):
    """
    Admin kiritgan Telegram ID yoki username bo'yicha profillarni topadi.
    - Raqam bo'lsa: foydalanuvchi profilini user_id bo'yicha qidiradi.
    - Aks holda username sifatida: foydalanuvchi va katalog profillarining
      aloqa havolasi (telegram_contact) bilan TO'LIQ moslikni tekshiradi.
    """
    db = await get_db()
    query = (query or "").strip()
    rows = []

    if re.match(r"^\d+$", query):
        async with db.execute(
            "SELECT 'user' AS source, id, name, age, user_id AS owner_id, telegram_contact "
            "FROM profiles WHERE user_id = ?",
            (int(query),),
        ) as cur:
            rows = list(await cur.fetchall())
        return rows

    uname = normalize_username(query)
    if not re.match(r"^[a-z0-9_]{3,32}$", uname):
        return None  # noto'g'ri format

    like = f"%{uname}%"
    async with db.execute(
        "SELECT 'user' AS source, id, name, age, user_id AS owner_id, telegram_contact "
        "FROM profiles WHERE lower(telegram_contact) LIKE ?",
        (like,),
    ) as cur:
        user_rows = await cur.fetchall()
    async with db.execute(
        "SELECT 'catalog' AS source, id, name, age, created_by AS owner_id, telegram_contact "
        "FROM admin_profiles WHERE lower(telegram_contact) LIKE ?",
        (like,),
    ) as cur:
        catalog_rows = await cur.fetchall()

    for row in list(user_rows) + list(catalog_rows):
        if normalize_username(row["telegram_contact"]) == uname:
            rows.append(row)
    return rows

async def delete_profile_record(source: str, profile_id: int):
    """Profilni bazadan o'chiradi. O'chirilgan profil nomini (belgi bilan) qaytaradi yoki None."""
    db = await get_db()
    if source == "user":
        async with db.execute(
            "SELECT name, user_id FROM profiles WHERE id=?", (profile_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        await db.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
        await db.execute("DELETE FROM seen_profiles WHERE profile_id=?", (profile_id,))
        await db.commit()
        await end_chat(row["user_id"])
        return f"👤 {row['name']}"
    if source == "catalog":
        async with db.execute(
            "SELECT name FROM admin_profiles WHERE id=?", (profile_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        await db.execute("DELETE FROM admin_profiles WHERE id=?", (profile_id,))
        await db.execute("DELETE FROM seen_profiles WHERE profile_id=?", (-profile_id,))
        await db.commit()
        return f"🗂 {row['name']}"
    return None

@dp.message(F.text == "🗑 Profilni o'chirish", StateFilter("*"))
async def admin_profile_delete_start(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    await state.set_state(AdminProfileDelete.waiting_for_query)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Umumiy ro'yxat", callback_data="profiles_page_0")
    ]])
    await message.answer(
        "🗑 *Profilni o'chirish*\n\n"
        "O'chirmoqchi bo'lgan foydalanuvchining *username*'ini yoki *Telegram ID*'sini yuboring:\n"
        "• `@username` yoki `https://t.me/username`\n"
        "• `123456789` (Telegram ID)\n\n"
        "Bekor qilish: /bekor",
        parse_mode="Markdown",
        reply_markup=kb,
    )

@dp.message(AdminProfileDelete.waiting_for_query)
async def admin_profile_delete_search(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("❌ Username yoki ID yuboring (matn ko'rinishida).")
        return

    rows = await find_profiles_for_admin(query)
    if rows is None:
        await message.answer(
            "❌ Format noto'g'ri. `@username`, t.me havolasi yoki raqamli ID yuboring.",
            parse_mode="Markdown",
        )
        return
    if not rows:
        await message.answer(
            "🔍 Bunday username/ID bo'yicha profil topilmadi.\n"
            "Boshqasini yuboring yoki /bekor deb yozing."
        )
        return

    buttons = []
    text = "🔎 *Topilgan profillar:*\n\n"
    for index, row in enumerate(rows, start=1):
        source_label = "Foydalanuvchi" if row["source"] == "user" else "Katalog"
        icon = "👤" if row["source"] == "user" else "🗂"
        text += (
            f"{index}. {md_escape(row['name'])} — {row['age']} yosh ({source_label})\n"
            f"   🆔 `{row['owner_id']}`\n"
        )
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {icon} {row['name']} ({row['age']})",
            callback_data=f"delfound_{row['source']}_{row['id']}",
        )])
    text += "\nO'chirish uchun tugmani bosing:"
    await message.answer(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )

@dp.callback_query(F.data.startswith("delfound_"))
async def admin_profile_delete_found_cb(call: types.CallbackQuery):
    if not await is_bot_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    parts = call.data.split("_")
    if len(parts) != 3 or not parts[2].isdigit():
        await call.answer("❌ Tugmada xato.", show_alert=True)
        return
    deleted_text = await delete_profile_record(parts[1], int(parts[2]))
    if not deleted_text:
        await call.answer("Profil allaqachon o'chirilgan.", show_alert=True)
        return
    await call.message.edit_text(f"✅ {deleted_text} o'chirildi.")
    await call.answer("Profil o'chirildi.")

@dp.callback_query(F.data.startswith("profiles_page_"))
async def admin_profiles_page_cb(call: types.CallbackQuery):
    if not await is_bot_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    try:
        page = int(call.data.rsplit("_", 1)[1])
    except (TypeError, ValueError):
        await call.answer("❌ Sahifa xatosi.", show_alert=True)
        return
    text, keyboard = await render_admin_profiles(page)
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    await call.answer()

@dp.callback_query(F.data == "profiles_close")
async def admin_profiles_close_cb(call: types.CallbackQuery):
    if not await is_bot_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    await call.message.edit_text("Admin menyusiga qaytildi.")
    await call.message.answer("Admin panel:", reply_markup=admin_menu(call.from_user.id))
    await call.answer()

@dp.callback_query(F.data.startswith("del_profile_"))
async def admin_profile_delete_cb(call: types.CallbackQuery):
    if not await is_bot_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return

    parts = call.data.split("_")
    if len(parts) != 4:
        await call.answer("❌ Profil tugmasida xato.", show_alert=True)
        return
    source, profile_id_text = parts[2], parts[3]
    try:
        profile_id = int(profile_id_text)
    except ValueError:
        await call.answer("❌ Profil ID noto'g'ri.", show_alert=True)
        return
    if source not in ("user", "catalog"):
        await call.answer("❌ Profil turi noto'g'ri.", show_alert=True)
        return

    deleted_text = await delete_profile_record(source, profile_id)
    if not deleted_text:
        await call.answer("Profil allaqachon o'chirilgan.", show_alert=True)
        return

    text, keyboard = await render_admin_profiles()
    await call.message.edit_text(
        f"✅ {deleted_text} o'chirildi.\n\n{text}",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    await call.answer("Profil o'chirildi.")


# ─── VOYAGA YETMAGAN PROFILLARNI TOZALASH ─────────────────────────────────────

@dp.message(F.text == "🧹 Voyaga yetmagan profillarni o'chirish", StateFilter("*"))
async def underage_profiles_cleanup_start(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    db = await get_db()
    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM profiles WHERE age < ?",
        (MIN_AGE,)
    ) as cur:
        user_count = (await cur.fetchone())["cnt"]
    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM admin_profiles WHERE age < ?",
        (MIN_AGE,)
    ) as cur:
        admin_count = (await cur.fetchone())["cnt"]
    total = user_count + admin_count

    if total == 0:
        await message.answer(
            f"✅ 18 yoshdan kichik profil topilmadi.\n\n"
            f"Oddiy profillar: {user_count}\n"
            f"Admin profillari: {admin_count}",
            reply_markup=admin_menu(message.from_user.id)
        )
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"✅ {total} ta profilni o'chirish",
            callback_data="cleanup_underage_confirm"
        )],
        [InlineKeyboardButton(
            text="❌ Bekor qilish",
            callback_data="cleanup_underage_cancel"
        )],
    ])
    await message.answer(
        f"⚠️ {total} ta 18 yoshdan kichik profil topildi.\n\n"
        f"• Oddiy profillar: {user_count}\n"
        f"• Admin profillari: {admin_count}\n\n"
        "Ular random qidiruvdan butunlay o'chiriladi. Davom etasizmi?",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "cleanup_underage_cancel")
async def underage_profiles_cleanup_cancel(call: types.CallbackQuery):
    if not await is_bot_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    await call.message.edit_text("❌ Tozalash bekor qilindi.")
    await call.answer()


@dp.callback_query(F.data == "cleanup_underage_confirm")
async def underage_profiles_cleanup_confirm(call: types.CallbackQuery):
    if not await is_bot_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return

    db = await get_db()
    async with db.execute(
        "SELECT id FROM profiles WHERE age < ?",
        (MIN_AGE,)
    ) as cur:
        user_profile_ids = [row["id"] for row in await cur.fetchall()]
    async with db.execute(
        "SELECT id FROM admin_profiles WHERE age < ?",
        (MIN_AGE,)
    ) as cur:
        admin_profile_ids = [row["id"] for row in await cur.fetchall()]

    if user_profile_ids:
        placeholders = ",".join("?" for _ in user_profile_ids)
        await db.execute(
            f"DELETE FROM seen_profiles WHERE profile_id IN ({placeholders})",
            user_profile_ids
        )
        await db.execute(
            f"DELETE FROM profiles WHERE id IN ({placeholders})",
            user_profile_ids
        )
    if admin_profile_ids:
        negative_ids = [-profile_id for profile_id in admin_profile_ids]
        placeholders = ",".join("?" for _ in negative_ids)
        await db.execute(
            f"DELETE FROM seen_profiles WHERE profile_id IN ({placeholders})",
            negative_ids
        )
        await db.execute(
            f"DELETE FROM admin_profiles WHERE id IN ({','.join('?' for _ in admin_profile_ids)})",
            admin_profile_ids
        )
    await db.commit()

    removed = len(user_profile_ids) + len(admin_profile_ids)
    await call.message.edit_text(
        f"✅ Tozalash tugadi.\n\n"
        f"🗑 O'chirilgan profillar: {removed}\n"
        "18 yoshdan kichik profillar random qidiruvdan olib tashlandi."
    )
    await call.answer("Profillar o'chirildi.")


# ─── PREMIUM BERISH (ADMIN) ───────────────────────────────────────────────────

@dp.message(F.text == "👥 Premium berish", StateFilter("*"))
async def admin_give_premium(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    await state.update_data(action="give")
    await state.set_state(AdminPremium.waiting_user_id)
    await message.answer(
        "Premium bermoqchi bo'lgan foydalanuvchining *Telegram ID* sini kiriting:\n\n"
        "_(ID ni bilish uchun foydalanuvchi @userinfobot ga yozsin)_",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(F.text == "❌ Premium olish", StateFilter("*"))
async def admin_remove_premium_cmd(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    await state.update_data(action="remove")
    await state.set_state(AdminPremium.waiting_user_id)
    await message.answer(
        "Premium *olmoqchi* bo'lgan foydalanuvchining *Telegram ID* sini kiriting:",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(AdminPremium.waiting_user_id)
async def process_premium_action(message: types.Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Noto'g'ri ID. Faqat raqam kiriting.")
        return

    data = await state.get_data()
    action = data.get("action", "give")
    if action == "give":
        expire = await add_premium(target_id, PREMIUM_DAYS)
        expire_str = expire.strftime("%d.%m.%Y")
        await message.answer(
            f"✅ Foydalanuvchi `{target_id}` ga {PREMIUM_DAYS} kunlik Premium berildi.\n"
            f"📅 Tugash sanasi: *{expire_str}*",
            reply_markup=admin_menu(message.from_user.id), parse_mode="Markdown"
        )
        try:
            await bot.send_message(
                target_id,
                f"🎉 *Tabriklaymiz!* Sizga {PREMIUM_DAYS} kunlik Premium berildi.\n"
                f"📅 *Muddat:* {expire_str} gacha\n\n"
                f"🌟 Endi \"💌 Tanishish\" tugmasi orqali bevosita bog'lanishingiz mumkin!",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    else:
        if await is_premium_user(target_id):
            await remove_premium(target_id)
            await message.answer(
                f"✅ Foydalanuvchi `{target_id}` ning Premium obunasi bekor qilindi.",
                reply_markup=admin_menu(message.from_user.id), parse_mode="Markdown"
            )
            try:
                await bot.send_message(
                    target_id,
                    "❌ Sizning Premium obunangiz admin tomonidan bekor qilindi."
                )
            except Exception:
                pass
        else:
            await message.answer(
                f"⚠️ Foydalanuvchi `{target_id}` premium a'zo emas.",
                reply_markup=admin_menu(message.from_user.id), parse_mode="Markdown"
            )
    await state.clear()

@dp.message(F.text == "📊 Premium ro'yxati", StateFilter("*"))
async def premium_list_admin(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    db = await get_db()
    async with db.execute(
        "SELECT user_id, expire_date FROM premium_users ORDER BY expire_date DESC"
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        await message.answer("Hozircha premium a'zolar yo'q.")
        return
    text = "🌟 *Premium a'zolar:*\n\n"
    for row in rows:
        exp = datetime.fromisoformat(row["expire_date"]) if row["expire_date"] else None
        exp_str = exp.strftime("%d.%m.%Y") if exp else "Noma'lum"
        status = "✅" if exp and exp > datetime.now() else "❌"
        text += f"{status} `{row['user_id']}` — {exp_str}\n"
    await message.answer(text, parse_mode="Markdown")


# ─── STATISTIKA ───────────────────────────────────────────────────────────────

@dp.message(F.text == "📊 Statistika", StateFilter("*"))
async def statistics(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    db = await get_db()
    async with db.execute("SELECT COUNT(*) as cnt FROM users") as cur:
        users_count = (await cur.fetchone())["cnt"]
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM premium_users WHERE expire_date > ?",
        (datetime.now().isoformat(),)
    ) as cur:
        prem_count = (await cur.fetchone())["cnt"]
    async with db.execute("SELECT COUNT(*) as cnt FROM profiles") as cur:
        profiles_count = (await cur.fetchone())["cnt"]
    async with db.execute("SELECT COUNT(*) as cnt FROM admin_profiles") as cur:
        admin_profiles_count = (await cur.fetchone())["cnt"]
    profiles_count += admin_profiles_count
    async with db.execute(
        "SELECT (SELECT COUNT(*) FROM profiles WHERE gender='male') + "
        "(SELECT COUNT(*) FROM admin_profiles WHERE gender='male') AS cnt"
    ) as cur:
        male_count = (await cur.fetchone())["cnt"]
    async with db.execute(
        "SELECT (SELECT COUNT(*) FROM profiles WHERE gender='female') + "
        "(SELECT COUNT(*) FROM admin_profiles WHERE gender='female') AS cnt"
    ) as cur:
        female_count = (await cur.fetchone())["cnt"]
    async with db.execute("SELECT COUNT(*) as cnt FROM channels") as cur:
        channels_count = (await cur.fetchone())["cnt"]

    await message.answer(
        f"📊 *Bot statistikasi:*\n\n"
        f"👥 Jami foydalanuvchilar: *{users_count:,}*\n"
        f"🌟 Faol premium a'zolar: *{prem_count:,}*\n"
        f"📝 Yaratilgan profillar: *{profiles_count:,}* "
        f"(🙋‍♂️ {male_count:,} / 🙋‍♀️ {female_count:,})\n"
        f"📢 Kanallar: *{channels_count:,}*",
        parse_mode="Markdown"
    )


# ─── XABAR YUBORISH ───────────────────────────────────────────────────────────

@dp.message(F.text == "📣 Xabar yuborish", StateFilter("*"))
async def start_broadcast(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.set_state(AdminBroadcast.waiting_for_message)
    await message.answer(
        "📣 Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni yuboring.\n"
        "(Matn, rasm yoki video bo'lishi mumkin)\n\n"
        "Bekor qilish uchun /bekor",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(AdminBroadcast.waiting_for_message)
async def send_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    db = await get_db()
    async with db.execute("SELECT user_id FROM users") as cur:
        all_users = await cur.fetchall()

    sent, failed = 0, 0
    for row in all_users:
        uid = row["user_id"]
        try:
            await message.copy_to(uid, protect_content=True)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await message.answer(
        f"📣 Xabar yuborildi!\n\n✅ Muvaffaqiyatli: *{sent}*\n❌ Yuborilmadi: *{failed}*",
        reply_markup=admin_menu(message.from_user.id), parse_mode="Markdown"
    )


# ─── KANAL QO'SHISH ──────────────────────────────────────────────────────────

async def save_telegram_channel(
    message: types.Message, state: FSMContext,
    save_ch_id: str, title: str, link: str, type_label: str
):
    """Kanal/guruhni bazaga saqlaydi (yoki eskisini yangilaydi) va adminга
    yakuniy xabar yuboradi. Agar bu channel_id bazada allaqachon bo'lsa —
    endi YANGI ma'lumot (jumladan yangi havola) bilan ustidan yoziladi,
    eski (eskirgan) qator saqlanib qolmaydi."""
    db = await get_db()
    await db.execute(
        "INSERT INTO channels (channel_id, title, link, type) "
        "VALUES (?, ?, ?, 'telegram') "
        "ON CONFLICT(channel_id) DO UPDATE SET "
        "title=excluded.title, link=excluded.link, type=excluded.type",
        (save_ch_id, title, link)
    )
    await db.commit()
    await state.clear()
    title_safe = md_escape(title)
    await message.answer(
        f"✅ *Kanal/guruh qo'shildi!*\n\n"
        f"🏷 Nomi: *{title_safe}*\n"
        f"📌 Turi: {type_label}\n"
        f"🆔 ID: `{save_ch_id}`\n"
        f"🔗 Havola: `{link}`\n\n"
        f"ℹ️ Yopiq kanal bo'lsa: bot qo'shilish so'rovlarini *avtomatik "
        f"tasdiqlamaydi* — admin qo'lda tasdiqlaydi. Lekin foydalanuvchi "
        f"so'rov yuborgani bilanoq botdan foydalanish uchun yetarli deb "
        f"hisoblanadi (\"✅ Tekshirish\" tugmasi ishlaydi).",
        parse_mode="Markdown",
        reply_markup=admin_menu(message.from_user.id)
    )


async def ask_backup_link(
    message: types.Message, state: FSMContext,
    chat_id_int: int, can_invite: bool, title: str, type_label: str
):
    """
    Yopiq kanal uchun adminDAN taklif havolasini so'raydi.

    MUHIM: /avto orqali bot yaratgan havola endi har doim
    `creates_join_request=True` bilan yaratiladi — ya'ni shu havoladan
    kirgan foydalanuvchi to'g'ridan-to'g'ri A'ZO BO'LMAYDI, balki
    "qo'shilish so'rovi" yuboradi va admin buni qo'lda tasdiqlashi
    kerak bo'ladi (aynan shuni admin so'ragan edi).

    Agar admin havolani qo'lda (Telegramdan nusxalab) yuborsa — bu holda
    o'sha havola qanday sozlab yaratilgan bo'lsa, xuddi shunday ishlaydi:
    agar u yaratilganda "So'rov orqali qo'shish" yoqilmagan bo'lsa,
    foydalanuvchi to'g'ridan-to'g'ri a'zo bo'lib ketadi. Shu sababli
    quyida buni ochiq ogohlantiramiz va /avto ni tavsiya qilamiz.
    """
    await state.update_data(
        pending_chat_id=chat_id_int,
        pending_title=title,
        pending_type_label=type_label,
        pending_can_invite=can_invite,
    )
    await state.set_state(AdminChannel.waiting_for_backup_link)

    if can_invite:
        hint = (
            "✅ *Tavsiya:* /avto deb yozing — bot o'zi \"qo'shilish so'rovi\" "
            "talab qiladigan havola yaratadi (hech kim to'g'ridan-to'g'ri "
            "a'zo bo'lolmaydi)."
        )
    else:
        hint = (
            "⚠️ *Diqqat:* botda hozircha \"Foydalanuvchilarni havola orqali "
            "qo'shish\" huquqi yo'q — shu sabab bot /avto havola yarata "
            "OLMAYDI. Havolani albatta qo'lda yuboring."
        )

    await message.answer(
        f"🔗 *{md_escape(title)}* — yopiq kanal uchun taklif havolasini yuboring.\n\n"
        f"{hint}\n\n"
        "❗️ Agar havolani o'zingiz qo'lda yubormoqchi bo'lsangiz: Telegram'da "
        "kanal → Havolalar (Invite Links) → \"Yangi havola yaratish\" → "
        "*\"So'rov orqali qo'shish\"* (Request admin's approval) tugmasini "
        "SHART yoqing — aks holda foydalanuvchi to'g'ridan-to'g'ri a'zo "
        "bo'lib ketadi va \"qo'shilish so'rovi\" umuman ishlamaydi.\n\n"
        "Bekor qilish: /bekor",
        parse_mode="Markdown"
    )


@dp.message(F.text == "📢 Kanal qo'shish", StateFilter("*"))
async def add_channel_start(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📢 Telegram kanal/guruh",
            callback_data="chtype_telegram"
        )],
        [InlineKeyboardButton(
            text="📸 Instagram / boshqa tashqi havola",
            callback_data="chtype_instagram"
        )],
    ])
    await state.set_state(AdminChannel.waiting_for_type)
    await message.answer(
        "Qanday turdagi kanal/havola qo'shmoqchisiz?",
        reply_markup=kb
    )

@dp.callback_query(F.data == "chtype_telegram", AdminChannel.waiting_for_type)
async def add_channel_type_telegram(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(ch_type="telegram")
    await state.set_state(AdminChannel.waiting_for_id)
    await call.message.answer(
        "📢 *Telegram kanal yoki guruh qo'shish*\n\n"
        "Quyidagilardan birini yuboring:\n"
        "• Kanal username: @mening\\_kanalim\n"
        "• Kanal ID raqami: -1001234567890\n"
        "• t.me havolasi: https://t.me/mening\\_kanalim\n"
        "• Yopiq kanal taklifi: https://t.me/+AbCdEfGh1234\n\n"
        "ℹ️ _Bot kanalga admin qilib qo'shilgan bo'lishi shart!_\n\n"
        "⚠️ *Yopiq (maxfiy) kanal bo'lsa:* botga \"Foydalanuvchilarni havola "
        "orqali qo'shish\" (Invite Users via Link) huquqini bering — aks "
        "holda bot foydalanuvchilarning \"qo'shilish so'rovi\"larini qabul "
        "qila olmaydi. Keyingi qadamda bot sizdan kanal havolasini ham "
        "so'raydi.\n\n"
        "Bekor qilish: /bekor",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await call.answer()

@dp.message(AdminChannel.waiting_for_id)
async def add_channel_get_id(message: types.Message, state: FSMContext):
    raw = message.text.strip()

    chat_id_for_api, full_link, is_invite = parse_channel_input(raw)

    # ── Yopiq kanal invite havolasi ──
    if is_invite:
        # Faqat havola matnini saqlash YETARLI EMAS: statik taklif havolasi
        # vaqt o'tishi, foydalanish limiti yoki qayta generatsiya qilinishi
        # sababli "eskirgan havola" bo'lib qoladi va bot a'zolikni Telegram
        # API orqali hech qachon tekshira olmaydi. Shuning uchun kanalning
        # HAQIQIY (raqamli) ID'sini aniqlashimiz kerak — shunda bot: (1) har
        # safar yangi, eskirmaydigan taklif havolasi yaratadi, (2) a'zolikni
        # real vaqtda tekshiradi.
        await state.update_data(invite_link=full_link)
        await state.set_state(AdminChannel.waiting_for_invite_resolve)
        await message.answer(
            "🔒 Bu — yopiq kanalning *taklif havolasi*.\n\n"
            "Yopiq kanallar uchun bot kanalning *haqiqiy ID raqamini* bilishi shart "
            "— aks holda havola vaqt o'tib \"eskirgan\" bo'lib qoladi va "
            "a'zolikni tekshira olmaydi.\n\n"
            "Iltimos quyidagilardan *birini* bajaring:\n"
            "1️⃣ Botni shu kanalga *administrator* qilib qo'shing, so'ng o'sha "
            "kanaldan istalgan xabarni shu yerga *forward* (uzatib) yuboring — "
            "ID avtomatik aniqlanadi.\n"
            "2️⃣ Yoki kanal ID raqamini qo'lda yuboring (masalan: `-1001234567890`).\n\n"
            "Bekor qilish: /bekor",
            parse_mode="Markdown"
        )
        return

    # ── Telegram kanal/guruh: API orqali tekshirish ──
    chat = await safe_get_chat(chat_id_for_api)

    if not chat:
        if isinstance(chat_id_for_api, str) and chat_id_for_api.startswith("@"):
            alt = chat_id_for_api.lstrip("@")
            chat = await safe_get_chat(alt)

    if not chat:
        await message.answer(
            "❌ Bot bu kanal/guruhni topa olmadi.\n\n"
            "*Tekshiring:*\n"
            "1. Username yoki havola to'g'ri yozilganmi?\n"
            "2. Bot kanalga admin qilib qo'shilganmi?\n"
            "3. Maxsus belgilar bo'lsa, ID raqamini (-100...) ishlating\n\n"
            "Qaytadan yuboring yoki /bekor deb yozing.",
            parse_mode="Markdown"
        )
        return

    ch_type_detected = getattr(chat, "type", None)

    if ch_type_detected in ("channel", "group", "supergroup"):
        title = chat.title or str(chat.id)
        uname = f"@{chat.username}" if getattr(chat, "username", None) else None

        type_label = {
            "channel": "📢 Kanal",
            "group": "👥 Guruh",
            "supergroup": "👥 Guruh (supergroup)"
        }.get(ch_type_detected, "Chat")

        uname_safe = md_escape(uname) if uname else "_(username yo'q)_"
        title_safe = md_escape(title)

        # Botning admin holati va "havola orqali qo'shish" huquqini tekshiramiz
        is_admin_here = False
        can_invite = False
        try:
            me_member = await asyncio.wait_for(
                bot.get_chat_member(chat.id, (await bot.get_me()).id),
                timeout=API_TIMEOUT
            )
            is_admin_here = me_member.status in ("administrator", "creator")
            if me_member.status == "creator":
                can_invite = True
            else:
                can_invite = bool(getattr(me_member, "can_invite_users", False))
        except Exception as e:
            logging.warning(f"Bot admin holatini tekshirib bo'lmadi ({chat.id}): {e}")

        if not is_admin_here:
            await state.clear()
            await message.answer(
                f"✅ *Aniqlandi!*\n\n"
                f"🏷 Nomi: *{title_safe}*\n"
                f"📌 Turi: {type_label}\n"
                f"👤 Username: {uname_safe}\n\n"
                "⚠️ *DIQQAT:* Bot bu kanalda hali *administrator* emas!\n"
                "Botni admin qilib qo'shing, so'ng \"📢 Kanal qo'shish\" dan "
                "qaytadan urinib ko'ring.",
                parse_mode="Markdown",
                reply_markup=admin_menu(message.from_user.id)
            )
            return

        if uname:
            # Ochiq kanal: o'zgarmas, hech qachon eskirmaydigan t.me/username havolasi
            link = f"https://t.me/{uname.lstrip('@')}"
            save_ch_id = uname
            await save_telegram_channel(message, state, save_ch_id, title, link, type_label)
            return

        # Yopiq kanal (username yo'q): ID to'g'ridan-to'g'ri yuborilgan bo'lsa
        # ham, endi bot HAVOLANI ADMINDAN HAM SO'RAYDI — faqat o'zining
        # avtomatik yaratgan havolasiga ishonib qolmaydi. Shu tufayl
        # "havola eskirgan" muammosi oldini oladi: agar botning huquqi
        # yetarli bo'lmasa yoki avtomatik yaratish muvaffaqiyatsiz bo'lsa,
        # adminning o'zi bergan joriy (eskirmagan) havola ishlatiladi.
        await ask_backup_link(message, state, chat.id, can_invite, title, type_label)
        return

    else:
        # Bot yoki oddiy foydalanuvchi
        uname = getattr(chat, "username", None)
        if not uname and isinstance(chat_id_for_api, str):
            uname = chat_id_for_api.lstrip("@")

        is_real_bot = bool(uname) and uname.lower().endswith("bot")

        if not is_real_bot:
            await message.answer(
                "❌ Bu — bot emas, oddiy foydalanuvchi profili ko'rinadi.\n\n"
                "Majburiy obuna faqat *kanal*, *guruh* yoki *bot* uchun qo'shiladi.\n"
                "Agar bu chindan ham bot bo'lsa, uning username doim "
                "`...bot` bilan tugashi kerak.\n\n"
                "Qaytadan yuboring yoki /bekor deb yozing.",
                parse_mode="Markdown"
            )
            return

        title = (
            getattr(chat, "full_name", None)
            or getattr(chat, "first_name", None)
            or uname
            or str(chat.id)
        )
        link = f"https://t.me/{uname}" if uname else ""
        ch_id = f"bot_{uname or int(datetime.now().timestamp())}"

        db = await get_db()
        await db.execute(
            "INSERT INTO channels (channel_id, title, link, type) "
            "VALUES (?, ?, ?, 'bot') "
            "ON CONFLICT(channel_id) DO UPDATE SET "
            "title=excluded.title, link=excluded.link, type=excluded.type",
            (ch_id, title, link)
        )
        await db.commit()
        await state.clear()
        uname_safe = md_escape(uname) if uname else "yo'q"
        title_safe = md_escape(title)
        no_link = "_(yo'q)_"
        link_display = f"`{link}`" if link else no_link
        await message.answer(
            f"✅ *Telegram bot qo'shildi!*\n\n"
            f"🤖 Nomi: *{title_safe}*\n"
            f"👤 Username: @{uname_safe}\n"
            f"🔗 Havola: {link_display}\n\n"
            f"ℹ️ Foydalanuvchilar botga o'tib, keyin "
            f"\"✅ Tekshirish\" tugmasini bosib tasdiqlaydi.",
            parse_mode="Markdown",
            reply_markup=admin_menu(message.from_user.id)
        )

@dp.message(AdminChannel.waiting_for_backup_link)
async def add_channel_backup_link(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id_int = data.get("pending_chat_id")
    title = data.get("pending_title") or ""
    type_label = data.get("pending_type_label") or "📢 Kanal"
    can_invite = bool(data.get("pending_can_invite"))

    if chat_id_int is None:
        await state.clear()
        await message.answer(
            "❌ Xatolik yuz berdi, qaytadan boshlang: \"📢 Kanal qo'shish\".",
            reply_markup=admin_menu(message.from_user.id)
        )
        return

    raw = (message.text or "").strip()
    use_auto = raw.lower() in ("/avto", "avto", "/auto")

    link = None
    if not use_auto:
        decoded = unquote(raw)
        is_tme = any(
            decoded.lower().startswith(p)
            for p in ("https://t.me/", "http://t.me/", "t.me/")
        )
        if not is_tme:
            extra = ", yoki /avto deb yozing." if can_invite else "."
            await message.answer(
                "❌ Bu to'g'ri taklif havolasiga o'xshamaydi.\n\n"
                f"`https://t.me/+...` ko'rinishidagi havola yuboring{extra}\n\n"
                "Bekor qilish: /bekor",
                parse_mode="Markdown"
            )
            return
        path = clean_tme_path(decoded)
        link = "https://t.me/" + path

    if link is None:
        if not can_invite:
            await message.answer(
                "⛔ Botda taklif havolasi yaratish huquqi yo'q va siz ham "
                "havola yubormadingiz.\n\nIltimos havolani qo'lda yuboring "
                "yoki /bekor deb yozing.",
                parse_mode="Markdown"
            )
            return
        try:
            invite = await asyncio.wait_for(
                bot.create_chat_invite_link(
                    chat_id_int,
                    creates_join_request=True
                ),
                timeout=API_TIMEOUT
            )
            link = invite.invite_link
        except Exception as e:
            logging.error(f"Invite link yaratib bo'lmadi ({chat_id_int}): {e}")
            await message.answer(
                f"❌ Avtomatik havola yaratib bo'lmadi: `{e}`\n\n"
                "Iltimos havolani qo'lda yuboring yoki /bekor deb yozing.",
                parse_mode="Markdown"
            )
            return

    await save_telegram_channel(
        message, state, str(chat_id_int), title, link, type_label
    )


@dp.message(AdminChannel.waiting_for_invite_resolve)
async def add_channel_invite_resolve(message: types.Message, state: FSMContext):
    data = await state.get_data()
    invite_link = data.get("invite_link", "")

    chat_id_int = None

    # 1) Kanaldan forward qilingan xabar bo'lsa — undan chat ID olamiz
    fwd_chat = getattr(message, "forward_from_chat", None)
    if fwd_chat is not None:
        chat_id_int = fwd_chat.id
    else:
        # 2) Yoki admin to'g'ridan-to'g'ri raqamli ID yuborgan bo'lishi mumkin
        raw = (message.text or "").strip()
        if re.match(r'^-?\d+$', raw):
            chat_id_int = int(raw)

    if chat_id_int is None:
        await message.answer(
            "❌ Kanal ID'ini aniqlab bo'lmadi.\n\n"
            "• Kanaldan xabar *forward* qiling (bot o'sha kanalda admin bo'lishi shart), "
            "yoki\n"
            "• Kanal ID raqamini yuboring (masalan: `-1001234567890`).\n\n"
            "Bekor qilish: /bekor",
            parse_mode="Markdown"
        )
        return

    # ID topilgach — bot haqiqatan ham shu kanalni ko'ra oladimi, tekshiramiz
    chat = await safe_get_chat(chat_id_int)
    if not chat:
        await message.answer(
            "❌ Bot bu kanalni topa olmadi.\n\n"
            "Bot kanalga *administrator* qilib qo'shilganini tekshirib, "
            "qaytadan urinib ko'ring yoki /bekor deb yozing.",
            parse_mode="Markdown"
        )
        return

    # Bot shu kanalda admin ekanini VA aniq "havola orqali qo'shish" huquqiga
    # ega ekanini tekshiramiz — aks holda create_chat_invite_link ishlamaydi
    is_admin_here = False
    can_invite = False
    try:
        me_member = await asyncio.wait_for(
            bot.get_chat_member(chat.id, (await bot.get_me()).id),
            timeout=API_TIMEOUT
        )
        is_admin_here = me_member.status in ("administrator", "creator")
        # Creator uchun bu huquq har doim bor; administrator uchun aniq
        # can_invite_users maydoni tekshiriladi
        if me_member.status == "creator":
            can_invite = True
        else:
            can_invite = bool(getattr(me_member, "can_invite_users", False))
    except Exception as e:
        logging.warning(f"Bot admin holatini tekshirib bo'lmadi ({chat.id}): {e}")

    if not is_admin_here:
        await message.answer(
            "⚠️ Bot bu kanalda hali *administrator* emas.\n\n"
            "Botni kanalga admin qilib qo'shing (\"Taklif havolalari orqali qo'shish\" "
            "huquqi bilan), so'ng shu xabarni qaytadan yuboring yoki forward qiling.",
            parse_mode="Markdown"
        )
        return

    title = chat.title or invite_link or str(chat.id)
    uname = f"@{chat.username}" if getattr(chat, "username", None) else None

    if uname:
        # Ochiq kanal (username bor): doim yangilanadigan, eskirmaydigan
        # t.me/username havolasi ishlatiladi — havola so'rashga hojat yo'q.
        save_ch_id = uname
        link_to_store = f"https://t.me/{uname.lstrip('@')}"
        await save_telegram_channel(
            message, state, save_ch_id, title, link_to_store, "📢 Yopiq kanal"
        )
        return

    # Yopiq kanal (username yo'q): endi HAVOLANI ADMINDAN HAM SO'RAYMIZ —
    # bot o'zining avtomatik yaratgan havolasiga yolg'iz ishonib qolmaydi.
    # Shu tufayl "havola eskirgan" muammosi bartaraf etiladi.
    await ask_backup_link(message, state, chat.id, can_invite, title, "📢 Yopiq kanal")

@dp.callback_query(F.data == "chtype_instagram", AdminChannel.waiting_for_type)
async def add_channel_type_instagram(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(ch_type="instagram")
    await state.set_state(AdminChannel.waiting_for_manual_title)
    await call.message.answer(
        "📸 *Instagram yoki boshqa tashqi havola qo'shish*\n\n"
        "1. Bu havola uchun nom kiriting:\n"
        "Masalan: Instagram sahifamiz yoki TikTok kanalimiz",
        parse_mode="Markdown"
    )
    await call.answer()

@dp.message(AdminChannel.waiting_for_manual_title)
async def add_channel_manual_title(message: types.Message, state: FSMContext):
    await state.update_data(manual_title=message.text.strip())
    await state.set_state(AdminChannel.waiting_for_manual_link)
    await message.answer(
        "2. Havolani (link) yuboring:\n"
        "Masalan: https://instagram.com/mening\\_sahifam\n\n"
        "ℹ️ *Eslatma:* Instagram havolalarga a'zolikni Telegram orqali tekshirib bo'lmaydi.\n"
        "Foydalanuvchi havolani ochib, keyin \"✅ Tekshirish\" tugmasini bosadi.",
        parse_mode="Markdown"
    )

@dp.message(AdminChannel.waiting_for_manual_link)
async def add_channel_manual_finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    title = data.get("manual_title", "Havola")
    link = message.text.strip()
    ch_type = data.get("ch_type", "instagram")

    if not (link.startswith("http://") or link.startswith("https://")):
        await message.answer(
            "❌ Iltimos to'liq havola yuboring:\nMasalan: https://instagram.com/...",
            parse_mode="Markdown"
        )
        return

    ch_id = f"{ch_type}_{int(datetime.now().timestamp())}"
    db = await get_db()
    await db.execute(
        "INSERT INTO channels (channel_id, title, link, type) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET "
        "title=excluded.title, link=excluded.link, type=excluded.type",
        (ch_id, title, link, ch_type)
    )
    await db.commit()
    icon = "📸" if ch_type == "instagram" else "🔗"
    await state.clear()
    title_safe = md_escape(title)
    await message.answer(
        f"✅ *Havola qo'shildi!*\n\n"
        f"{icon} Nomi: *{title_safe}*\n"
        f"🔗 `{link}`\n\n"
        f"ℹ️ Foydalanuvchilar havolani ochib, \"✅ Tekshirish\" tugmasini bosib tasdiqlaydi.",
        parse_mode="Markdown",
        reply_markup=admin_menu(message.from_user.id)
    )


# ─── KANAL O'CHIRISH ──────────────────────────────────────────────────────────

@dp.message(F.text == "🗑 Kanal o'chirish", StateFilter("*"))
async def list_del_channels(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    db = await get_db()
    async with db.execute(
        "SELECT id, title, channel_id, type FROM channels"
    ) as cur:
        channels = await cur.fetchall()
    if not channels:
        await message.answer("Kanallar mavjud emas.")
        return

    type_icons = {"telegram": "📢", "bot": "🤖", "instagram": "📸", "manual": "🔗"}
    buttons = [
        [InlineKeyboardButton(
            text=f"❌ {type_icons.get(row['type'], '🔗')} {row['title']}",
            callback_data=f"del_ch_{row['id']}"
        )]
        for row in channels
    ]
    await message.answer(
        "O'chirmoqchi bo'lgan kanalni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@dp.callback_query(F.data.startswith("del_ch_"))
async def delete_channel_cb(call: types.CallbackQuery):
    if not await is_bot_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    ch_db_id = int(call.data.split("_")[2])
    db = await get_db()
    async with db.execute(
        "SELECT title FROM channels WHERE id=?", (ch_db_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        await call.answer("Kanal topilmadi.", show_alert=True)
        return
    await db.execute("DELETE FROM channels WHERE id=?", (ch_db_id,))
    await db.commit()
    await call.message.answer(
        f"✅ *{md_escape(row['title'])}* kanali o'chirildi.",
        parse_mode="Markdown",
        reply_markup=admin_menu(call.from_user.id)
    )
    await call.answer()

@dp.message(F.text == "📋 Kanallar ro'yxati", StateFilter("*"))
async def list_channels(message: types.Message, state: FSMContext):
    if not await is_bot_admin(message.from_user.id):
        return
    await state.clear()
    db = await get_db()
    async with db.execute(
        "SELECT channel_id, title, link, type FROM channels"
    ) as cur:
        channels = await cur.fetchall()
    if not channels:
        await message.answer("Hozircha kanallar qo'shilmagan.")
        return

    type_icons = {"telegram": "📢", "bot": "🤖", "instagram": "📸", "manual": "🔗"}
    text = "📋 *Kanallar ro'yxati:*\n\n"
    refresh_buttons = []
    for ch in channels:
        icon = type_icons.get(ch["type"], "🔗")
        link_str = f"`{ch['link']}`" if ch["link"] else "_(havola yo'q)_"
        ch_id_str = md_escape(str(ch["channel_id"]))
        title_str = md_escape(ch["title"])
        text += (
            f"{icon} *{title_str}*\n"
            f"  🆔 `{ch_id_str}`\n"
            f"  🔗 {link_str}\n\n"
        )
        # Faqat yopiq (raqamli ID) Telegram kanallar uchun havolani
        # yangilash imkoniyati beramiz — @username kanallarga kerak emas
        if ch["type"] == "telegram" and not str(ch["channel_id"]).startswith("@"):
            short_title = ch["title"][:28]
            refresh_buttons.append([InlineKeyboardButton(
                text=f"🔄 {short_title}",
                callback_data=f"refresh_link_{ch['channel_id']}"
            )])

    kb = None
    if refresh_buttons:
        text += "ℹ️ _Yopiq kanal havolasi ishlamay qolsa, quyidan yangilang:_"
        kb = InlineKeyboardMarkup(inline_keyboard=refresh_buttons)
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("refresh_link_"))
async def refresh_channel_link_cb(call: types.CallbackQuery):
    if not await is_bot_admin(call.from_user.id):
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    ch_id = call.data[len("refresh_link_"):]
    try:
        chat_id_int = int(ch_id)
    except ValueError:
        await call.answer("❌ Noto'g'ri kanal ID.", show_alert=True)
        return

    try:
        invite = await asyncio.wait_for(
            bot.create_chat_invite_link(
                chat_id_int,
                creates_join_request=True
            ),
            timeout=API_TIMEOUT
        )
        new_link = invite.invite_link
    except Exception as e:
        logging.error(f"Havolani yangilashda xato ({ch_id}): {e}")
        await call.answer(
            "❌ Yangi havola yaratib bo'lmadi. Botga kanalda \"Foydalanuvchilarni "
            "havola orqali qo'shish\" huquqi berilganini tekshiring.",
            show_alert=True
        )
        return

    db = await get_db()
    await db.execute("UPDATE channels SET link=? WHERE channel_id=?", (new_link, ch_id))
    await db.commit()
    await call.answer("✅ Yangi havola yaratildi!", show_alert=True)
    await call.message.answer(f"🔗 Yangi havola:\n{new_link}")


# ─── NARX O'ZGARTIRISH ───────────────────────────────────────────────────────

@dp.message(F.text == "💰 Premium narxini o'zgartirish", StateFilter("*"))
async def start_price_change(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    await state.clear()
    price = await get_premium_price(30)
    await state.set_state(AdminPriceChange.waiting_for_price)
    await message.answer(
        f"💰 Hozirgi narx: *{price}* so'm\n\nYangi narxni kiriting (masalan: 25000):",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(AdminPriceChange.waiting_for_price)
async def finish_price_change(message: types.Message, state: FSMContext):
    new_price = message.text.strip().replace(" ", "")
    if not new_price.isdigit():
        await message.answer("❌ Iltimos faqat raqam kiriting (masalan: 25000).")
        return
    formatted = f"{int(new_price):,}"
    await set_premium_price(formatted, 30)
    await state.clear()
    await message.answer(
        f"✅ Premium narxi *{formatted} so'm* qilib o'zgartirildi.",
        reply_markup=admin_menu(message.from_user.id), parse_mode="Markdown"
    )


# ─── KARTA O'ZGARTIRISH ───────────────────────────────────────────────────────

@dp.message(F.text == "💳 Karta raqamini o'zgartirish", StateFilter("*"))
async def start_card_change(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    await state.clear()
    card_number, card_holder = await get_card_info()
    holder_line = f"\n👤 Hozirgi egasi: {md_escape(card_holder)}" if card_holder else ""
    await state.set_state(AdminCardChange.waiting_for_number)
    await message.answer(
        f"💳 Hozirgi karta raqami: `{card_number}`{holder_line}\n\n"
        "Yangi karta raqamini kiriting (masalan: 8600 1234 5678 9012):",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(AdminCardChange.waiting_for_number)
async def process_card_number(message: types.Message, state: FSMContext):
    await state.update_data(card_number=message.text.strip())
    await state.set_state(AdminCardChange.waiting_for_holder)
    await message.answer("👤 Endi karta egasining F.I.Sh (ism-familiyasi)ni kiriting:")

@dp.message(AdminCardChange.waiting_for_holder)
async def process_card_holder(message: types.Message, state: FSMContext):
    data = await state.get_data()
    card_number = data.get("card_number")
    card_holder = message.text.strip()
    await set_card_info(card_number, card_holder)
    await state.clear()
    await message.answer(
        f"✅ Karta ma'lumotlari yangilandi!\n\n"
        f"💳 `{card_number}`\n"
        f"👤 {md_escape(card_holder)}",
        parse_mode="Markdown",
        reply_markup=admin_menu(message.from_user.id)
    )


# ─── ADMINLARNI BOSHQARISH ────────────────────────────────────────────────────

@dp.message(F.text == "👨‍💼 Adminlar", StateFilter("*"))
async def admins_panel(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    await state.clear()
    admins = await get_admins_list()
    text = "👨‍💼 *Adminlar ro'yxati:*\n\n"
    text += f"👑 Asosiy admin: `{ADMIN_ID}`\n\n"
    if admins:
        for row in admins:
            uname_part = f"@{md_escape(row['username'])}" if row["username"] else "username yo'q"
            text += f"🔹 `{row['user_id']}` — {uname_part} ({row['added_at']})\n"
    else:
        text += "Qo'shimcha adminlar yo'q."

    if message.from_user.id == ADMIN_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="➕ Admin qo'shish", callback_data="add_admin_start"
            )],
            [InlineKeyboardButton(
                text="➖ Admin olib tashlash", callback_data="remove_admin_start"
            )]
        ])
        await message.answer(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="Markdown")

@dp.callback_query(F.data == "add_admin_start")
async def add_admin_start_cb(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        await call.answer(
            "⛔ Faqat asosiy admin yangi admin qo'sha oladi.", show_alert=True
        )
        return
    await state.set_state(AdminManage.waiting_for_add_id)
    await call.message.answer("➕ Yangi adminning Telegram ID raqamini yuboring:")
    await call.answer()

@dp.message(AdminManage.waiting_for_add_id)
async def process_add_admin(message: types.Message, state: FSMContext):
    try:
        new_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Noto'g'ri format. Faqat raqam (ID) yuboring.")
        return
    if new_id == ADMIN_ID or await is_bot_admin(new_id):
        await message.answer("⚠️ Bu foydalanuvchi allaqachon admin.")
        await state.clear()
        return
    chat = await safe_get_chat(new_id)
    uname = getattr(chat, "username", "") if chat else ""
    await add_admin(new_id, uname or "")
    await state.clear()
    await message.answer(
        f"✅ `{new_id}` endi admin!",
        parse_mode="Markdown",
        reply_markup=admin_menu(message.from_user.id)
    )
    try:
        await bot.send_message(
            new_id, "🎉 Sizga bot admin huquqi berildi! /admin buyrug'ini yuboring."
        )
    except Exception:
        pass

@dp.callback_query(F.data == "remove_admin_start")
async def remove_admin_start_cb(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer(
            "⛔ Faqat asosiy admin admin olib tashlay oladi.", show_alert=True
        )
        return
    admins = await get_admins_list()
    if not admins:
        await call.answer("Qo'shimcha adminlar yo'q.", show_alert=True)
        return
    kb_rows = []
    for row in admins:
        label = f"@{row['username']}" if row["username"] else str(row["user_id"])
        kb_rows.append([InlineKeyboardButton(
            text=f"❌ {label}",
            callback_data=f"rm_admin_{row['user_id']}"
        )])
    await call.message.answer(
        "Olib tashlamoqchi bo'lgan adminni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )
    await call.answer()

@dp.callback_query(F.data.startswith("rm_admin_"))
async def process_remove_admin(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("⛔ Ruxsat yo'q.", show_alert=True)
        return
    target_id = int(call.data.split("_")[2])
    await remove_admin(target_id)
    await call.message.answer(
        f"✅ `{target_id}` admin huquqidan olib tashlandi.", parse_mode="Markdown"
    )
    await call.answer()
    try:
        await bot.send_message(
            target_id, "ℹ️ Sizning bot admin huquqingiz olib tashlandi."
        )
    except Exception:
        pass


# ─── MAXFIY KANAL: A'ZOLIKKA SO'ROV (JOIN REQUEST) ───────────────────────────

async def resolve_tracked_channel_id(chat_id: int, username: str = None):
    """
    Berilgan chat botning `channels` bazasida (type='telegram') qanday
    channel_id bilan saqlanganini topadi (raqamli ID yoki @username
    ko'rinishida) va shuni qaytaradi. Topilmasa None qaytaradi — bu holda
    kanal botga aloqasi yo'q, aralashmaymiz.
    """
    db = await get_db()
    async with db.execute(
        "SELECT channel_id FROM channels WHERE type='telegram'"
    ) as cur:
        rows = await cur.fetchall()
    ids = {row["channel_id"] for row in rows}
    if str(chat_id) in ids:
        return str(chat_id)
    if username and f"@{username}" in ids:
        return f"@{username}"
    return None

@dp.chat_join_request()
async def handle_join_request(join_request: types.ChatJoinRequest):
    """
    Kanalda "Yangi a'zolarni tasdiqlash" (Approve New Members) yoqilgan
    bo'lsa, taklif havolasini bosgan foydalanuvchi darhol a'zo bo'lmaydi —
    Telegram "qo'shilish so'rovi" yaratadi va kanal administratori buni
    QO'LDA tasdiqlaydi (bot AVTOMATIK tasdiqlamaydi).

    Lekin botdan foydalanish uchun buni kutish shart emas: foydalanuvchi
    so'rov YUBORGANI botga yetarli dalil sifatida hisoblanadi va
    check_subscriptions uni "obuna bo'lgan" deb qabul qiladi.
    """
    chat = join_request.chat
    user = join_request.from_user

    ch_id = await resolve_tracked_channel_id(chat.id, getattr(chat, "username", None))
    if not ch_id:
        return  # Botga aloqasi bo'lmagan kanal — aralashmaymiz

    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO manual_confirmations (user_id, channel_id, confirmed_at) "
        "VALUES (?, ?, ?)",
        (user.id, ch_id, datetime.now().isoformat())
    )
    await db.commit()
    logging.info(f"Join so'rovi qayd etildi (avtomatik tasdiqlanmadi): chat={chat.id} user={user.id}")

    # Foydalanuvchiga botdan qisqa xabar (ixtiyoriy, botni bloklagan bo'lsa xato bermaydi)
    try:
        await bot.send_message(
            user.id,
            f"✅ *{md_escape(chat.title or 'Kanal')}* ga qo'shilish so'rovingiz qabul qilindi!\n\n"
            f"Administrator tez orada tasdiqlaydi. Hozircha botdan foydalanishingiz mumkin — "
            f"\"✅ Tekshirish\" tugmasini bosing.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.warning(f"Join-request foydalanuvchiga xabar yuborilmadi ({user.id}): {e}")


# ─── COMMANDS ────────────────────────────────────────────────────────────────

@dp.message(Command("premium"))
async def cmd_premium(message: types.Message):
    await premium_info(message)

@dp.message(Command("izlash"))
async def cmd_izlash(message: types.Message):
    await search_start(message)

@dp.message(Command("suhbat"))
async def cmd_suhbat(message: types.Message, state: FSMContext):
    await chat_panel(message, state)

@dp.message(Command("profil"))
async def cmd_profil(message: types.Message):
    await my_profile(message)


# ─── TANILMAGAN XABARLAR (CATCH-ALL) ─────────────────────────────────────────

@dp.message(StateFilter(None))
async def unknown_message(message: types.Message):
    """
    Hech qaysi handlerga tushmaydigan xabarlarga darhol javob beradi.
    """
    user_id = message.from_user.id

    if await is_bot_admin(user_id):
        await message.answer(
            "ℹ️ Noma'lum buyruq. Menyudan foydalaning:",
            reply_markup=admin_menu(user_id)
        )
        return

    if not await is_premium_user(user_id):
        unsub = await check_subscriptions(user_id)
        if unsub:
            kb = await build_subscription_keyboard(unsub)
            await message.answer(
                "⚠️ Botdan foydalanish uchun quyidagi kanal(lar)ga obuna bo'ling:",
                reply_markup=kb
            )
            return

    if not await has_profile(user_id):
        await message.answer("ℹ️ Iltimos, avval /start bosib profil yarating.")
        return

    await message.answer(
        "ℹ️ Noma'lum buyruq. Quyidagi menyudan foydalaning:",
        reply_markup=main_menu(user_id)
    )


# ─── ISHGA TUSHIRISH ─────────────────────────────────────────────────────────

async def set_commands():
    user_commands = [
        types.BotCommand(command="start",   description="🚀 Botni ishga tushirish"),
        types.BotCommand(command="izlash",  description="🔍 Suhbatdosh izlash"),
        types.BotCommand(command="suhbat",  description="💬 Suhbatlashish"),
        types.BotCommand(command="profil",  description="👤 Profil"),
        types.BotCommand(command="premium", description="🌟 Premium bo'lim"),
    ]
    admin_commands = user_commands + [
        types.BotCommand(command="admin",  description="👨‍💻 Admin panel"),
        types.BotCommand(command="bekor",  description="❌ Amalni bekor qilish"),
    ]
    await bot.set_my_commands(user_commands)
    try:
        await bot.set_my_commands(
            admin_commands,
            scope=types.BotCommandScopeChat(chat_id=ADMIN_ID)
        )
    except Exception as e:
        logging.warning(f"Admin buyruqlari o'rnatilmadi: {e}")

@dp.errors()
async def global_error_handler(event: types.ErrorEvent):
    """
    Har qanday handlerda ushlanmagan kutilmagan xato shu yerga tushadi.
    Buning yo'qligi sabab, ilgari ba'zi xatolar foydalanuvchiga HECH
    QANDAY javob bermay, "sukut bilan" yo'qolib ketardi (masalan tugma
    bosilganda hech narsa bo'lmagandek tuyulishi). Endi bunday holatda
    ham kamida log yoziladi va imkon bo'lsa foydalanuvchi/admin xabardor
    qilinadi.
    """
    # "query is too old" — muddati o'tgan callback query, jimgina o'tkazib yubor
    if isinstance(event.exception, TelegramBadRequest) and "query is too old" in str(event.exception):
        logging.warning(f"Muddati o'tgan callback query e'tiborsiz qoldirildi: {event.exception}")
        return

    logging.error(f"Global xato: {event.exception}", exc_info=event.exception)
    try:
        upd = event.update
        chat_id = None
        if upd.message:
            chat_id = upd.message.chat.id
        elif upd.callback_query and upd.callback_query.message:
            chat_id = upd.callback_query.message.chat.id
            try:
                await upd.callback_query.answer("❌ Xatolik yuz berdi.", show_alert=True)
            except Exception:
                pass
        if chat_id:
            if await is_bot_admin(chat_id):
                await bot.send_message(
                    chat_id,
                    f"❌ Kutilmagan xato yuz berdi.\n\n🛠 {event.exception}"
                )
            else:
                await bot.send_message(
                    chat_id, "❌ Xatolik yuz berdi. Birozdan so'ng qaytadan urinib ko'ring."
                )
    except Exception:
        pass


async def run_bot():
    global BOT_USERNAME

    await init_db()

    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username
        logging.info(f"Bot: @{BOT_USERNAME} (id={me.id})")
    except Exception as e:
        logging.error(f"Bot username olishda xato: {e}")

    try:
        await bot.set_my_short_description("Tanishuv botiga xush kelibsiz!")
        await bot.set_my_description(
            "🚀 Yangi insonlar bilan tanishing!\n"
            "🌟 Premium a'zolik: istalgan profil bilan bevosita bog'lanish!"
        )
        await set_commands()
    except Exception as e:
        logging.error(f"Bot ma'lumotlarini o'rnatishda xato: {e}")

    try:
        main_file = "main.py"
        if not os.path.exists(main_file):
            uploaded_main_file = os.path.join(
                "attached_assets", "main_(4)_1786964552074.py"
            )
            if os.path.exists(uploaded_main_file):
                main_file = uploaded_main_file

        if os.path.exists(main_file):
            await bot.send_document(
                chat_id=ADMIN_ID,
                document=types.FSInputFile(main_file, filename="main.py"),
                caption=(
                    "✅ Botning yangilangan main.py fayli.\n"
                    "✅ Username avtomatik aniqlanadi yoki profil havolasi yaratiladi.\n"
                    "💬 Suhbat menyusi va faol suhbat xabari yangilandi."
                ),
            )
    except Exception as e:
        logging.warning(f"main.py faylini adminga yuborib bo'lmadi: {e}")

    asyncio.create_task(premium_checker())
    asyncio.create_task(backup_scheduler())
    asyncio.create_task(link_refresh_scheduler())

    first_start = True
    while True:
        try:
            logging.info("Bot ishga tushmoqda (polling)...")
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                drop_pending_updates=first_start,
            )
        except Exception as e:
            logging.error(f"Polling to'xtadi: {e}. 5 soniyadan keyin qayta uriniladi...")
            await asyncio.sleep(5)
        finally:
            first_start = False

if __name__ == "__main__":
    asyncio.run(run_bot())
