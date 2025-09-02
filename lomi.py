import os
import json
import logging
import traceback
from enum import Enum
from datetime import datetime
from flask import Flask, request

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from dotenv import load_dotenv

# Load environment variables from .env file for local development
load_dotenv()

# --- Configuration & Setup ---

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot token from environment variable
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("No TELEGRAM_BOT_TOKEN found in environment variables")

# --- IMPORTANT: Data Persistence Warning for Render ---
# Render's filesystem is ephemeral, meaning this JSON file will be lost on every deploy or restart.
# For production, you MUST use a persistent storage solution.
# 1. Render Disks: Mount a persistent disk to your service to save this file.
# 2. Database: Use a service like Redis, PostgreSQL (available on Render), or another database.
USER_DATA_FILE = os.path.join(os.getenv("RENDER_DISK_PATH", "."), "user_data.json")

# Create Flask app
app = Flask(__name__)

# --- Enums and Data Models ---

class AgeGroup(Enum):
    UNDER_18 = "Under 18"
    AGE_18_24 = "18-24"
    AGE_25_34 = "25-34"
    AGE_35_44 = "35-44"
    AGE_45_PLUS = "45+"

class Gender(Enum):
    MALE = "Male"
    FEMALE = "Female"

# --- Data Handling Functions ---

def load_user_data() -> dict:
    try:
        with open(USER_DATA_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_user_data(data: dict):
    # Ensure the directory exists if a path is specified (like on a Render Disk)
    os.makedirs(os.path.dirname(USER_DATA_FILE), exist_ok=True)
    with open(USER_DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

def get_user_data(user_id: int) -> dict:
    data = load_user_data()
    return data.get(str(user_id), {})

def update_user_data(user_id: int, updates: dict):
    data = load_user_data()
    user_id_str = str(user_id)
    if user_id_str not in data:
        data[user_id_str] = {}
    data[user_id_str].update(updates)
    save_user_data(data)

# --- Core Bot Logic ---

def find_partner(user_id: int) -> int | None:
    user_data = get_user_data(user_id)
    all_users = load_user_data()
    user_pref_gender = user_data.get("preferred_gender")
    user_pref_age = user_data.get("preferred_age_group")
    user_id_str = str(user_id)

    for uid, data in all_users.items():
        if (
            uid != user_id_str
            and data.get("in_pool", False)
            and not data.get("in_conversation", False)
            and data.get("profile_complete", False)
        ):
            if user_pref_gender and user_pref_gender != data.get("gender"):
                continue
            if user_pref_age and user_pref_age != data.get("age_group"):
                continue
            return int(uid)
    return None

# --- Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    user_data = get_user_data(user_id)

    if not user_data:
        update_user_data(
            user_id,
            {
                "points": 1000,
                "profile_complete": False,
                "in_pool": False,
                "in_conversation": False,
                "conversation_partner": None,
                "joined_date": datetime.now().isoformat(),
                "username": user.username,
            },
        )
        await update.message.reply_text(
            "👋 Welcome to LomiTalk!\n\n"
            "You've received 1000 free points to start with. "
            "Complete your profile to begin matching.\n\n"
            "Use /profile to set up your profile."
        )
    else:
        if user_data.get("username") != user.username:
            update_user_data(user_id, {"username": user.username})
        await update.message.reply_text(
            "Welcome back to LomiTalk!\n\n"
            f"Your current points: {user_data.get('points', 0)}\n"
            "Use /help to see all available commands."
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🤖 LomiTalk Help Guide

💡 How it works:
- Complete your profile with sex, nickname, and age group.
- Join the pool ✅ for FREE to earn points.
- Use /find to choose preferences and find a partner 🔍.
- Chat with your partner 💬.
- The person who initiates the conversation PAYS 1 point per character sent.
- The person selected from the pool EARNS 1 point per character received.

💰 Payment System:
- Initiator: PAYS 1 point per character sent.
- Partner: EARNS 1 point per character received.
- Images: 150 points, Videos: 250 points.
- No points created or destroyed - only transferred.

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
    """
    await update.message.reply_text(help_text)

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    if not user_data:
        await update.message.reply_text("Please use /start first.")
        return

    if context.user_data.get("state", "").startswith("PROFILE_"):
        await update.message.reply_text("Please complete your current profile setup first.")
        return

    if user_data.get("profile_complete"):
        profile_text = (
            f"📋 Your Profile:\n"
            f"Nickname: {user_data.get('nickname', 'Not set')}\n"
            f"Gender: {user_data.get('gender', 'Not set')}\n"
            f"Age Group: {user_data.get('age_group', 'Not set')}\n"
            f"Preferred Gender: {user_data.get('preferred_gender', 'Any')}\n"
            f"Preferred Age Group: {user_data.get('preferred_age_group', 'Any')}"
        )
        keyboard = [[InlineKeyboardButton("Edit Profile", callback_data="edit_profile")]]
        await update.message.reply_text(profile_text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        # Start profile setup from scratch
        context.user_data["state"] = "PROFILE_GENDER"
        keyboard = [
            [InlineKeyboardButton("♂️ Male", callback_data="gender_MALE")],
            [InlineKeyboardButton("♀️ Female", callback_data="gender_FEMALE")],
            [InlineKeyboardButton("Skip for now", callback_data="gender_skip")]
        ]
        await update.message.reply_text("Let's set up your profile!\n\nFirst, select your gender:", reply_markup=InlineKeyboardMarkup(keyboard))

async def points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    await update.message.reply_text(f"💰 Your current points balance: {user_data.get('points', 0)}")

async def join_pool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    if not user_data.get("profile_complete"):
        await update.message.reply_text("Please complete your profile with /profile before joining.")
        return
    if user_data.get("in_pool"):
        await update.message.reply_text("You're already in the pool!")
        return
    update_user_data(user_id, {"in_pool": True})
    await update.message.reply_text("✅ You've joined the pool and can now be matched.")

async def leave_pool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    update_user_data(user_id, {"in_pool": False})
    await update.message.reply_text("You've left the pool.")

async def find_partner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    if not user_data.get("profile_complete"):
        await update.message.reply_text("Please complete your profile with /profile first.")
        return
    if user_data.get("in_conversation"):
        await update.message.reply_text("You're already in a conversation! Use /end to end it.")
        return
    
    keyboard = [
        [InlineKeyboardButton("♂️ Male", callback_data="pref_gender_MALE")],
        [InlineKeyboardButton("♀️ Female", callback_data="pref_gender_FEMALE")],
        [InlineKeyboardButton("Any gender", callback_data="pref_gender_ANY")],
    ]
    await update.message.reply_text("👥 Choose your preferred gender for matching:", reply_markup=InlineKeyboardMarkup(keyboard))

async def _end_conversation_logic(user_id: int, partner_id: int | None, context: ContextTypes.DEFAULT_TYPE):
    """Helper function to end conversation for both users."""
    update_user_data(user_id, {"in_conversation": False, "conversation_partner": None, "is_initiator": None})
    if partner_id:
        update_user_data(partner_id, {"in_conversation": False, "conversation_partner": None, "is_initiator": None})
        return True
    return False

async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    if not user_data.get("in_conversation"):
        await update.message.reply_text("You're not in a conversation.")
        return

    partner_id = user_data.get("conversation_partner")
    if await _end_conversation_logic(user_id, partner_id, context):
         await context.bot.send_message(chat_id=partner_id, text="❌ Your partner has ended the conversation.")
    
    await update.message.reply_text("Conversation ended. Use /find to start a new one.")

async def report_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    if not user_data.get("in_conversation"):
        await update.message.reply_text("You must be in a conversation to report a user.")
        return

    partner_id = user_data.get("conversation_partner")
    logger.warning(f"User {user_id} reported partner {partner_id}.")

    if await _end_conversation_logic(user_id, partner_id, context):
        await context.bot.send_message(chat_id=partner_id, text="❌ Your conversation has been ended due to a report.")
    
    await update.message.reply_text("Thank you for your report. The conversation has been ended.")

async def transact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💳 For deposits or withdrawals, please contact our admin team: @busi_admin"
    )

# --- Callback & Message Handlers ---

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "edit_profile":
        context.user_data["state"] = "PROFILE_GENDER"
        keyboard = [
            [InlineKeyboardButton("♂️ Male", callback_data="gender_MALE")],
            [InlineKeyboardButton("♀️ Female", callback_data="gender_FEMALE")],
            [InlineKeyboardButton("Keep Current", callback_data="gender_skip")]
        ]
        await query.edit_message_text("Let's update your profile!\n\nSelect your gender:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("gender_"):
        if data != "gender_skip":
            gender = Gender[data.split("_")[1]].value
            update_user_data(user_id, {"gender": gender})
        context.user_data["state"] = "PROFILE_NICKNAME"
        await query.edit_message_text("Great! Now please send me your nickname:")

    elif data.startswith("age_"):
        if data != "age_skip":
            age_group = AgeGroup[data.split("_")[1]].value
            update_user_data(user_id, {"age_group": age_group})
        update_user_data(user_id, {"profile_complete": True})
        context.user_data.pop("state", None)
        await query.edit_message_text("✅ Your profile is complete!\nUse /join and /find to connect.")

    elif data.startswith("pref_gender_"):
        pref = data.split("_")[-1]
        update_user_data(user_id, {"preferred_gender": Gender[pref].value if pref != "ANY" else None})
        keyboard = [[InlineKeyboardButton(a.value, callback_data=f"pref_age_{a.name}")] for a in AgeGroup]
        keyboard.append([InlineKeyboardButton("🎯 Any age", callback_data="pref_age_ANY")])
        await query.edit_message_text("🎂 Choose your preferred age group:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("pref_age_"):
        pref = data.split("_")[-1]
        update_user_data(user_id, {"preferred_age_group": AgeGroup[pref].value if pref != "ANY" else None})
        await query.edit_message_text("🔍 Searching for a partner...")

        partner_id = find_partner(user_id)
        if partner_id:
            user_data = get_user_data(user_id)
            partner_data = get_user_data(partner_id)
            
            update_user_data(user_id, {"in_conversation": True, "conversation_partner": partner_id, "is_initiator": True})
            update_user_data(partner_id, {"in_conversation": True, "conversation_partner": user_id, "is_initiator": False})

            await context.bot.send_message(user_id, f"✅ Match found: {partner_data.get('nickname')}! You are the initiator and will pay for messages.")
            await context.bot.send_message(partner_id, f"✅ You've been matched with {user_data.get('nickname')}! You will earn points for messages you receive.")
        else:
            await query.edit_message_text("❌ No available partners found. Please try again later.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)

    # Handle profile setup
    if context.user_data.get("state") == "PROFILE_NICKNAME":
        update_user_data(user_id, {"nickname": update.message.text})
        context.user_data["state"] = "PROFILE_AGE"
        keyboard = [
            [InlineKeyboardButton(a.value, callback_data=f"age_{a.name}") for a in AgeGroup],
            [InlineKeyboardButton("Keep Current", callback_data="age_skip")]
        ]
        await update.message.reply_text("Nice nickname! Now select your age group:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # Handle conversation messages
    if user_data.get("in_conversation"):
        partner_id = user_data.get("conversation_partner")
        if not partner_id: return

        is_initiator = user_data.get("is_initiator", False)
        cost = 0
        
        if update.message.text: cost = len(update.message.text)
        elif update.message.photo: cost = 150
        elif update.message.video: cost = 250
        
        if is_initiator:
            user_points = user_data.get("points", 0)
            if user_points < cost:
                await update.message.reply_text(f"❌ Not enough points. Cost: {cost}, Your points: {user_points}.")
                return
            
            update_user_data(user_id, {"points": user_points - cost})
            partner_data = get_user_data(partner_id)
            update_user_data(partner_id, {"points": partner_data.get("points", 0) + cost})
        
        # Using forward_message is cleaner as it handles all media types and captions
        await update.message.forward(chat_id=partner_id)
    else:
        await update.message.reply_text("You are not in a conversation. Use /find to start one.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    logger.error("".join(tb_list))

# --- Flask Webserver and PTB Application Setup ---

application = Application.builder().token(TOKEN).build()

# Command handlers
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

# Message and Callback handlers
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
application.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_message))

# Error handler
application.add_error_handler(error_handler)

# --- Flask Routes ---

@app.route("/")
def index():
    return "LomiTalk Bot is running!"

@app.route("/health")
def health_check():
    return "OK", 200

# This is the webhook endpoint that Telegram will call
@app.route(f"/{TOKEN}", methods=["POST"])
async def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    await application.process_update(update)
    return "OK", 200

# Call this endpoint ONCE after deploying to set the webhook
@app.route("/set_webhook", methods=["GET"])
async def set_webhook():
    hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME")
    if not hostname:
        return "RENDER_EXTERNAL_HOSTNAME environment variable not set", 500
    
    webhook_url = f"https://{hostname}/{TOKEN}"
    await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
    return f"Webhook successfully set to {webhook_url}", 200

# This part is for local development and will not be used by Render's Gunicorn server
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
