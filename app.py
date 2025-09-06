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

# Telegram env
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set. Add it in your environment.")

WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")  # optional but recommended
BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Add your Neon connection string as DATABASE_URL.")

# Admin username (string without @)
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
    """Fetch a single user row and return safe python types."""
    if not db_pool:
        logger.warning("DB pool not ready when fetching user %s", user_id)
        return {}
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", str(user_id))
        if not row:
            return {}
        d = dict(row)
        # Normalize types
        d['profile_complete'] = bool_from_db(d.get('profile_complete'))
        d['in_pool'] = bool_from_db(d.get('in_pool'))
        d['in_conversation'] = bool_from_db(d.get('in_conversation'))
        d['is_initiator'] = bool_from_db(d.get('is_initiator'))
        # keep conversation_partner as string or None
        cp = d.get('conversation_partner')
        d['conversation_partner'] = str(cp) if cp is not None else None
        # Keep points as int
        d['points'] = int(d.get('points') or 0)
        return d

async def update_user_data_async(user_id: str, updates: Dict) -> bool:
    """
    Insert or update user row. updates is a dict of fields to set.
    We'll load existing values, merge, and upsert.
    """
    if not db_pool:
        logger.warning("DB pool not ready when updating user %s", user_id)
        return False

    # Read current data
    current = await get_user_data_async(user_id)
    # Merge
    merged = {**current, **updates}
    # Prepare final values
    points = int(merged.get('points', 1000))
    profile_complete = bool_from_db(merged.get('profile_complete', False))
    in_pool = bool_from_db(merged.get('in_pool', False))
    in_conversation = bool_from_db(merged.get('in_conversation', False))
    conversation_partner = merged.get('conversation_partner', None)
    # convert conversation partner to string if present, else None
    if conversation_partner is None:
        conversation_partner = None
    else:
        conversation_partner = str(conversation_partner)
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
                               str(user_id),
                               points,
                               profile_complete,
                               in_pool,
                               in_conversation,
                               conversation_partner,
                               is_initiator,
                               joined_date,
                               username,
                               gender,
                               age_group,
                               nickname,
                               preferred_age_group
                               )
            return True
        except Exception:
            logger.exception("Failed to upsert user %s", user_id)
            return False

async def get_all_users_async() -> List[Dict]:
    if not db_pool:
        logger.warning("DB pool not ready when fetching all users")
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
# Domain logic (async)
# --------------------
class AgeGroup(Enum):
    AGE_18_25 = "18-25"
    AGE_26_35 = "26-35"
    AGE_35_PLUS = ">35"

class Gender(Enum):
    MALE = "Male"
    FEMALE = "Female"

async def find_partner_async(user_id: str, target_age_group: Optional[str] = None) -> Optional[str]:
    """
    Find a suitable partner for user_id. Matching rules:
    - partner must be in_pool and not in_conversation
    - opposite gender only (dating focus)
    - if target_age_group provided, match that age group (else any)
    Returns partner user_id string (or None).
    """
    current_user = await get_user_data_async(user_id)
    if not current_user:
        return None
    if not current_user.get('in_pool'):
        return None

    # Load all users
    all_users = await get_all_users_async()

    my_gender = current_user.get('gender')
    # Determine opposite gender
    if my_gender == Gender.MALE.value:
        desired_gender = Gender.FEMALE.value
    elif my_gender == Gender.FEMALE.value:
        desired_gender = Gender.MALE.value
    else:
        # if gender not set, do not match
        return None

    for u in all_users:
        uid = str(u.get('user_id'))
        if uid == str(user_id):
            continue
        if not u.get('in_pool'):
            continue
        if u.get('in_conversation'):
            continue
        if not u.get('profile_complete'):
            continue
        if u.get('gender') != desired_gender:
            continue
        if target_age_group and u.get('age_group') != target_age_group:
            continue
        # found
        return uid
    return None

# --------------------
# Bot handlers (async)
# --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data:
        # create new user with 1000 points default
        await update_user_data_async(user_id, {
            'points': 1000,
            'profile_complete': False,
            'in_pool': False,
            'in_conversation': False,
            'conversation_partner': None,
            'joined_date': datetime.now().isoformat(),
            'username': update.effective_user.username
        })
        await update.message.reply_text(
            "Welcome! You received 1000 points (1.00 🍋 Lemon).\nUse /profile to set up your short dating profile."
        )
    else:
        # update username if changed
        current_username = update.effective_user.username
        if user_data.get('username') != current_username:
            await update_user_data_async(user_id, {'username': current_username})
        points = user_data.get('points', 0)
        await update.message.reply_text(
            f"Welcome back.\nBalance: {format_balance(points)}\nUse /help for commands."
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "Commands (short):\n"
        "/start - register\n"
        "/profile - set/edit profile\n"
        "/points - show balance\n"
        "/join - join earning pool\n"
        "/leave - leave pool\n"
        "/find - find a date (bot searches opposite sex)\n"
        "/end - end conversation\n"
        "/transact - show transaction info\n"
        "/report - report partner\n"
    )
    await update.message.reply_text(help_text)

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data:
        await update.message.reply_text("Use /start first.")
        return

    if context.user_data.get('setting_up_profile'):
        await update.message.reply_text("Complete current setup.")
        return

    if user_data.get('profile_complete'):
        profile_text = (
            f"Profile:\n"
            f"Nickname: {user_data.get('nickname','Not set')}\n"
            f"Gender: {user_data.get('gender','Not set')}\n"
            f"Age: {user_data.get('age_group','Not set')}\n"
        )
        keyboard = [[InlineKeyboardButton("Edit", callback_data="edit_profile")]]
        await update.message.reply_text(profile_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # Start profile setup
    context.user_data['setting_up_profile'] = True
    context.user_data['profile_setup_step'] = 'gender'
    keyboard = [
        [InlineKeyboardButton("♂️ Male", callback_data="gender_MALE")],
        [InlineKeyboardButton("♀️ Female", callback_data="gender_FEMALE")],
        [InlineKeyboardButton("Skip", callback_data="gender_skip")],
    ]
    await update.message.reply_text("Set your gender:", reply_markup=InlineKeyboardMarkup(keyboard))

async def points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data:
        await update.message.reply_text("Use /start first.")
        return
    points = user_data.get('points', 0)
    await update.message.reply_text(f"Balance: {format_balance(points)}")

async def join_pool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data:
        await update.message.reply_text("Use /start first.")
        return
    if not user_data.get('profile_complete'):
        await update.message.reply_text("Complete profile first with /profile.")
        return
    if user_data.get('in_pool'):
        await update.message.reply_text("Already in pool.")
        return
    await update_user_data_async(user_id, {'in_pool': True})
    await update.message.reply_text("Joined pool. You'll be visible to matches.")

async def leave_pool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data:
        await update.message.reply_text("Use /start first.")
        return
    if not user_data.get('in_pool'):
        await update.message.reply_text("You are not in pool.")
        return
    await update_user_data_async(user_id, {'in_pool': False})
    await update.message.reply_text("Left pool.")

async def find_partner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data:
        await update.message.reply_text("Use /start first.")
        return
    if not user_data.get('profile_complete'):
        await update.message.reply_text("Complete profile with /profile.")
        return
    if user_data.get('in_conversation'):
        await update.message.reply_text("You're already in a conversation. Use /end first.")
        return
    if not user_data.get('in_pool'):
        await update.message.reply_text("Join the pool with /join to be found.")
        return

    # Ask for age preference only
    keyboard = [
        [InlineKeyboardButton(AgeGroup.AGE_18_25.value, callback_data="find_age_18_25")],
        [InlineKeyboardButton(AgeGroup.AGE_26_35.value, callback_data="find_age_26_35")],
        [InlineKeyboardButton(AgeGroup.AGE_35_PLUS.value, callback_data="find_age_35_plus")],
        [InlineKeyboardButton("Any age", callback_data="find_age_any")],
    ]
    # Brief message
    await update.message.reply_text("Searching for the opposite sex. Choose age group (or Any):", reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data['finding_partner'] = True

async def handle_find_age_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, age_pref_key: str):
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)

    # Map callback to age group string in DB (or None for any)
    age_map = {
        "18_25": AgeGroup.AGE_18_25.value,
        "26_35": AgeGroup.AGE_26_35.value,
        "35_plus": AgeGroup.AGE_35_PLUS.value,
        "any": None
    }
    target_age = age_map.get(age_pref_key.replace("find_age_", ""), None)

    # Notify user briefly that bot is searching
    await query.edit_message_text("🔎 Looking for matches — this may take a moment...")

    partner_id = await find_partner_async(user_id, target_age_group=target_age)
    if not partner_id:
        await query.edit_message_text("No matches found right now. Try again later.")
        context.user_data.pop('finding_partner', None)
        return

    # Mark both users as in conversation and record partner
    await update_user_data_async(user_id, {'in_conversation': True, 'conversation_partner': partner_id, 'is_initiator': True})
    await update_user_data_async(partner_id, {'in_conversation': True, 'conversation_partner': user_id, 'is_initiator': False})

    user_data = await get_user_data_async(user_id)
    partner_data = await get_user_data_async(partner_id)

    # Send concise notifications
    await context.bot.send_message(chat_id=user_id, text=f"Matched with {partner_data.get('nickname') or partner_data.get('username') or partner_id}. You pay 1 point/char.")
    await context.bot.send_message(chat_id=partner_id, text=f"Matched with {user_data.get('nickname') or user_data.get('username') or user_id}. You earn 1 point/char.")

    context.user_data.pop('finding_partner', None)

async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data or not user_data.get("in_conversation"):
        await update.message.reply_text("Not in a conversation.")
        return
    partner_id = user_data.get("conversation_partner")

    await update_user_data_async(user_id, {"in_conversation": False, "conversation_partner": None, "is_initiator": False})
    if partner_id:
        await update_user_data_async(str(partner_id), {"in_conversation": False, "conversation_partner": None, "is_initiator": False})

        # Show balances to both users
        updated_user = await get_user_data_async(user_id)
        updated_partner = await get_user_data_async(str(partner_id))

        await context.bot.send_message(chat_id=user_id, text=f"Conversation ended.\nYour balance: {format_balance(updated_user.get('points',0))}")
        await context.bot.send_message(chat_id=partner_id, text=f"Your partner ended the conversation.\nYour balance: {format_balance(updated_partner.get('points',0))}")
    else:
        await update.message.reply_text("Conversation ended.")


async def report_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data or not user_data.get('in_conversation'):
        await update.message.reply_text("You need to be in a conversation to report.")
        return
    partner_id = user_data.get('conversation_partner')
    if not partner_id:
        await update.message.reply_text("No partner found.")
        return
    # End conversation for both
    await update_user_data_async(user_id, {'in_conversation': False, 'conversation_partner': None, 'is_initiator': False})
    await update_user_data_async(str(partner_id), {'in_conversation': False, 'conversation_partner': None, 'is_initiator': False})
    await update.message.reply_text("Report received. Conversation ended.")
    await context.bot.send_message(chat_id=partner_id, text="Conversation ended due to a report.")

async def transact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data:
        await update.message.reply_text("Use /start first.")
        return
    points = user_data.get('points', 0)
    text = (
        f"Transactions:\nBalance: {format_balance(points)}\n"
        "To change balances, contact the admin @busi_admin or use /setbalance (admin only)."
    )
    await update.message.reply_text(text)

# Admin-only: setbalance
async def set_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")  # without @
    ADMIN_ID = os.getenv("ADMIN_ID")

    user_id = str(update.effective_user.id)
    username = update.effective_user.username or ""

    # Check admin
    if str(user_id) != str(ADMIN_ID) and username.lower() != (ADMIN_USERNAME or "").lower():
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /setbalance <username> <add|subtract|reset> <amount>\n"
            "Example: /setbalance johndoe add 1000"
        )
        return

    target_username = context.args[0].lstrip("@").lower()
    action = context.args[1].lower()
    amount = int(context.args[2]) if action != "reset" else 0

    # Fetch all users and find by username
    users = await get_all_users_async()
    target_user = next((u for u in users if (u.get("username") or "").lower() == target_username), None)

    if not target_user:
        await update.message.reply_text(f"❌ User @{target_username} not found.")
        return

    current_points = target_user.get("points", 0)

    if action == "add":
        new_points = current_points + amount
    elif action == "subtract":
        new_points = max(0, current_points - amount)
    elif action == "reset":
        new_points = 0
    else:
        await update.message.reply_text("❌ Invalid action. Use add, subtract, or reset.")
        return

    await update_user_data_async(target_user["user_id"], {"points": new_points})
    await update.message.reply_text(
        f"✅ Balance updated for @{target_username}: {format_balance(new_points)}"
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)

    if query.data == "edit_profile":
        context.user_data['setting_up_profile'] = True
        context.user_data['profile_setup_step'] = 'gender'
        keyboard = [
            [InlineKeyboardButton("♂️ Male", callback_data="gender_MALE")],
            [InlineKeyboardButton("♀️ Female", callback_data="gender_FEMALE")],
            [InlineKeyboardButton("Skip", callback_data="gender_skip")],
        ]
        await query.edit_message_text("Update gender:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if query.data.startswith("gender_"):
        if query.data != "gender_skip":
            gender_name = query.data.replace("gender_", "")
            gender = Gender[gender_name].value
            await update_user_data_async(user_id, {'gender': gender})
        context.user_data['profile_setup_step'] = 'nickname'
        await query.edit_message_text("Send your nickname now (short):")
        return

    if query.data.startswith("age_"):
        if query.data != "age_skip":
            age_group_name = query.data.replace("age_", "")
            # map to AgeGroup values
            mapping = {
                "AGE_18_25": AgeGroup.AGE_18_25.value,
                "AGE_26_35": AgeGroup.AGE_26_35.value,
                "AGE_35_PLUS": AgeGroup.AGE_35_PLUS.value
            }
            age_group = mapping.get(age_group_name, None)
            if age_group:
                await update_user_data_async(user_id, {'age_group': age_group})
        await update_user_data_async(user_id, {'profile_complete': True})
        context.user_data['setting_up_profile'] = False
        await query.edit_message_text("Profile complete. Use /join to be visible to matches.")
        return

    if query.data.startswith("find_age_"):
        # callback from find flow
        await handle_find_age_callback(update, context, query.data)
        return

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)

    if context.user_data.get("setting_up_profile"):
        # Profile setup logic (unchanged)
        step = context.user_data.get("profile_setup_step")
        if step == "nickname":
            nickname = update.message.text.strip()[:32]
            await update_user_data_async(user_id, {"nickname": nickname})
            context.user_data["profile_setup_step"] = "age"
            keyboard_rows = [
                [InlineKeyboardButton(AgeGroup.AGE_18_25.value, callback_data="age_AGE_18_25")],
                [InlineKeyboardButton(AgeGroup.AGE_26_35.value, callback_data="age_AGE_26_35")],
                [InlineKeyboardButton(AgeGroup.AGE_35_PLUS.value, callback_data="age_AGE_35_PLUS")],
                [InlineKeyboardButton("Skip", callback_data="age_skip")],
            ]
            await update.message.reply_text("Select your age group:", reply_markup=InlineKeyboardMarkup(keyboard_rows))
        return

    if user_data.get("in_conversation"):
        partner_id = user_data.get("conversation_partner")
        if not partner_id:
            await update.message.reply_text("Error: partner missing.")
            return
        is_initiator = user_data.get("is_initiator", False)
        partner_data = await get_user_data_async(str(partner_id))

        # Calculate cost based on message type
        cost = 0
        if update.message.text:
            cost = len(update.message.text)
        elif update.message.photo:
            cost = 150
        elif update.message.video:
            cost = 250

        if is_initiator and cost > 0:
            user_points = user_data.get("points", 0)
            if user_points < cost:
                await update.message.reply_text(f"Not enough points. You need {cost} points.")
                return
            # Deduct and update partner
            await update_user_data_async(user_id, {"points": user_points - cost})
            await update_user_data_async(str(partner_id), {"points": partner_data.get("points", 0) + cost})

        # Forward message to partner
        if update.message.text:
            await context.bot.send_message(chat_id=partner_id, text=update.message.text)
        elif update.message.photo:
            photo_file = await update.message.photo[-1].get_file()
            await context.bot.send_photo(chat_id=partner_id, photo=photo_file.file_id)
        elif update.message.video:
            video_file = await update.message.video.get_file()
            await context.bot.send_video(chat_id=partner_id, video=video_file.file_id)
    else:
        await update.message.reply_text("Not in conversation. Use /find to search for matches.")

# --------------------
# Build Application and FastAPI
# --------------------
application = Application.builder().token(TOKEN).build()
# Register handlers
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
application.add_error_handler(error_handler)

# FastAPI app
app = FastAPI()
WEBHOOK_PATH = f"/{TOKEN}"

@app.on_event("startup")
async def on_startup():
    # Init DB pool
    await init_db_pool()
    # PTB initialization
    await application.initialize()
    await application.start()
    # Set webhook if we have a base url
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


