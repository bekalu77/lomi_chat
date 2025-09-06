import os
import logging
from typing import Dict, List, Optional, Any
from enum import Enum
from datetime import datetime

import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

import asyncpg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# --------------------
# Bootstrap & settings
# --------------------
load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("lomitalk")

# Telegram env
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set. Add it in your environment.")

WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")  # optional but recommended
BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Add your Neon connection string as DATABASE_URL.")

# Admin username and ID from environment
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_ID = os.getenv("ADMIN_ID")

# DB pool: will be created at startup
db_pool: Optional[asyncpg.pool.Pool] = None

# --------------------
# Utilities & helpers
# --------------------
def safe_int(value: Any) -> Optional[int]:
    """Convert to int if possible, else return None."""
    if value is None:
        return None
    s = str(value).strip()
    if s == "" or s.lower() == "none":
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None

def bool_from_db(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if val in (1, "1", "t", "true", "True"):
        return True
    return False

def format_balance(points: int) -> str:
    lemons = points / 1000
    return f"{points} points ({lemons:.2f} 🍋 Lemons)"

# --------------------
# DB operations (asyncpg)
# --------------------
CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    points INTEGER DEFAULT 1000,
    profile_complete BOOLEAN DEFAULT FALSE,
    in_pool BOOLEAN DEFAULT FALSE,
    in_conversation BOOLEAN DEFAULT FALSE,
    conversation_partner TEXT,
    is_initiator BOOLEAN DEFAULT FALSE,
    joined_date TEXT,
    username TEXT,
    gender TEXT,
    age_group TEXT,
    nickname TEXT,
    preferred_age_group TEXT
);
"""

async def init_db_pool():
    global db_pool
    logger.info("Creating DB pool to %s", DATABASE_URL)
    db_pool = await asyncpg.create_pool(DATABASE_URL, max_size=10)
    async with db_pool.acquire() as conn:
        await conn.execute(CREATE_USERS_SQL)
    logger.info("DB pool initialized and schema ensured.")

async def close_db_pool():
    global db_pool
    if db_pool:
        await db_pool.close()
        db_pool = None
        logger.info("DB pool closed.")

async def get_user_data_async(user_id: str) -> Dict:
    if not db_pool:
        return {}
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", str(user_id))
        if not row:
            return {}
        d = dict(row)
        d['profile_complete'] = bool_from_db(d.get('profile_complete'))
        d['in_pool'] = bool_from_db(d.get('in_pool'))
        d['in_conversation'] = bool_from_db(d.get('in_conversation'))
        d['is_initiator'] = bool_from_db(d.get('is_initiator'))
        cp = d.get('conversation_partner')
        d['conversation_partner'] = str(cp) if cp is not None else None
        d['points'] = int(d.get('points') or 0)
        return d

async def update_user_data_async(user_id: str, updates: Dict) -> bool:
    if not db_pool:
        return False

    current = await get_user_data_async(user_id)
    merged = {**current, **updates}
    points = int(merged.get('points', 1000))
    profile_complete = bool_from_db(merged.get('profile_complete', False))
    in_pool = bool_from_db(merged.get('in_pool', False))
    in_conversation = bool_from_db(merged.get('in_conversation', False))
    conversation_partner = merged.get('conversation_partner')
    conversation_partner = str(conversation_partner) if conversation_partner else None
    is_initiator = bool_from_db(merged.get('is_initiator', False))
    joined_date = merged.get('joined_date') or datetime.now().isoformat()
    username = merged.get('username')
    gender = merged.get('gender')
    age_group = merged.get('age_group')
    nickname = merged.get('nickname')
    preferred_age_group = merged.get('preferred_age_group')

    sql = """
    INSERT INTO users (
        user_id, points, profile_complete, in_pool, in_conversation,
        conversation_partner, is_initiator, joined_date, username,
        gender, age_group, nickname, preferred_age_group
    ) VALUES (
        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13
    )
    ON CONFLICT (user_id) DO UPDATE SET
        points = EXCLUDED.points,
        profile_complete = EXCLUDED.profile_complete,
        in_pool = EXCLUDED.in_pool,
        in_conversation = EXCLUDED.in_conversation,
        conversation_partner = EXCLUDED.conversation_partner,
        is_initiator = EXCLUDED.is_initiator,
        joined_date = EXCLUDED.joined_date,
        username = EXCLUDED.username,
        gender = EXCLUDED.gender,
        age_group = EXCLUDED.age_group,
        nickname = EXCLUDED.nickname,
        preferred_age_group = EXCLUDED.preferred_age_group;
    """
    async with db_pool.acquire() as conn:
        try:
            await conn.execute(sql,
                               str(user_id), points, profile_complete, in_pool,
                               in_conversation, conversation_partner, is_initiator,
                               joined_date, username, gender, age_group, nickname,
                               preferred_age_group)
            return True
        except Exception as e:
            logger.exception("Failed to upsert user %s", user_id)
            return False

async def get_all_users_async() -> List[Dict]:
    if not db_pool:
        return []
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users")
        users = []
        for row in rows:
            d = dict(row)
            d['profile_complete'] = bool_from_db(d.get('profile_complete'))
            d['in_pool'] = bool_from_db(d.get('in_pool'))
            d['in_conversation'] = bool_from_db(d.get('in_conversation'))
            d['is_initiator'] = bool_from_db(d.get('is_initiator'))
            cp = d.get('conversation_partner')
            d['conversation_partner'] = str(cp) if cp is not None else None
            d['points'] = int(d.get('points') or 0)
            users.append(d)
        return users

# --------------------
# Domain logic
# --------------------
class AgeGroup(Enum):
    AGE_18_25 = "18-25"
    AGE_26_35 = "26-35"
    AGE_35_PLUS = ">35"

class Gender(Enum):
    MALE = "Male"
    FEMALE = "Female"

# --------------------
# Handlers
# --------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)

# ✅ (Keep the rest of your handlers as they were — start, help, profile, points, join, leave, find, setbalance, etc.)
# I will keep all logic same, just ensured error_handler is defined before usage.

# --------------------
# Bot Application
# --------------------
application = Application.builder().token(TOKEN).build()
application.add_error_handler(error_handler)

# Register commands
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CommandHandler("profile", profile))
application.add_handler(CommandHandler("points", points))
application.add_handler(CommandHandler("join", join_pool))
application.add_handler(CommandHandler("leave", leave_pool))
application.add_handler(CommandHandler("find", find_partner_cmd))
application.add_handler(CommandHandler("end", end_conversation))
application.add_handler(CommandHandler("report", report_user))
application.add_handler(CommandHandler("transact", transact))
application.add_handler(CommandHandler("setbalance", set_balance))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
application.add_handler(MessageHandler(filters.PHOTO, handle_message))
application.add_handler(MessageHandler(filters.VIDEO, handle_message))

# --------------------
# FastAPI Integration
# --------------------
app = FastAPI()
WEBHOOK_PATH = f"/{TOKEN}"

@app.on_event("startup")
async def on_startup():
    await init_db_pool()
    await application.initialize()
    await application.start()
    if BASE_URL:
        webhook_url = f"{BASE_URL}{WEBHOOK_PATH}"
        try:
            if WEBHOOK_SECRET:
                await application.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET)
            else:
                await application.bot.set_webhook(url=webhook_url)
            logger.info("Webhook set to %s", webhook_url)
        except Exception:
            logger.exception("Failed to set webhook to %s", webhook_url)
    else:
        logger.warning("BASE_URL empty; webhook not set.")

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await application.stop()
        await application.shutdown()
    finally:
        await close_db_pool()

@app.get("/")
async def home():
    return PlainTextResponse("LomiTalk Bot is running!")

@app.get("/health")
async def health():
    return PlainTextResponse("OK")

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret token")
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
    return PlainTextResponse("OK")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
