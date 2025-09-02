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
    format="%((asctime)s - %(name)s - %(levelname)s - %(message)s)",
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
    preferred_gender TEXT,
    preferred_age_group TEXT
);
"""

async def init_db_pool():
    global db_pool
    logger.info("Creating DB pool to %s", DATABASE_URL)
    # asyncpg's create_pool will accept the full connection string
    db_pool = await asyncpg.create_pool(DATABASE_URL, max_size=10)
    # Ensure table exists
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
        d['conversation_partner'] = safe_int(d.get('conversation_partner'))
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
    if safe_int(conversation_partner) is None:
        conversation_partner = None
    else:
        conversation_partner = str(safe_int(conversation_partner))
    is_initiator = bool_from_db(merged.get('is_initiator', False))
    joined_date = merged.get('joined_date') or datetime.now().isoformat()
    username = merged.get('username')
    gender = merged.get('gender')
    age_group = merged.get('age_group')
    nickname = merged.get('nickname')
    preferred_gender = merged.get('preferred_gender')
    preferred_age_group = merged.get('preferred_age_group')

    sql = """
    INSERT INTO users (
        user_id, points, profile_complete, in_pool, in_conversation,
        conversation_partner, is_initiator, joined_date, username,
        gender, age_group, nickname, preferred_gender, preferred_age_group
    ) VALUES (
        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14
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
        preferred_gender = EXCLUDED.preferred_gender,
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
                               preferred_gender,
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
            d['conversation_partner'] = safe_int(d.get('conversation_partner'))
            d['points'] = int(d.get('points') or 0)
            users.append(d)
        return users

# --------------------
# Domain logic (async)
# --------------------
class AgeGroup(Enum):
    UNDER_18 = "Under 18"
    AGE_18_24 = "18-24"
    AGE_25_34 = "25-34"
    AGE_35_44 = "35-44"
    AGE_45_PLUS = "45+"

class Gender(Enum):
    MALE = "Male"
    FEMALE = "Female"

async def find_partner_async(user_id: str) -> Optional[str]:
    """
    Find a suitable partner for user_id, honoring pool/status and preferences.
    Returns partner user_id string (or None).
    """
    current_user = await get_user_data_async(user_id)
    if not current_user:
        return None
    if not current_user.get('in_pool'):
        return None

    # Load all users
    all_users = await get_all_users_async()

    pref_gender = current_user.get('preferred_gender')
    pref_age = current_user.get('preferred_age_group')
    # simple matching: iterate users and find first that satisfies constraints
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
        # gender filter
        if pref_gender and pref_gender != u.get('gender'):
            continue
        if pref_age and pref_age != u.get('age_group'):
            continue
        # match found
        return uid
    return None

# --------------------
# Bot handlers (async)
# --------------------
# NOTE: All handlers below now call async DB helpers.
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data:
        # create new user
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
            "👋 Welcome to LomiTalk!\n\n"
            "You've received 1000 free points to start with. Complete your profile to begin matching with partners.\n\n"
            "Use /profile to set up your profile."
        )
    else:
        # update username if changed
        current_username = update.effective_user.username
        if user_data.get('username') != current_username:
            await update_user_data_async(user_id, {'username': current_username})
        await update.message.reply_text(
            "Welcome back to LomiTalk!\n\n"
            f"Your current points: {user_data.get('points', 0)}\n"
            "Use /help to see all available commands."
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        """
🤖 LomiTalk Help Guide

💡 How it works:
- Complete your profile with gender, nickname, and age group
- Join the pool ✅ for FREE to earn points
- Use /find to choose preferences and find a partner 🔍
- Chat with your partner 💬
- The person who initiates the conversation PAYS 1 point per character sent
- The person selected from the pool EARNS 1 point per character received

💰 Payment System:
- Initiator: PAYS 1 point per character sent
- Partner: EARNS 1 point per character received
- Images: 150 points, Videos: 250 points
- No points created or destroyed - only transferred

⚡️ Commands:
/start - Register and get 1000 free points
/profile - Edit your profile
/points - Check your points balance
/join - Join the earning pool (FREE)
/leave - Leave the pool
/find - Find a partner with preferences (you will pay for messages)
/end - End current conversation
/transact - Deposit or withdraw points
/help - Show this help menu
/report - Report a user for inappropriate behavior

🎯 Tips:
- Complete your profile to get better matches
- Use specific preferences for better matches
- Stay in the pool to earn while you wait
- Be active to get found by others
- Longer conversations = more earnings!
"""
    )
    await update.message.reply_text(help_text)

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data:
        await update.message.reply_text("Please use /start first to initialize your account.")
        return

    if context.user_data.get('setting_up_profile'):
        await update.message.reply_text("Please complete your current profile setup first.")
        return

    if user_data.get('profile_complete'):
        profile_text = f"""
📋 Your Profile:
Nickname: {user_data.get('nickname', 'Not set')}
Gender: {user_data.get('gender', 'Not set')}
Age Group: {user_data.get('age_group', 'Not set')}
Preferred Gender: {user_data.get('preferred_gender', 'Any')}
Preferred Age Group: {user_data.get('preferred_age_group', 'Any')}
"""
        keyboard = [[InlineKeyboardButton("Edit Profile", callback_data="edit_profile")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(profile_text, reply_markup=reply_markup)
        return

    # Start profile setup
    context.user_data['setting_up_profile'] = True
    context.user_data['profile_setup_step'] = 'gender'
    keyboard = [
        [InlineKeyboardButton("♂️ Male", callback_data="gender_MALE")],
        [InlineKeyboardButton("♀️ Female", callback_data="gender_FEMALE")],
        [InlineKeyboardButton("Skip for now", callback_data="gender_skip")],
    ]
    await update.message.reply_text("Let's set up your profile!\n\nFirst, select your gender:", reply_markup=InlineKeyboardMarkup(keyboard))

async def points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data:
        await update.message.reply_text("Please use /start first to initialize your account.")
        return
    await update.message.reply_text(f"💰 Your current points balance: {user_data.get('points', 0)}")

async def join_pool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data:
        await update.message.reply_text("Please use /start first to initialize your account.")
        return
    if not user_data.get('profile_complete'):
        await update.message.reply_text("Please complete your profile with /profile before joining the pool.")
        return
    if user_data.get('in_pool'):
        await update.message.reply_text("You're already in the pool!")
        return
    await update_user_data_async(user_id, {'in_pool': True})
    await update.message.reply_text("✅ You've joined the pool! You can now be matched with other users.")

async def leave_pool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data:
        await update.message.reply_text("Please use /start first to initialize your account.")
        return
    if not user_data.get('in_pool'):
        await update.message.reply_text("You're not in the pool.")
        return
    await update_user_data_async(user_id, {'in_pool': False})
    await update.message.reply_text("You've left the pool. You will no longer be matched with partners.")

async def find_partner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data:
        await update.message.reply_text("Please use /start first to initialize your account.")
        return
    if not user_data.get('profile_complete'):
        await update.message.reply_text("Please complete your profile with /profile before finding a partner.")
        return
    if not user_data.get('in_pool'):
        await update.message.reply_text("You need to join the pool first with /join.")
        return
    if user_data.get('in_conversation'):
        await update.message.reply_text("You're already in a conversation! Use /end to end it first.")
        return

    keyboard = [
        [InlineKeyboardButton("♂️ Male", callback_data="pref_gender_MALE")],
        [InlineKeyboardButton("♀️ Female", callback_data="pref_gender_FEMALE")],
        [InlineKeyboardButton("Any gender", callback_data="pref_gender_ANY")],
    ]
    await update.message.reply_text("👥 Choose your preferred gender for matching:", reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data['finding_partner'] = True

async def handle_gender_preference(update: Update, context: ContextTypes.DEFAULT_TYPE, gender_pref):
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)

    if gender_pref == "ANY":
        await update_user_data_async(user_id, {'preferred_gender': None})
    else:
        await update_user_data_async(user_id, {'preferred_gender': Gender[gender_pref].value})

    keyboard = [
        [InlineKeyboardButton("👶 Under 18", callback_data="pref_age_UNDER_18")],
        [InlineKeyboardButton("👦 18-24", callback_data="pref_age_AGE_18_24")],
        [InlineKeyboardButton("👨 25-34", callback_data="pref_age_AGE_25_34")],
        [InlineKeyboardButton("🧔 35-44", callback_data="pref_age_AGE_35_44")],
        [InlineKeyboardButton("👴 45+", callback_data="pref_age_AGE_45_PLUS")],
        [InlineKeyboardButton("🎯 Any age", callback_data="pref_age_ANY")],
    ]
    await query.edit_message_text("🎂 Choose your preferred age group for matching:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_age_preference(update: Update, context: ContextTypes.DEFAULT_TYPE, age_pref):
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)

    if age_pref == "ANY":
        await update_user_data_async(user_id, {'preferred_age_group': None})
    else:
        await update_user_data_async(user_id, {'preferred_age_group': AgeGroup[age_pref].value})

    # Now try to find a partner
    await query.edit_message_text("🔍 Searching for a partner based on your preferences...")

    partner_id = await find_partner_async(user_id)
    if not partner_id:
        await query.edit_message_text("❌ No available partners found matching your preferences at the moment. Please try again later.")
        context.user_data.pop('finding_partner', None)
        return

    # Mark both users as in conversation and record partner
    await update_user_data_async(user_id, {'in_conversation': True, 'conversation_partner': partner_id, 'is_initiator': True})
    await update_user_data_async(partner_id, {'in_conversation': True, 'conversation_partner': user_id, 'is_initiator': False})

    user_data = await get_user_data_async(user_id)
    partner_data = await get_user_data_async(partner_id)

    await context.bot.send_message(
        chat_id=user_id,
        text=(f"✅ You've been matched with {partner_data.get('nickname') or partner_data.get('username') or partner_id}! Start chatting now.\n\nRemember: You pay 1 point per character sent.")
    )
    await context.bot.send_message(
        chat_id=partner_id,
        text=(f"✅ You've been matched with {user_data.get('nickname') or user_data.get('username') or user_id}! Start chatting now.\n\nRemember: You earn 1 point per character received.")
    )

    context.user_data.pop('finding_partner', None)

async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data or not user_data.get('in_conversation'):
        await update.message.reply_text("You're not in a conversation.")
        return
    partner_id = user_data.get('conversation_partner')
    # reset both
    await update_user_data_async(user_id, {'in_conversation': False, 'conversation_partner': None, 'is_initiator': False})
    if partner_id:
        await update_user_data_async(str(partner_id), {'in_conversation': False, 'conversation_partner': None, 'is_initiator': False})
        await context.bot.send_message(chat_id=partner_id, text="❌ Your partner has ended the conversation.")
    await update.message.reply_text("Conversation ended. Use /find to find a new partner.")

async def report_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)
    if not user_data or not user_data.get('in_conversation'):
        await update.message.reply_text("You need to be in a conversation to report a user.")
        return
    partner_id = user_data.get('conversation_partner')
    if not partner_id:
        await update.message.reply_text("No conversation partner found.")
        return
    await update_user_data_async(user_id, {'in_conversation': False, 'conversation_partner': None, 'is_initiator': False})
    await update_user_data_async(str(partner_id), {'in_conversation': False, 'conversation_partner': None, 'is_initiator': False})
    await update.message.reply_text("Thank you for your report. We will review it and take appropriate action.\n\nYour conversation has been ended.")
    await context.bot.send_message(chat_id=partner_id, text="❌ Your conversation has been ended due to a report.")

async def transact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        """
💳 Transaction Information

For deposits (adding points to your account) or withdrawals (cashing out your points), please contact our admin team.

📧 Contact: @busi_admin

We accept various payment methods and will process your transactions promptly.

⚠️ Important:
- Always verify you're contacting the official admin (@busi_admin)
- Never share your password with anyone
- Transactions are typically processed within 24 hours
- Minimum withdrawal amount: 100 points
"""
    )
    await update.message.reply_text(help_text)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)
    # We fetch user fresh as needed
    user_data = await get_user_data_async(user_id)

    if query.data == "edit_profile":
        context.user_data['setting_up_profile'] = True
        context.user_data['profile_setup_step'] = 'gender'
        keyboard = [
            [InlineKeyboardButton("♂️ Male", callback_data="gender_MALE")],
            [InlineKeyboardButton("♀️ Female", callback_data="gender_FEMALE")],
            [InlineKeyboardButton("Skip for now", callback_data="gender_skip")],
        ]
        await query.edit_message_text("Let's update your profile!\n\nFirst, select your gender:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if query.data.startswith("gender_"):
        if query.data != "gender_skip":
            gender_name = query.data.replace("gender_", "")
            gender = Gender[gender_name].value
            await update_user_data_async(user_id, {'gender': gender})
        context.user_data['profile_setup_step'] = 'nickname'
        await query.edit_message_text("Great! Now please send me your nickname:")
        return

    if query.data.startswith("age_"):
        if query.data != "age_skip":
            age_group_name = query.data.replace("age_", "")
            age_group = AgeGroup[age_group_name].value
            await update_user_data_async(user_id, {'age_group': age_group})
        await update_user_data_async(user_id, {'profile_complete': True})
        context.user_data['setting_up_profile'] = False
        await query.edit_message_text("✅ Your profile is now complete!\n\nYou can now join the pool with /join and start finding partners with /find.")
        return

    if query.data.startswith("pref_gender_"):
        gender_pref = query.data.replace("pref_gender_", "")
        await handle_gender_preference(update, context, gender_pref)
        return

    if query.data.startswith("pref_age_"):
        age_pref = query.data.replace("pref_age_", "")
        await handle_age_preference(update, context, age_pref)
        return

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_data = await get_user_data_async(user_id)

    if context.user_data.get('setting_up_profile'):
        step = context.user_data.get('profile_setup_step')
        if step == 'nickname':
            nickname = update.message.text
            await update_user_data_async(user_id, {'nickname': nickname})
            context.user_data['profile_setup_step'] = 'age'
            keyboard_rows = [
                [InlineKeyboardButton(a.value, callback_data=f"age_{a.name}")] for a in AgeGroup
            ]
            keyboard_rows.append([InlineKeyboardButton("Skip for now", callback_data="age_skip")])
            await update.message.reply_text("Nice nickname! Now select your age group:", reply_markup=InlineKeyboardMarkup(keyboard_rows))
        return

    if user_data.get('in_conversation'):
        partner_id = user_data.get('conversation_partner')
        if not partner_id:
            await update.message.reply_text("Error: No conversation partner found.")
            return
        is_initiator = user_data.get('is_initiator', False)
        partner_data = await get_user_data_async(str(partner_id))
        if update.message.text:
            message_text = update.message.text
            message_length = len(message_text)
            if is_initiator:
                user_points = user_data.get('points', 0)
                if user_points < message_length:
                    await update.message.reply_text("❌ Not enough points to send this message. Please check your balance with /points")
                    return
                # transfer points
                await update_user_data_async(user_id, {'points': user_points - message_length})
                partner_points = partner_data.get('points', 0)
                await update_user_data_async(str(partner_id), {'points': partner_points + message_length})
            await context.bot.send_message(chat_id=partner_id, text=message_text)

        elif update.message.photo:
            photo_cost = 150 if is_initiator else 0
            if is_initiator:
                user_points = user_data.get('points', 0)
                if user_points < photo_cost:
                    await update.message.reply_text(f"❌ Not enough points to send this image. You need {photo_cost} points but have only {user_points}.")
                    return
                await update_user_data_async(user_id, {'points': user_points - photo_cost})
                partner_points = partner_data.get('points', 0)
                await update_user_data_async(str(partner_id), {'points': partner_points + photo_cost})
            photo_file = await update.message.photo[-1].get_file()
            await context.bot.send_photo(chat_id=partner_id, photo=photo_file.file_id)

        elif update.message.video:
            video_cost = 250 if is_initiator else 0
            if is_initiator:
                user_points = user_data.get('points', 0)
                if user_points < video_cost:
                    await update.message.reply_text(f"❌ Not enough points to send this video. You need {video_cost} points but have only {user_points}.")
                    return
                await update_user_data_async(user_id, {'points': user_points - video_cost})
                partner_points = partner_data.get('points', 0)
                await update_user_data_async(str(partner_id), {'points': partner_points + video_cost})
            video_file = await update.message.video.get_file()
            await context.bot.send_video(chat_id=partner_id, video=video_file.file_id)
    else:
        await update.message.reply_text("You're not in a conversation. Use /find to find a partner to chat with.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)

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
            # set webhook with optional secret token header
            if WEBHOOK_SECRET:
                await application.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET)
            else:
                await application.bot.set_webhook(url=webhook_url)
            logger.info("Webhook set to %s", webhook_url)
        except Exception:
            logger.exception("Failed to set webhook to %s", webhook_url)
    else:
        logger.warning("BASE_URL empty; webhook not set. Once deployment provides an external URL, redeploy or set WEBHOOK_BASE_URL env var.")

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
    # Optional secret token guard
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret token")
    data = await request.json()
    update = Update.de_json(data, application.bot)
    # enqueue update for PTB to process
    await application.update_queue.put(update)
    return PlainTextResponse("OK")

# Local entrypoint
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
