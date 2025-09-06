# app.py
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

TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set. Add it in your environment.")

WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Add your Neon connection string as DATABASE_URL.")

ADMIN_ID = os.getenv("ADMIN_ID")

db_pool: Optional[asyncpg.pool.Pool] = None

# --------------------
# Utilities
# --------------------
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
# DB operations
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
    db_pool = await asyncpg.create_pool(DATABASE_URL, max_size=10)
    async with db_pool.acquire() as conn:
        await conn.execute(CREATE_USERS_SQL)
    logger.info("DB pool initialized.")

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
        except Exception:
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

async def find_partner_async(user_id: str, target_age_group: Optional[str] = None) -> Optional[str]:
    current_user = await get_user_data_async(user_id)
    if not current_user or not current_user.get('in_pool'):
        return None
    all_users = await get_all_users_async()
    my_gender = current_user.get('gender')
    desired_gender = Gender.FEMALE.value if my_gender == Gender.MALE.value else Gender.MALE.value
    for u in all_users:
        uid = str(u.get('user_id'))
        if uid == user_id:
            continue
        if not u.get('in_pool') or u.get('in_conversation') or not u.get('profile_complete'):
            continue
        if u.get('gender') != desired_gender:
            continue
        if target_age_group and u.get('age_group') != target_age_group:
            continue
        return uid
    return None

# --------------------
# Bot Handlers
# --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data:
        await update_user_data_async(user_id, {
            'points': 1000,
            'profile_complete': False,
            'in_pool': False,
            'in_conversation': False,
            'conversation_partner': None,
            'joined_date': datetime.now().isoformat(),
            'username': update.effective_user.username
        })
        await update.message.reply_text("Welcome! You received 1000 points (1.00 🍋 Lemon). Use /profile to set up profile.")
    else:
        current_username = update.effective_user.username
        if user_data.get('username') != current_username:
            await update_user_data_async(user_id, {'username': current_username})
        points = user_data.get('points', 0)
        await update.message.reply_text(f"Welcome back.\nBalance: {format_balance(points)}\nUse /help for commands.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Commands:\n"
        "/start - register\n"
        "/profile - set/edit profile\n"
        "/points - show balance\n"
        "/join - join pool\n"
        "/leave - leave pool\n"
        "/find - find a date\n"
        "/end - end conversation\n"
        "/report - report partner\n"
        "/transact - transaction info\n"
        "/setbalance - admin only\n"
    )
    await update.message.reply_text(help_text)

# --------------------
# Conversation & message handling
# --------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data or not user_data.get('in_conversation'):
        await update.message.reply_text("You are not in a conversation. Use /find to match.")
        return

    partner_id = user_data.get('conversation_partner')
    if not partner_id:
        await update.message.reply_text("Error: partner not found.")
        return

    # Deduct 1 point for sender
    new_points_sender = max(0, user_data.get('points', 0) - 1)
    await update_user_data_async(user_id, {'points': new_points_sender})

    # Add 1 point to receiver
    partner_data = await get_user_data_async(partner_id)
    if partner_data:
        new_points_receiver = partner_data.get('points', 0) + 1
        await update_user_data_async(partner_id, {'points': new_points_receiver})

    # Forward message to partner
    await context.bot.forward_message(chat_id=int(partner_id),
                                      from_chat_id=int(user_id),
                                      message_id=update.message.message_id)

# --------------------
# End conversation
# --------------------
async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data or not user_data.get('in_conversation'):
        await update.message.reply_text("You are not in a conversation.")
        return

    partner_id = user_data.get('conversation_partner')
    await update_user_data_async(user_id, {
        'in_conversation': False,
        'conversation_partner': None,
        'is_initiator': False
    })
    if partner_id:
        await update_user_data_async(partner_id, {
            'in_conversation': False,
            'conversation_partner': None,
            'is_initiator': False
        })

        # Notify both users with updated balances
        sender_points = (await get_user_data_async(user_id)).get('points', 0)
        receiver_points = (await get_user_data_async(partner_id)).get('points', 0)
        await context.bot.send_message(chat_id=int(user_id),
                                       text=f"Conversation ended.\nYour balance: {format_balance(sender_points)}")
        await context.bot.send_message(chat_id=int(partner_id),
                                       text=f"Conversation ended.\nYour balance: {format_balance(receiver_points)}")
    else:
        await update.message.reply_text("Conversation ended.")

# --------------------
# FastAPI app for webhook
# --------------------
app = FastAPI()
telegram_app: Optional[Application] = None

@app.on_event("startup")
async def on_startup():
    global telegram_app
    await init_db_pool()
    telegram_app = Application.builder().token(TOKEN).build()
    # Register handlers
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("end", end_conversation))
    telegram_app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO, handle_message))
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    logger.info("Bot started.")

@app.on_event("shutdown")
async def on_shutdown():
    if telegram_app:
        await telegram_app.stop()
    await close_db_pool()
    logger.info("App shutdown completed.")

@app.post(f"/telegram/{WEBHOOK_SECRET}", response_class=PlainTextResponse)
async def telegram_webhook(request: Request):
    if not telegram_app:
        raise HTTPException(status_code=503, detail="Bot not ready")
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.update_queue.put(update)
    return "OK"

# --------------------
# Run locally for debug
# --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), log_level="info")
