import asyncio
import logging
import os
import sqlite3
from typing import Optional

import aiosqlite
from aiohttp import web
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN Environment Variable topilmadi. "
        "Render -> Environment Variables -> BOT_TOKEN qo'shing."
    )

PORT = int(os.getenv("PORT", "10000"))

ADMIN_IDS = set()

admin_ids_raw = os.getenv("ADMIN_IDS", "")

for item in admin_ids_raw.split(","):
    item = item.strip()

    if item.isdigit():
        ADMIN_IDS.add(int(item))


REQUIRED_CHAT_ID = os.getenv("REQUIRED_CHAT_ID", "")

REQUIRED_CHAT_LINK = os.getenv("REQUIRED_CHAT_LINK", "")

CATALOG_CHANNEL_LINK = os.getenv(
    "CATALOG_CHANNEL_LINK",
    "https://t.me/wittkino"
)

ADMIN_CONTACT_URL = "https://t.me/wittmen"

DB_NAME = os.getenv("DB_NAME", "kino_bot.db")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# STATUSLAR
# ============================================================

VALID_STATUSES = {
    "USER",
    "VIP",
    "PREMIUM",
    "ADMIN",
}

REMOVE_STATUS_WORDS = {
    "REMOVE",
    "DELETE",
    "OLIBTASHLA",
    "OLIB_TASHLA",
    "OLIBTASHLASH",
}


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_table_columns(cursor, table_name: str):
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def add_column_if_missing(
    cursor,
    table_name: str,
    column_name: str,
    column_definition: str,
):
    columns = get_table_columns(cursor, table_name)

    if column_name not in columns:
        cursor.execute(
            f"ALTER TABLE {table_name} "
            f"ADD COLUMN {column_name} {column_definition}"
        )


async def init_db():
    """
    Database va jadvallarni yaratadi.
    Eski DB bo'lsa, kerakli ustunlarni migration orqali qo'shadi.
    """

    async with aiosqlite.connect(DB_NAME) as db:

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                status TEXT DEFAULT 'USER',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                category TEXT,
                file_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                movie_id INTEGER NOT NULL,
                rating INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, movie_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # ----------------------------------------------------
        # OLD DATABASE MIGRATION
        # ----------------------------------------------------

        cursor = await db.execute("PRAGMA table_info(users)")
        columns = {
            row[1]
            for row in await cursor.fetchall()
        }

        if "status" not in columns:
            await db.execute(
                "ALTER TABLE users "
                "ADD COLUMN status TEXT DEFAULT 'USER'"
            )

        if "username" not in columns:
            await db.execute(
                "ALTER TABLE users "
                "ADD COLUMN username TEXT"
            )

        if "full_name" not in columns:
            await db.execute(
                "ALTER TABLE users "
                "ADD COLUMN full_name TEXT"
            )

        await db.commit()

    logger.info("Database initialized: %s", DB_NAME)


# ============================================================
# USER FUNCTIONS
# ============================================================

async def register_user(user):
    async with aiosqlite.connect(DB_NAME) as db:

        await db.execute("""
            INSERT INTO users (
                id,
                username,
                full_name,
                status
            )
            VALUES (?, ?, ?, 'USER')
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name
        """, (
            user.id,
            user.username,
            user.full_name,
        ))

        await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:

        cursor = await db.execute("""
            SELECT
                id,
                username,
                full_name,
                status,
                created_at
            FROM users
            WHERE id = ?
        """, (user_id,))

        return await cursor.fetchone()


async def get_user_status(user_id: int) -> str:

    # .env dagi adminlar doimiy/root admin
    if user_id in ADMIN_IDS:
        return "ADMIN"

    user = await get_user(user_id)

    if not user:
        return "USER"

    status = user[3]

    if not status:
        return "USER"

    return status.upper()


async def is_admin(user_id: int) -> bool:

    # .env admin
    if user_id in ADMIN_IDS:
        return True

    user = await get_user(user_id)

    if not user:
        return False

    status = (user[3] or "USER").upper()

    return status == "ADMIN"


async def set_user_status(
    user_id: int,
    status: str,
) -> bool:

    status = status.upper()

    if status not in VALID_STATUSES:
        return False

    async with aiosqlite.connect(DB_NAME) as db:

        cursor = await db.execute("""
            UPDATE users
            SET status = ?
            WHERE id = ?
        """, (
            status,
            user_id,
        ))

        await db.commit()

        return cursor.rowcount > 0


# ============================================================
# MOVIE FUNCTIONS
# ============================================================

async def add_movie_to_db(
    code: str,
    name: str,
    description: str,
    category: str,
    file_id: str,
):
    async with aiosqlite.connect(DB_NAME) as db:

        await db.execute("""
            INSERT INTO movies (
                code,
                name,
                description,
                category,
                file_id
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            code,
            name,
            description,
            category,
            file_id,
        ))

        await db.commit()


async def get_movie_by_code(code: str):
    async with aiosqlite.connect(DB_NAME) as db:

        cursor = await db.execute("""
            SELECT
                id,
                code,
                name,
                description,
                category,
                file_id,
                created_at
            FROM movies
            WHERE LOWER(code) = LOWER(?)
        """, (code,))

        return await cursor.fetchone()


async def search_movies(query: str):
    async with aiosqlite.connect(DB_NAME) as db:

        search = f"%{query}%"

        cursor = await db.execute("""
            SELECT
                id,
                code,
                name,
                description,
                category,
                file_id,
                created_at
            FROM movies
            WHERE
                code LIKE ?
                OR name LIKE ?
                OR description LIKE ?
                OR category LIKE ?
            ORDER BY id DESC
            LIMIT 20
        """, (
            search,
            search,
            search,
            search,
        ))

        return await cursor.fetchall()


async def get_latest_movies(limit: int = 10):
    async with aiosqlite.connect(DB_NAME) as db:

        cursor = await db.execute("""
            SELECT
                id,
                code,
                name,
                description,
                category,
                file_id,
                created_at
            FROM movies
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))

        return await cursor.fetchall()


async def get_popular_movies(limit: int = 10):
    async with aiosqlite.connect(DB_NAME) as db:

        cursor = await db.execute("""
            SELECT
                m.id,
                m.code,
                m.name,
                m.description,
                m.category,
                m.file_id,
                COUNT(r.id) AS rating_count
            FROM movies m
            LEFT JOIN ratings r
                ON r.movie_id = m.id
            GROUP BY m.id
            ORDER BY rating_count DESC, m.id DESC
            LIMIT ?
        """, (limit,))

        return await cursor.fetchall()


async def delete_movie_by_code(code: str) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:

        cursor = await db.execute("""
            DELETE FROM movies
            WHERE LOWER(code) = LOWER(?)
        """, (code,))

        await db.commit()

        return cursor.rowcount > 0


# ============================================================
# RATING FUNCTIONS
# ============================================================

async def set_rating(
    user_id: int,
    movie_id: int,
    rating: int,
):
    async with aiosqlite.connect(DB_NAME) as db:

        await db.execute("""
            INSERT INTO ratings (
                user_id,
                movie_id,
                rating
            )
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, movie_id)
            DO UPDATE SET
                rating = excluded.rating
        """, (
            user_id,
            movie_id,
            rating,
        ))

        await db.commit()


async def get_movie_rating(movie_id: int):
    async with aiosqlite.connect(DB_NAME) as db:

        cursor = await db.execute("""
            SELECT
                AVG(rating),
                COUNT(*)
            FROM ratings
            WHERE movie_id = ?
        """, (movie_id,))

        return await cursor.fetchone()


# ============================================================
# KEYBOARDS
# ============================================================

def main_reply_keyboard(is_user_admin: bool):

    buttons = [
        ["🔎 Qidirish", "🎬 Katalog"],
        ["🔥 Mashhur", "🆕 Yangi"],
        ["📂 Kategoriyalar", "👤 Profil"],
    ]

    if is_user_admin:
        buttons.append(["👑 Admin panel"])

    return ReplyKeyboardMarkup(
        buttons,
        resize_keyboard=True,
    )


def admin_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ Kino qo'shish",
                callback_data="admin_add_movie"
            ),
        ],
        [
            InlineKeyboardButton(
                "🗑 Kino o'chirish",
                callback_data="admin_delete"
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 Statistika",
                callback_data="admin_stats"
            ),
            InlineKeyboardButton(
                "👥 Foydalanuvchilar",
                callback_data="admin_users"
            ),
        ],
        [
            InlineKeyboardButton(
                "⭐ Status berish",
                callback_data="admin_status"
            ),
            InlineKeyboardButton(
                "❌ Statusni olib tashlash",
                callback_data="admin_status_remove"
            ),
        ],
        [
            InlineKeyboardButton(
                "📢 Reklama yuborish",
                callback_data="admin_broadcast"
            ),
        ],
    ])


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    await register_user(user)

    user_is_admin = await is_admin(user.id)

    text = (
        "🎬 Kino Botga xush kelibsiz!\n\n"
        "Kerakli filmni qidirish uchun "
        "🔎 Qidirish tugmasidan foydalaning."
    )

    await update.message.reply_text(
        text,
        reply_markup=main_reply_keyboard(user_is_admin),
    )


# ============================================================
# PROFILE
# ============================================================

async def show_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    await register_user(user)

    status = await get_user_status(user.id)

    text = (
        "👤 <b>Sizning profilingiz</b>\n\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"👤 Ism: {user.full_name}\n"
        f"⭐ Status: <b>{status}</b>\n"
    )

    if status == "ADMIN":
        text += "\n👑 Siz adminsiz."
    else:
        text += (
            "\n💡 <b>Statusingizni oshirish uchun "
            "admin bilan bog‘laning.</b>"
        )

    keyboard = None

    if status != "ADMIN":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "👨‍💼 Admin bilan bog‘lanish",
                    url=ADMIN_CONTACT_URL,
                )
            ]
        ])

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ============================================================
# ADMIN PANEL
# ============================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ Sizda admin huquqi yo'q."
        )
        return

    await update.message.reply_text(
        "👑 <b>Admin panel</b>\n\n"
        "Kerakli amalni tanlang:",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_keyboard(),
    )


# ============================================================
# STATUS MANAGEMENT
# ============================================================

async def admin_status_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        await query.message.reply_text(
            "❌ Sizda admin huquqi yo'q."
        )
        return

    context.user_data["status_mode"] = "set"

    await query.message.reply_text(
        "⭐ <b>Status berish</b>\n\n"
        "Quyidagi formatda yuboring:\n\n"
        "<code>ID STATUS</code>\n\n"
        "Misollar:\n"
        "<code>123456789 VIP</code>\n"
        "<code>123456789 PREMIUM</code>\n"
        "<code>123456789 ADMIN</code>\n\n"
        "USER qilib yuborsangiz oddiy foydalanuvchiga qaytadi.",
        parse_mode=ParseMode.HTML,
    )


async def admin_status_remove_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        await query.message.reply_text(
            "❌ Sizda admin huquqi yo'q."
        )
        return

    context.user_data["status_mode"] = "remove"

    await query.message.reply_text(
        "❌ <b>Statusni olib tashlash</b>\n\n"
        "Foydalanuvchi ID sini yuboring:\n\n"
        "<code>123456789</code>\n\n"
        "Status <b>USER</b> ga qaytariladi.",
        parse_mode=ParseMode.HTML,
    )


async def process_status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await is_admin(update.effective_user.id):
        return False

    text = update.message.text.strip()

    parts = text.split()

    if len(parts) != 2:
        return False

    user_id_text = parts[0]
    status = parts[1].upper()

    if not user_id_text.isdigit():
        return False

    if status in REMOVE_STATUS_WORDS:
        status = "USER"

    if status not in VALID_STATUSES:
        return False

    target_id = int(user_id_text)

    # .env adminni DB orqali olib tashlab bo'lmaydi
    if target_id in ADMIN_IDS and status != "ADMIN":
        await update.message.reply_text(
            "⚠️ Bu foydalanuvchi <b>ADMIN_IDS</b> orqali "
            "asosiy admin qilib qo'yilgan.\n\n"
            "Uni status orqali olib tashlab bo'lmaydi.",
            parse_mode=ParseMode.HTML,
        )
        context.user_data.pop("status_mode", None)
        return True

    updated = await set_user_status(
        target_id,
        status,
    )

    if not updated:
        await update.message.reply_text(
            "❌ Bu ID bilan foydalanuvchi topilmadi.\n\n"
            "Foydalanuvchi avval botni ishga tushirgan bo'lishi kerak."
        )

        context.user_data.pop("status_mode", None)
        return True

    if status == "ADMIN":
        message = (
            "👑 <b>ADMIN status berildi!</b>\n\n"
            f"🆔 ID: <code>{target_id}</code>\n"
            "⭐ Status: <b>ADMIN</b>\n\n"
            "Endi u foydalanuvchi avtomatik ravishda "
            "admin panelga kira oladi."
        )

    elif status == "USER":
        message = (
            "✅ Status olib tashlandi.\n\n"
            f"🆔 ID: <code>{target_id}</code>\n"
            "⭐ Status: <b>USER</b>"
        )

    else:
        message = (
            "✅ Status muvaffaqiyatli o'zgartirildi.\n\n"
            f"🆔 ID: <code>{target_id}</code>\n"
            f"⭐ Status: <b>{status}</b>"
        )

    await update.message.reply_text(
        message,
        parse_mode=ParseMode.HTML,
    )

    context.user_data.pop("status_mode", None)

    return True


async def process_remove_status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await is_admin(update.effective_user.id):
        return False

    text = update.message.text.strip()

    if not text.isdigit():
        return False

    target_id = int(text)

    if target_id in ADMIN_IDS:
        await update.message.reply_text(
            "⚠️ Bu asosiy adminni olib tashlab bo'lmaydi."
        )

        context.user_data.pop("status_mode", None)

        return True

    updated = await set_user_status(
        target_id,
        "USER",
    )

    if not updated:
        await update.message.reply_text(
            "❌ Foydalanuvchi topilmadi."
        )

        context.user_data.pop("status_mode", None)

        return True

    await update.message.reply_text(
        "✅ Status olib tashlandi.\n\n"
        f"🆔 ID: <code>{target_id}</code>\n"
        "⭐ Status: <b>USER</b>",
        parse_mode=ParseMode.HTML,
    )

    context.user_data.pop("status_mode", None)

    return True


# ============================================================
# SEARCH
# ============================================================

async def search_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🔎 Kino nomi yoki kodini yuboring."
    )

    context.user_data["search_mode"] = True


async def perform_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.message.text.strip()

    movies = await search_movies(query)

    context.user_data.pop("search_mode", None)

    if not movies:
        await update.message.reply_text(
            "❌ Kino topilmadi."
        )
        return

    buttons = []

    for movie in movies:

        movie_id = movie[0]
        code = movie[1]
        name = movie[2]

        buttons.append([
            InlineKeyboardButton(
                f"🎬 {code} — {name}",
                callback_data=f"movie_{movie_id}",
            )
        ])

    await update.message.reply_text(
        f"🔎 <b>{len(movies)}</b> ta natija topildi:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ============================================================
# SHOW MOVIE
# ============================================================

async def show_movie(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    movie_id: int,
):

    async with aiosqlite.connect(DB_NAME) as db:

        cursor = await db.execute("""
            SELECT
                id,
                code,
                name,
                description,
                category,
                file_id
            FROM movies
            WHERE id = ?
        """, (movie_id,))

        movie = await cursor.fetchone()

    if not movie:
        await update.effective_message.reply_text(
            "❌ Kino topilmadi."
        )
        return

    (
        movie_id,
        code,
        name,
        description,
        category,
        file_id,
    ) = movie

    rating = await get_movie_rating(movie_id)

    avg_rating = rating[0] or 0
    rating_count = rating[1] or 0

    caption = (
        f"🎬 <b>{name}</b>\n\n"
        f"🔢 Kod: <code>{code}</code>\n"
        f"📂 Kategoriya: {category or 'Noma'lum'}\n\n"
        f"📝 {description or 'Tavsif mavjud emas.'}\n\n"
        f"⭐ Reyting: {avg_rating:.1f}/5 "
        f"({rating_count} ta ovoz)"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⭐ 1",
                callback_data=f"rate_{movie_id}_1",
            ),
            InlineKeyboardButton(
                "⭐ 2",
                callback_data=f"rate_{movie_id}_2",
            ),
            InlineKeyboardButton(
                "⭐ 3",
                callback_data=f"rate_{movie_id}_3",
            ),
            InlineKeyboardButton(
                "⭐ 4",
                callback_data=f"rate_{movie_id}_4",
            ),
            InlineKeyboardButton(
                "⭐ 5",
                callback_data=f"rate_{movie_id}_5",
            ),
        ]
    ])

    await update.effective_message.reply_video(
        video=file_id,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ============================================================
# LATEST
# ============================================================

async def latest_movies(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    movies = await get_latest_movies()

    if not movies:
        await update.message.reply_text(
            "❌ Hozircha kino yo'q."
        )
        return

    buttons = []

    for movie in movies:

        movie_id = movie[0]
        code = movie[1]
        name = movie[2]

        buttons.append([
            InlineKeyboardButton(
                f"🎬 {code} — {name}",
                callback_data=f"movie_{movie_id}",
            )
        ])

    await update.message.reply_text(
        "🆕 <b>Yangi kinolar</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ============================================================
# POPULAR
# ============================================================

async def popular_movies(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    movies = await get_popular_movies()

    if not movies:
        await update.message.reply_text(
            "❌ Hozircha reytinglar mavjud emas."
        )
        return

    buttons = []

    for movie in movies:

        movie_id = movie[0]
        code = movie[1]
        name = movie[2]

        buttons.append([
            InlineKeyboardButton(
                f"🔥 {code} — {name}",
                callback_data=f"movie_{movie_id}",
            )
        ])

    await update.message.reply_text(
        "🔥 <b>Mashhur kinolar</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ============================================================
# CATALOG
# ============================================================

async def catalog(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    movies = await get_latest_movies(20)

    if not movies:
        await update.message.reply_text(
            "❌ Katalog bo'sh."
        )
        return

    buttons = []

    for movie in movies:

        movie_id = movie[0]
        code = movie[1]
        name = movie[2]

        buttons.append([
            InlineKeyboardButton(
                f"🎬 {code} — {name}",
                callback_data=f"movie_{movie_id}",
            )
        ])

    await update.message.reply_text(
        "🎬 <b>Katalog</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ============================================================
# ADMIN ADD MOVIE
# ============================================================

MOVIE_CODE = 1
MOVIE_FILE = 2
MOVIE_NAME = 3
MOVIE_DESCRIPTION = 4
MOVIE_CATEGORY = 5


async def add_movie_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        return ConversationHandler.END

    await query.message.reply_text(
        "➕ Kino qo'shish\n\n"
        "1️⃣ Kino kodini yuboring:"
    )

    return MOVIE_CODE


async def add_movie_code(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    context.user_data["movie_code"] = update.message.text.strip()

    await update.message.reply_text(
        "2️⃣ Endi kino videosini yuboring."
    )

    return MOVIE_FILE


async def add_movie_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message.video:
        await update.message.reply_text(
            "❌ Iltimos, video yuboring."
        )
        return MOVIE_FILE

    context.user_data["movie_file_id"] = (
        update.message.video.file_id
    )

    await update.message.reply_text(
        "3️⃣ Kino nomini yuboring:"
    )

    return MOVIE_NAME


async def add_movie_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    context.user_data["movie_name"] = update.message.text.strip()

    await update.message.reply_text(
        "4️⃣ Kino tavsifini yuboring:"
    )

    return MOVIE_DESCRIPTION


async def add_movie_description(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    context.user_data["movie_description"] = (
        update.message.text.strip()
    )

    await update.message.reply_text(
        "5️⃣ Kino kategoriyasini yuboring:"
    )

    return MOVIE_CATEGORY


async def add_movie_category(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    category = update.message.text.strip()

    code = context.user_data.get("movie_code")
    file_id = context.user_data.get("movie_file_id")
    name = context.user_data.get("movie_name")
    description = context.user_data.get("movie_description")

    try:

        await add_movie_to_db(
            code=code,
            name=name,
            description=description,
            category=category,
            file_id=file_id,
        )

        await update.message.reply_text(
            "✅ Kino muvaffaqiyatli qo'shildi!"
        )

    except sqlite3.IntegrityError:

        await update.message.reply_text(
            "❌ Bu kino kodi allaqachon mavjud."
        )

    finally:

        for key in [
            "movie_code",
            "movie_file_id",
            "movie_name",
            "movie_description",
        ]:
            context.user_data.pop(key, None)

    return ConversationHandler.END


async def cancel_movie(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    context.user_data.clear()

    await update.message.reply_text(
        "❌ Amal bekor qilindi."
    )

    return ConversationHandler.END


# ============================================================
# ADMIN DELETE
# ============================================================

async def admin_delete_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        return

    context.user_data["delete_movie_mode"] = True

    await query.message.reply_text(
        "🗑 O'chiriladigan kino kodini yuboring."
    )


async def delete_movie_process(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await is_admin(update.effective_user.id):
        return False

    if not context.user_data.get("delete_movie_mode"):
        return False

    code = update.message.text.strip()

    deleted = await delete_movie_by_code(code)

    context.user_data.pop(
        "delete_movie_mode",
        None,
    )

    if deleted:
        await update.message.reply_text(
            f"✅ <code>{code}</code> kodi bilan kino o'chirildi.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "❌ Bunday kodli kino topilmadi."
        )

    return True


# ============================================================
# ADMIN STATS
# ============================================================

async def admin_stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        return

    async with aiosqlite.connect(DB_NAME) as db:

        cursor = await db.execute(
            "SELECT COUNT(*) FROM users"
        )
        users_count = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM movies"
        )
        movies_count = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM ratings"
        )
        ratings_count = (await cursor.fetchone())[0]

    await query.message.reply_text(
        "📊 <b>Statistika</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{users_count}</b>\n"
        f"🎬 Kinolar: <b>{movies_count}</b>\n"
        f"⭐ Reytinglar: <b>{ratings_count}</b>",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# ADMIN USERS
# ============================================================

async def admin_users(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        return

    async with aiosqlite.connect(DB_NAME) as db:

        cursor = await db.execute("""
            SELECT
                id,
                username,
                full_name,
                status
            FROM users
            ORDER BY id DESC
            LIMIT 50
        """)

        users = await cursor.fetchall()

    if not users:
        await query.message.reply_text(
            "❌ Foydalanuvchilar yo'q."
        )
        return

    lines = ["👥 <b>Foydalanuvchilar</b>\n"]

    for user in users:

        user_id = user[0]
        username = user[1] or "-"
        full_name = user[2] or "-"
        status = user[3] or "USER"

        lines.append(
            f"🆔 <code>{user_id}</code>\n"
            f"👤 {full_name}\n"
            f"🔗 @{username}\n"
            f"⭐ {status}\n"
        )

    await query.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# BROADCAST
# ============================================================

async def admin_broadcast_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        return

    context.user_data["broadcast_mode"] = True

    await query.message.reply_text(
        "📢 Reklama yuborish rejimi.\n\n"
        "Endi yubormoqchi bo'lgan xabarni yuboring."
    )


async def broadcast_process(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not await is_admin(update.effective_user.id):
        return False

    if not context.user_data.get("broadcast_mode"):
        return False

    context.user_data.pop(
        "broadcast_mode",
        None,
    )

    async with aiosqlite.connect(DB_NAME) as db:

        cursor = await db.execute(
            "SELECT id FROM users"
        )

        users = await cursor.fetchall()

    success = 0
    failed = 0

    for (user_id,) in users:

        try:

            await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )

            success += 1

        except Exception as error:

            failed += 1

            logger.warning(
                "Broadcast failed for %s: %s",
                user_id,
                error,
            )

    await update.message.reply_text(
        "📢 <b>Broadcast tugadi.</b>\n\n"
        f"✅ Yuborildi: {success}\n"
        f"❌ Yuborilmadi: {failed}",
        parse_mode=ParseMode.HTML,
    )

    return True


# ============================================================
# RATING CALLBACK
# ============================================================

async def rating_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")

    if len(parts) != 3:
        return

    movie_id = int(parts[1])
    rating = int(parts[2])

    await set_rating(
        query.from_user.id,
        movie_id,
        rating,
    )

    await query.answer(
        f"⭐ {rating}/5 baho saqlandi!",
        show_alert=True,
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    data = query.data

    if data.startswith("movie_"):

        try:
            movie_id = int(
                data.replace("movie_", "")
            )
        except ValueError:
            return

        await query.answer()

        await show_movie(
            update,
            context,
            movie_id,
        )

        return

    if data.startswith("rate_"):
        await rating_callback(
            update,
            context,
        )
        return

    if data == "admin_add_movie":
        await add_movie_start(
            update,
            context,
        )
        return

    if data == "admin_delete":
        await admin_delete_start(
            update,
            context,
        )
        return

    if data == "admin_stats":
        await admin_stats(
            update,
            context,
        )
        return

    if data == "admin_users":
        await admin_users(
            update,
            context,
        )
        return

    if data == "admin_status":
        await admin_status_start(
            update,
            context,
        )
        return

    if data == "admin_status_remove":
        await admin_status_remove_start(
            update,
            context,
        )
        return

    if data == "admin_broadcast":
        await admin_broadcast_start(
            update,
            context,
        )
        return


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    await register_user(user)

    text = update.message.text.strip()

    # --------------------------------------------------------
    # ADMIN STATUS REMOVE MODE
    # --------------------------------------------------------

    if await is_admin(user.id):

        if context.user_data.get("status_mode") == "remove":

            handled = await process_remove_status_command(
                update,
                context,
            )

            if handled:
                return

        # ----------------------------------------------------
        # ADMIN STATUS SET
        # ----------------------------------------------------

        if context.user_data.get("status_mode") == "set":

            handled = await process_status_command(
                update,
                context,
            )

            if handled:
                return

        # ----------------------------------------------------
        # DELETE MODE
        # ----------------------------------------------------

        handled = await delete_movie_process(
            update,
            context,
        )

        if handled:
            return

        # ----------------------------------------------------
        # BROADCAST MODE
        # ----------------------------------------------------

        handled = await broadcast_process(
            update,
            context,
        )

        if handled:
            return

        # ----------------------------------------------------
        # DIRECT STATUS COMMAND
        #
        # 123456789 ADMIN
        # 123456789 VIP
        # 123456789 PREMIUM
        # 123456789 USER
        # 123456789 REMOVE
        # ----------------------------------------------------

        parts = text.split()

        if len(parts) == 2 and parts[0].isdigit():

            handled = await process_status_command(
                update,
                context,
            )

            if handled:
                return

    # --------------------------------------------------------
    # SEARCH MODE
    # --------------------------------------------------------

    if context.user_data.get("search_mode"):

        await perform_search(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # MAIN BUTTONS
    # --------------------------------------------------------

    if text == "🔎 Qidirish":

        await search_command(
            update,
            context,
        )

        return

    if text == "🎬 Katalog":

        await catalog(
            update,
            context,
        )

        return

    if text == "🔥 Mashhur":

        await popular_movies(
            update,
            context,
        )

        return

    if text == "🆕 Yangi":

        await latest_movies(
            update,
            context,
        )

        return

    if text == "👤 Profil":

        await show_profile(
            update,
            context,
        )

        return

    if text == "👑 Admin panel":

        await admin_command(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # DIRECT MOVIE CODE SEARCH
    # --------------------------------------------------------

    movie = await get_movie_by_code(text)

    if movie:

        await show_movie(
            update,
            context,
            movie[0],
        )

        return

    # --------------------------------------------------------
    # NORMAL SEARCH
    # --------------------------------------------------------

    movies = await search_movies(text)

    if movies:

        buttons = []

        for movie in movies:

            movie_id = movie[0]
            code = movie[1]
            name = movie[2]

            buttons.append([
                InlineKeyboardButton(
                    f"🎬 {code} — {name}",
                    callback_data=f"movie_{movie_id}",
                )
            ])

        await update.message.reply_text(
            "🔎 Natijalar:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

        return

    await update.message.reply_text(
        "❓ Buyruqni tushunmadim.\n\n"
        "🎬 Kino kodini yoki nomini yuboring."
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.error(
        "Exception while handling update:",
        exc_info=context.error,
    )


# ============================================================
# AIOHTTP HEALTH SERVER
# ============================================================

async def health_handler(request):

    return web.json_response({
        "status": "ok",
        "bot": "running",
    })


async def root_handler(request):

    return web.Response(
        text="🎬 Kino Bot is running!"
    )


async def start_web_server():

    web_app = web.Application()

    web_app.router.add_get(
        "/",
        root_handler,
    )

    web_app.router.add_get(
        "/health",
        health_handler,
    )

    runner = web.AppRunner(web_app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT,
    )

    await site.start()

    logger.info(
        "🌐 Web server started on port %s",
        PORT,
    )

    return runner


# ============================================================
# BUILD TELEGRAM APPLICATION
# ============================================================

def build_application():

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # --------------------------------------------------------
    # ADMIN COMMAND
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command,
        )
    )

    # --------------------------------------------------------
    # ADD MOVIE CONVERSATION
    # --------------------------------------------------------

    movie_conversation = ConversationHandler(

        entry_points=[
            CallbackQueryHandler(
                add_movie_start,
                pattern="^admin_add_movie$",
            )
        ],

        states={

            MOVIE_CODE: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    add_movie_code,
                )
            ],

            MOVIE_FILE: [
                MessageHandler(
                    filters.VIDEO,
                    add_movie_file,
                )
            ],

            MOVIE_NAME: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    add_movie_name,
                )
            ],

            MOVIE_DESCRIPTION: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    add_movie_description,
                )
            ],

            MOVIE_CATEGORY: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    add_movie_category,
                )
            ],
        },

        fallbacks=[
            CommandHandler(
                "cancel",
                cancel_movie,
            )
        ],

        per_message=False,
    )

    application.add_handler(
        movie_conversation
    )

    # --------------------------------------------------------
    # CALLBACKS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    # --------------------------------------------------------
    # ERRORS
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    return application


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info("🚀 Starting Kino Bot...")

    # Database
    await init_db()

    # Telegram application
    application = build_application()

    # HTTP server
    web_runner = await start_web_server()

    try:

        # Telegram initialize
        await application.initialize()

        # Telegram start
        await application.start()

        # Polling
        if application.updater is None:
            raise RuntimeError(
                "Telegram Updater mavjud emas."
            )

        await application.updater.start_polling(
            drop_pending_updates=True
        )

        logger.info(
            "🤖 Telegram polling started"
        )

        logger.info(
            "🌐 Health URL: /health"
        )

        logger.info(
            "✅ Bot + aiohttp server bir vaqtda ishlayapti"
        )

        # Processni tirik ushlab turadi
        await asyncio.Event().wait()

    except asyncio.CancelledError:

        logger.info(
            "Application cancelled."
        )

    finally:

        logger.info(
            "🛑 Shutting down..."
        )

        try:

            if application.updater:

                await application.updater.stop()

        except Exception:

            logger.exception(
                "Error stopping updater"
            )

        try:

            await application.stop()

        except Exception:

            logger.exception(
                "Error stopping application"
            )

        try:

            await application.shutdown()

        except Exception:

            logger.exception(
                "Error shutting down application"
            )

        try:

            await web_runner.cleanup()

        except Exception:

            logger.exception(
                "Error cleaning up web server"
            )

        logger.info(
            "👋 Shutdown complete."
        )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped manually."
        )