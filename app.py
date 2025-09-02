# app.py
import os
import json
import logging
import sqlite3
from typing import Dict, Optional
from enum import Enum
from datetime import datetime

import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

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
    raise RuntimeError("TELEGRAM_TOKEN is not set. Add it in Render > Environment.")

# Optional but recommended: validate Telegram webhook calls
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")  # set any random string

# Use custom domain if you have one; otherwise Render exposes RENDER_EXTERNAL_URL
BASE_URL = (
    (os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
)
if not BASE_URL:
    logger.warning(
        "WEBHOOK_BASE_URL/RENDER_EXTERNAL_URL not set yet. The app will still start, "
        "but the webhook can't be set until Render injects RENDER_EXTERNAL_URL on first deploy."
    )

DB_FILE = os.getenv("DB_FILE", "lomitalk.db")

# --------------------
# Data layer (SQLite)
# --------------------
# NOTE: Render free tier has an ephemeral filesystem. Your SQLite DB will reset on redeploys or restarts.
# For persistence, switch to Postgres later. This code is kept as-is for minimal changes.

def _connect_db():
    # create a fresh connection per call (safe with async handlers)
    return sqlite3.connect(DB_FILE, check_same_thread=False)


def setup_database():
    try:
        conn = _connect_db()
        cursor = conn.cursor()
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                points INTEGER,
                profile_complete INTEGER,
                in_pool INTEGER,
                in_conversation INTEGER,
                conversation_partner TEXT,
                is_initiator INTEGER,
                joined_date TEXT,
                username TEXT,
                gender TEXT,
                age_group TEXT,
                nickname TEXT,
                preferred_gender TEXT,
                preferred_age_group TEXT
            )
            '''
        )
        conn.commit()
        conn.close()
        logger.info("Database setup complete. Table 'users' is ready.")
    except sqlite3.Error as e:
        logger.exception("Error setting up database")
        raise RuntimeError("Failed to set up SQLite database.") from e


def get_user_data(user_id) -> Dict:
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (str(user_id),))
        row = cursor.fetchone()
        if row:
            user_data = dict(row)
            user_data['profile_complete'] = bool(user_data['profile_complete'])
            user_data['in_pool'] = bool(user_data['in_pool'])
            user_data['in_conversation'] = bool(user_data['in_conversation'])
            user_data['is_initiator'] = bool(user_data['is_initiator'])
            if user_data['conversation_partner']:
                user_data['conversation_partner'] = safe_int(user_data.get('conversation_partner'))
            return user_data
        return {}
    except sqlite3.Error:
        logger.exception(f"Error retrieving user data for {user_id}")
        return {}
    finally:
        conn.close()


def update_user_data(user_id, updates):
    conn = _connect_db()
    cursor = conn.cursor()
    try:
        current_data = get_user_data(user_id)
        current_data.update(updates)
        data_to_save = {
            'user_id': str(user_id),
            'points': current_data.get('points', 1000),
            'profile_complete': int(current_data.get('profile_complete', False)),
            'in_pool': int(current_data.get('in_pool', False)),
            'in_conversation': int(current_data.get('in_conversation', False)),
            'conversation_partner': str(current_data.get('conversation_partner', '')),
            'is_initiator': int(current_data.get('is_initiator', False)),
            'joined_date': current_data.get('joined_date', datetime.now().isoformat()),
            'username': current_data.get('username'),
            'gender': current_data.get('gender'),
            'age_group': current_data.get('age_group'),
            'nickname': current_data.get('nickname'),
            'preferred_gender': current_data.get('preferred_gender'),
            'preferred_age_group': current_data.get('preferred_age_group')
        }
        cursor.execute(
            '''
            INSERT OR REPLACE INTO users (
                user_id, points, profile_complete, in_pool, in_conversation,
                conversation_partner, is_initiator, joined_date, username,
                gender, age_group, nickname, preferred_gender, preferred_age_group
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                data_to_save['user_id'],
                data_to_save['points'],
                data_to_save['profile_complete'],
                data_to_save['in_pool'],
                data_to_save['in_conversation'],
                data_to_save['conversation_partner'],
                data_to_save['is_initiator'],
                data_to_save['joined_date'],
                data_to_save['username'],
                data_to_save['gender'],
                data_to_save['age_group'],
                data_to_save['nickname'],
                data_to_save['preferred_gender'],
                data_to_save['preferred_age_group']
            ),
        )
        conn.commit()
    except sqlite3.Error:
        logger.exception(f"Error updating user data for {user_id}")
        conn.rollback()
    finally:
        conn.close()


def get_all_users() -> Dict[str, Dict]:
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT * FROM users')
        rows = cursor.fetchall()
        all_users = {}
        for row in rows:
            user_data = dict(row)
            user_data['profile_complete'] = bool(user_data['profile_complete'])
            user_data['in_pool'] = bool(user_data['in_pool'])
            user_data['in_conversation'] = bool(user_data['in_conversation'])
            user_data['is_initiator'] = bool(user_data['is_initiator'])
            if user_data['conversation_partner']:
                user_data['conversation_partner'] = int(user_data['conversation_partner'])
            all_users[str(user_data['user_id'])] = user_data
        return all_users
    except sqlite3.Error:
        logger.exception("Error retrieving all users")
        return {}
    finally:
        conn.close()

# --------------------
# Domain logic
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


def find_partner(user_id):
    user_data = get_user_data(user_id)
    if not user_data.get('in_pool', False):
        return None

    all_users = get_all_users()
    user_pref_gender = user_data.get('preferred_gender')
    user_pref_age = user_data.get('preferred_age_group')
    user_id_str = str(user_id)

    for uid, data in all_users.items():
        if (
            uid != user_id_str
            and data.get('in_pool', False)
            and not data.get('in_conversation', False)
            and data.get('profile_complete', False)
        ):
            if user_pref_gender and user_pref_gender != data.get('gender'):
                continue
            if user_pref_age and user_pref_age != data.get('age_group'):
                continue
            return int(uid)
    return None

# --------------------
# Bot handlers 
# --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)

    if not user_data:
        update_user_data(
            user_id,
            {
                'points': 1000,
                'profile_complete': False,
                'in_pool': False,
                'in_conversation': False,
                'conversation_partner': None,
                'joined_date': datetime.now().isoformat(),
                'username': update.effective_user.username,
            },
        )
        await update.message.reply_text(
            "👋 Welcome to LomiTalk!\n\n"
            "You've received 1000 free points to start with. Complete your profile to begin matching with partners.\n\n"
            "Use /profile to set up your profile."
        )
    else:
        current_username = update.effective_user.username
        if user_data.get('username') != current_username:
            update_user_data(user_id, {'username': current_username})
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
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)

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
    else:
        context.user_data['setting_up_profile'] = True
        context.user_data['profile_setup_step'] = 'gender'
        keyboard = [
            [InlineKeyboardButton("♂️ Male", callback_data="gender_MALE")],
            [InlineKeyboardButton("♀️ Female", callback_data="gender_FEMALE")],
            [InlineKeyboardButton("Skip for now", callback_data="gender_skip")],
        ]
        await update.message.reply_text("Let's set up your profile!\n\nFirst, select your gender:", reply_markup=InlineKeyboardMarkup(keyboard))


async def points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    if not user_data:
        await update.message.reply_text("Please use /start first to initialize your account.")
        return
    await update.message.reply_text(f"💰 Your current points balance: {user_data.get('points', 0)}")


async def join_pool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    if not user_data:
        await update.message.reply_text("Please use /start first to initialize your account.")
        return
    if not user_data.get('profile_complete'):
        await update.message.reply_text("Please complete your profile with /profile before joining the pool.")
        return
    if user_data.get('in_pool'):
        await update.message.reply_text("You're already in the pool!")
        return
    update_user_data(user_id, {'in_pool': True})
    await update.message.reply_text("✅ You've joined the pool! You can now be matched with other users.")


async def leave_pool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    if not user_data:
        await update.message.reply_text("Please use /start first to initialize your account.")
        return
    if not user_data.get('in_pool'):
        await update.message.reply_text("You're not in the pool.")
        return
    update_user_data(user_id, {'in_pool': False})
    await update.message.reply_text("You've left the pool. You will no longer be matched with partners.")


async def find_partner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)

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
    user_id = update.effective_user.id

    if gender_pref == "ANY":
        update_user_data(user_id, {'preferred_gender': None})
    else:
        update_user_data(user_id, {'preferred_gender': Gender[gender_pref].value})

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
    user_id = update.effective_user.id

    if age_pref == "ANY":
        update_user_data(user_id, {'preferred_age_group': None})
    else:
        update_user_data(user_id, {'preferred_age_group': AgeGroup[age_pref].value})

    await query.edit_message_text("🔍 Searching for a partner based on your preferences...")

    partner_id = find_partner(user_id)
    if not partner_id:
        await query.edit_message_text("❌ No available partners found matching your preferences at the moment. Please try again later.")
        context.user_data.pop('finding_partner', None)
        return

    update_user_data(user_id, {'in_conversation': True, 'conversation_partner': partner_id, 'is_initiator': True})
    user_data = get_user_data(user_id)
    partner_data = get_user_data(partner_id)
    update_user_data(partner_id, {'in_conversation': True, 'conversation_partner': user_id, 'is_initiator': False})

    await context.bot.send_message(chat_id=user_id, text=(
        f"✅ You've been matched with {partner_data.get('nickname')}! Start chatting now.\n\n"
        "Remember: You pay 1 point per character sent."
    ))
    await context.bot.send_message(chat_id=partner_id, text=(
        f"✅ You've been matched with {user_data.get('nickname')}! Start chatting now.\n\n"
        "Remember: You earn 1 point per character received."
    ))

    context.user_data.pop('finding_partner', None)


async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)

    if not user_data.get('in_conversation'):
        await update.message.reply_text("You're not in a conversation.")
        return

    partner_id = user_data.get('conversation_partner')
    update_user_data(user_id, {'in_conversation': False, 'conversation_partner': None, 'is_initiator': False})

    if partner_id:
        update_user_data(partner_id, {'in_conversation': False, 'conversation_partner': None, 'is_initiator': False})
        await context.bot.send_message(chat_id=partner_id, text="❌ Your partner has ended the conversation.")

    await update.message.reply_text("Conversation ended. Use /find to find a new partner.")


async def report_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    if not user_data.get('in_conversation'):
        await update.message.reply_text("You need to be in a conversation to report a user.")
        return

    partner_id = user_data.get('conversation_partner')
    if not partner_id:
        await update.message.reply_text("No conversation partner found.")
        return

    await update.message.reply_text(
        "Thank you for your report. We will review it and take appropriate action.\n\nYour conversation has been ended."
    )

    update_user_data(user_id, {'in_conversation': False, 'conversation_partner': None, 'is_initiator': False})
    update_user_data(partner_id, {'in_conversation': False, 'conversation_partner': None, 'is_initiator': False})
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
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)

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
            update_user_data(user_id, {'gender': gender})
        context.user_data['profile_setup_step'] = 'nickname'
        await query.edit_message_text("Great! Now please send me your nickname:")
        return

    if query.data.startswith("age_"):
        if query.data != "age_skip":
            age_group_name = query.data.replace("age_", "")
            age_group = AgeGroup[age_group_name].value
            update_user_data(user_id, {'age_group': age_group})
        update_user_data(user_id, {'profile_complete': True})
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
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)

    if context.user_data.get('setting_up_profile'):
        step = context.user_data.get('profile_setup_step')
        if step == 'nickname':
            nickname = update.message.text
            update_user_data(user_id, {'nickname': nickname})
            context.user_data['profile_setup_step'] = 'age'
            keyboard_rows = [
                [InlineKeyboardButton(a.value, callback_data=f"age_{a.name}")]
                for a in AgeGroup
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
        partner_data = get_user_data(partner_id)

        if update.message.text:
            message_text = update.message.text
            message_length = len(message_text)
            if is_initiator:
                user_points = user_data.get('points', 0)
                if user_points < message_length:
                    await update.message.reply_text(
                        "❌ Not enough points to send this message. Please check your balance with /points"
                    )
                    return
                update_user_data(user_id, {'points': user_points - message_length})
                partner_points = partner_data.get('points', 0)
                update_user_data(partner_id, {'points': partner_points + message_length})
            await context.bot.send_message(chat_id=partner_id, text=message_text)

        elif update.message.photo:
            photo_cost = 150 if is_initiator else 0
            if is_initiator:
                user_points = user_data.get('points', 0)
                if user_points < photo_cost:
                    await update.message.reply_text(
                        f"❌ Not enough points to send this image. You need {photo_cost} points but have only {user_points}."
                    )
                    return
                update_user_data(user_id, {'points': user_points - photo_cost})
                partner_points = partner_data.get('points', 0)
                update_user_data(partner_id, {'points': partner_points + photo_cost})
            photo_file = await update.message.photo[-1].get_file()
            await context.bot.send_photo(chat_id=partner_id, photo=photo_file.file_id)

        elif update.message.video:
            video_cost = 250 if is_initiator else 0
            if is_initiator:
                user_points = user_data.get('points', 0)
                if user_points < video_cost:
                    await update.message.reply_text(
                        f"❌ Not enough points to send this video. You need {video_cost} points but have only {user_points}."
                    )
                    return
                update_user_data(user_id, {'points': user_points - video_cost})
                partner_points = partner_data.get('points', 0)
                update_user_data(partner_id, {'points': partner_points + video_cost})
            video_file = await update.message.video.get_file()
            await context.bot.send_video(chat_id=partner_id, video=video_file.file_id)
    else:
        await update.message.reply_text("You're not in a conversation. Use /find to find a partner to chat with.")
        
def safe_int(value):
    if value is None or str(value).lower() == "none":
        return None
    return int(value)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)


# --------------------
# Build Application and FastAPI
# --------------------
application = Application.builder().token(TOKEN).build()
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

# Web app (one server only!)
app = FastAPI()

WEBHOOK_PATH = f"/{TOKEN}"

@app.on_event("startup")
async def _on_startup():
    setup_database()
    await application.initialize()
    await application.start()
    if BASE_URL:
        webhook_url = f"{BASE_URL}{WEBHOOK_PATH}"
        await application.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET)
        logger.info(f"Webhook set to {webhook_url}")
    else:
        logger.warning(
            "BASE_URL is empty. Webhook not set yet. Once Render injects RENDER_EXTERNAL_URL, redeploy to set it."
        )


@app.on_event("shutdown")
async def _on_shutdown():
    await application.stop()
    await application.shutdown()


@app.get("/")
async def home():
    return PlainTextResponse("LomiTalk Bot is running!")


@app.get("/health")
async def health():
    return PlainTextResponse("OK")


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    # Optional check of secret token from Telegram
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret token")
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)
    return PlainTextResponse("OK")


if __name__ == "__main__":
    # Local dev: uvicorn app:app --reload
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


