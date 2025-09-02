import os
import sqlite3
import logging
from enum import Enum
from datetime import datetime
from typing import Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    ContextTypes, CallbackQueryHandler, filters
)
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot token from environment
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

# Age groups
class AgeGroup(Enum):
    UNDER_18 = "Under 18"
    AGE_18_24 = "18-24"
    AGE_25_34 = "25-34"
    AGE_35_44 = "35-44"
    AGE_45_PLUS = "45+"

# Gender options (only Male and Female)
class Gender(Enum):
    MALE = "Male"
    FEMALE = "Female"

# Initialize SQLite database
def init_db():
    conn = sqlite3.connect('lomitalk.db')
    c = conn.cursor()
    
    # Create users table
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            points INTEGER DEFAULT 1000,
            profile_complete BOOLEAN DEFAULT FALSE,
            in_pool BOOLEAN DEFAULT FALSE,
            in_conversation BOOLEAN DEFAULT FALSE,
            conversation_partner INTEGER,
            is_initiator BOOLEAN DEFAULT FALSE,
            username TEXT,
            nickname TEXT,
            gender TEXT,
            age_group TEXT,
            preferred_gender TEXT,
            preferred_age_group TEXT,
            joined_date TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

# Get user data from database
def get_user_data(user_id):
    conn = sqlite3.connect('lomitalk.db')
    c = conn.cursor()
    
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    
    conn.close()
    
    if user:
        # Convert tuple to dictionary
        keys = ['user_id', 'points', 'profile_complete', 'in_pool', 'in_conversation',
                'conversation_partner', 'is_initiator', 'username', 'nickname',
                'gender', 'age_group', 'preferred_gender', 'preferred_age_group', 'joined_date']
        return dict(zip(keys, user))
    return {}

# Update user data in database
def update_user_data(user_id, updates):
    conn = sqlite3.connect('lomitalk.db')
    c = conn.cursor()
    
    # Check if user exists
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    
    if user:
        # Update existing user
        set_clause = ', '.join([f"{key} = ?" for key in updates.keys()])
        values = list(updates.values())
        values.append(user_id)
        c.execute(f"UPDATE users SET {set_clause} WHERE user_id = ?", values)
    else:
        # Insert new user
        keys = ['user_id'] + list(updates.keys())
        placeholders = ', '.join(['?'] * len(keys))
        values = [user_id] + list(updates.values())
        c.execute(f"INSERT INTO users ({', '.join(keys)}) VALUES ({placeholders})", values)
    
    conn.commit()
    conn.close()

# Find a partner for the user with preferences
def find_partner(user_id):
    user_data = get_user_data(user_id)
    if not user_data.get('in_pool', False):
        return None
    
    conn = sqlite3.connect('lomitalk.db')
    c = conn.cursor()
    
    user_pref_gender = user_data.get('preferred_gender')
    user_pref_age = user_data.get('preferred_age_group')
    
    # Build query based on preferences
    query = """
        SELECT user_id FROM users 
        WHERE user_id != ? 
        AND in_pool = 1 
        AND in_conversation = 0 
        AND profile_complete = 1
    """
    params = [user_id]
    
    if user_pref_gender:
        query += " AND gender = ?"
        params.append(user_pref_gender)
    
    if user_pref_age:
        query += " AND age_group = ?"
        params.append(user_pref_age)
    
    c.execute(query, params)
    partner = c.fetchone()
    
    conn.close()
    
    if partner:
        return partner[0]  # Return user_id
    
    return None

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    
    if not user_data:
        # New user - initialize with free points
        update_user_data(user_id, {
            'points': 1000,
            'profile_complete': False,
            'in_pool': False,
            'in_conversation': False,
            'conversation_partner': None,
            'is_initiator': False,
            'username': update.effective_user.username,
            'joined_date': datetime.now().isoformat()
        })
        await update.message.reply_text(
            "👋 Welcome to LomiTalk!\n\n"
            "You've received 1000 free points (1 🍋) to start with!\n"
            "💵 1 🍋 = 15 Birr\n\n"
            "Complete your profile to begin matching with partners.\n\n"
            "Use /profile to set up your profile."
        )
    else:
        points_balance = user_data.get('points', 0)
        lomi_balance = points_balance / 1000
        
        await update.message.reply_text(
            "Welcome back to LomiTalk!\n\n"
            f"Your current points: {points_balance}\n"
            f"Lomi balance: {lomi_balance:.3f} 🍋\n\n"
            "Use /help to see all available commands."
        )

# Help command
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🤖 LomiTalk Help Guide

💡 How it works:
- Complete your profile with sex, nickname, and age group
- Join the pool ✅ for FREE to earn points
- Use /find to choose preferences and find a partner 🔍
- Chat with your partner 💬
- The person who initiates the conversation PAYS 1 point per character sent
- The person selected from the pool EARNS 1 point per character received

💰 Payment System:
- 1000 points = 1 🍋
- 1 🍋 = 15 Birr
- Initiator: PAYS 1 point per character sent
- Partner: EARNS 1 point per character received
- Images: 150 points, Videos: 250 points
- No points created or destroyed - only transferred

⚡️ Commands:
/start - Register and get 1000 free points (1 🍋)
/profile - Edit your profile
/points - Check your points and Lomi balance
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
    await update.message.reply_text(help_text)

# Profile command
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    
    if not user_data:
        await update.message.reply_text("Please use /start first to initialize your account.")
        return
    
    # Check if we're in the middle of profile setup
    if context.user_data.get('setting_up_profile'):
        await update.message.reply_text("Please complete your current profile setup first.")
        return
    
    # Show current profile if exists
    if user_data.get('profile_complete'):
        profile_text = f"""
📋 Your Profile:
Nickname: {user_data.get('nickname', 'Not set')}
Gender: {user_data.get('gender', 'Not set')}
Age Group: {user_data.get('age_group', 'Not set')}
Preferred Gender: {user_data.get('preferred_gender', 'Any')}
Preferred Age Group: {user_data.get('preferred_age_group', 'Any')}
        """
        keyboard = [
            [InlineKeyboardButton("Edit Profile", callback_data="edit_profile")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(profile_text, reply_markup=reply_markup)
    else:
        # Start profile setup
        context.user_data['setting_up_profile'] = True
        context.user_data['profile_setup_step'] = 'gender'
        
        # Only show Male and Female options for profile setup
        keyboard = [
            [InlineKeyboardButton("♂️ Male", callback_data="gender_MALE")],
            [InlineKeyboardButton("♀️ Female", callback_data="gender_FEMALE")],
            [InlineKeyboardButton("Skip for now", callback_data="gender_skip")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Let's set up your profile!\n\nFirst, select your gender:",
            reply_markup=reply_markup
        )

# Points command
async def points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    
    if not user_data:
        await update.message.reply_text("Please use /start first to initialize your account.")
        return
    
    points_balance = user_data.get('points', 0)
    lomi_balance = points_balance / 1000
    
    await update.message.reply_text(
        f"💰 Your current points balance: {points_balance}\n"
        f"🍋 Lomi balance: {lomi_balance:.3f} Lomi\n\n"
        f"💵 Exchange rate: 1 🍋 = 15 Birr\n"
        f"1 🍋 = 1000 points"
    )

# Join pool command
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

# Leave pool command
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

# Find partner command
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
    
    # Ask for gender preference (only Male and Female options)
    keyboard = [
        [InlineKeyboardButton("♂️ Male", callback_data="pref_gender_MALE")],
        [InlineKeyboardButton("♀️ Female", callback_data="pref_gender_FEMALE")],
        [InlineKeyboardButton("🌈 Any gender", callback_data="pref_gender_ANY")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "👥 Choose your preferred gender for matching:",
        reply_markup=reply_markup
    )
    
    context.user_data['finding_partner'] = True

# Handle gender preference selection
async def handle_gender_preference(update: Update, context: ContextTypes.DEFAULT_TYPE, gender_pref):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    if gender_pref == "ANY":
        update_user_data(user_id, {'preferred_gender': None})
    else:
        update_user_data(user_id, {'preferred_gender': Gender[gender_pref].value})
    
    # Now ask for age group preference
    keyboard = [
        [InlineKeyboardButton("👶 Under 18", callback_data="pref_age_UNDER_18")],
        [InlineKeyboardButton("👦 18-24", callback_data="pref_age_AGE_18_24")],
        [InlineKeyboardButton("👨 25-34", callback_data="pref_age_AGE_25_34")],
        [InlineKeyboardButton("🧔 35-44", callback_data="pref_age_AGE_35_44")],
        [InlineKeyboardButton("👴 45+", callback_data="pref_age_AGE_45_PLUS")],
        [InlineKeyboardButton("🎯 Any age", callback_data="pref_age_ANY")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🎂 Choose your preferred age group for matching:",
        reply_markup=reply_markup
    )

# Handle age preference selection and start searching
async def handle_age_preference(update: Update, context: ContextTypes.DEFAULT_TYPE, age_pref):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    if age_pref == "ANY":
        update_user_data(user_id, {'preferred_age_group': None})
    else:
        update_user_data(user_id, {'preferred_age_group': AgeGroup[age_pref].value})
    
    # Start searching for partner with preferences
    await query.edit_message_text("🔍 Searching for a partner based on your preferences...")
    
    partner_id = find_partner(user_id)
    if not partner_id:
        await query.edit_message_text("❌ No available partners found matching your preferences at the moment. Please try again later.")
        context.user_data.pop('finding_partner', None)
        return
    
    # Start conversation
    update_user_data(user_id, {
        'in_conversation': True,
        'conversation_partner': partner_id,
        'is_initiator': True
    })
    
    user_data = get_user_data(user_id)
    partner_data = get_user_data(partner_id)
    update_user_data(partner_id, {
        'in_conversation': True,
        'conversation_partner': user_id,
        'is_initiator': False
    })
    
    await context.bot.send_message(
        chat_id=user_id,
        text=f"✅ You've been matched with {partner_data.get('nickname')}! Start chatting now.\n\n"
             "Remember: You pay 1 point per character sent."
    )
    
    await context.bot.send_message(
        chat_id=partner_id,
        text=f"✅ You've been matched with {user_data.get('nickname')}! Start chatting now.\n\n"
             "Remember: You earn 1 point per character received."
    )
    
    context.user_data.pop('finding_partner', None)

# End conversation command
async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    
    if not user_data.get('in_conversation'):
        await update.message.reply_text("You're not in a conversation.")
        return
    
    partner_id = user_data.get('conversation_partner')
    
    # End conversation for both users
    update_user_data(user_id, {
        'in_conversation': False,
        'conversation_partner': None,
        'is_initiator': False
    })
    
    if partner_id:
        update_user_data(partner_id, {
            'in_conversation': False,
            'conversation_partner': None,
            'is_initiator': False
        })
        
        await context.bot.send_message(
            chat_id=partner_id,
            text="❌ Your partner has ended the conversation."
        )
    
    await update.message.reply_text("Conversation ended. Use /find to find a new partner.")

# Report command
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
    
    # In a real implementation, you would log the report and potentially take action
    await update.message.reply_text(
        "Thank you for your report. We will review it and take appropriate action.\n\n"
        "Your conversation has been ended."
    )
    
    # End the conversation
    update_user_data(user_id, {
        'in_conversation': False,
        'conversation_partner': None,
        'is_initiator': False
    })
    
    update_user_data(partner_id, {
        'in_conversation': False,
        'conversation_partner': None,
        'is_initiator': False
    })
    
    await context.bot.send_message(
        chat_id=partner_id,
        text="❌ Your conversation has been ended due to a report."
    )

# Transaction command for deposits and withdrawals
async def transact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    points_balance = user_data.get('points', 0)
    lomi_balance = points_balance / 1000
    
    help_text = f"""
💳 Transaction Information

💰 Your current balance:
- Points: {points_balance}
- Lomi: {lomi_balance:.3f} 🍋

💵 Exchange rate:
- 1000 points = 1 🍋
- 1 🍋 = 15 Birr

For deposits (adding points to your account) or withdrawals (cashing out your points), please contact our admin team.

📧 Contact: @busi_admin

We accept various payment methods and will process your transactions promptly.

⚠️ Important:
- Always verify you're contacting the official admin (@busi_admin)
- Never share your password with anyone
- Transactions are typically processed within 24 hours
- Minimum withdrawal amount: 100 points (0.1 🍋)
    """
    await update.message.reply_text(help_text)

# Handle button callbacks
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    
    if query.data == "edit_profile":
        context.user_data['setting_up_profile'] = True
        context.user_data['profile_setup_step'] = 'gender'
        
        # Only show Male and Female options for profile editing
        keyboard = [
            [InlineKeyboardButton("♂️ Male", callback_data="gender_MALE")],
            [InlineKeyboardButton("♀️ Female", callback_data="gender_FEMALE")],
            [InlineKeyboardButton("Skip for now", callback_data="gender_skip")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "Let's update your profile!\n\nFirst, select your gender:",
            reply_markup=reply_markup
        )
        return
    
    if query.data.startswith("gender_"):
        if query.data == "gender_skip":
            # Keep existing gender if skipping
            pass
        else:
            # Extract gender name correctly
            gender_name = query.data.replace("gender_", "")
            gender = Gender[gender_name].value
            update_user_data(user_id, {'gender': gender})
        
        context.user_data['profile_setup_step'] = 'nickname'
        await query.edit_message_text(
            "Great! Now please send me your nickname:"
        )
        return
    
    if query.data.startswith("age_"):
        if query.data == "age_skip":
            # Keep existing age group if skipping
            pass
        else:
            # Extract age group name correctly
            age_group_name = query.data.replace("age_", "")
            age_group = AgeGroup[age_group_name].value
            update_user_data(user_id, {'age_group': age_group})
        
        # Profile setup complete
        update_user_data(user_id, {'profile_complete': True})
        context.user_data['setting_up_profile'] = False
        
        await query.edit_message_text(
            "✅ Your profile is now complete!\n\n"
            "You can now join the pool with /join and start finding partners with /find."
        )
        return
    
    # Handle gender preference selection
    if query.data.startswith("pref_gender_"):
        gender_pref = query.data.replace("pref_gender_", "")
        await handle_gender_preference(update, context, gender_pref)
        return
    
    # Handle age preference selection
    if query.data.startswith("pref_age_"):
        age_pref = query.data.replace("pref_age_", "")
        await handle_age_preference(update, context, age_pref)
        return

# Handle regular messages and media
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    
    # Check if we're in profile setup mode
    if context.user_data.get('setting_up_profile'):
        step = context.user_data.get('profile_setup_step')
        
        if step == 'nickname':
            nickname = update.message.text
            update_user_data(user_id, {'nickname': nickname})
            
            context.user_data['profile_setup_step'] = 'age'
            
            keyboard = [
                [InlineKeyboardButton(a.value, callback_data=f"age_{a.name}") for a in AgeGroup],
                [InlineKeyboardButton("Skip for now", callback_data="age_skip")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "Nice nickname! Now select your age group:",
                reply_markup=reply_markup
            )
        return
    
    # Check if user is in a conversation
    if user_data.get('in_conversation'):
        partner_id = user_data.get('conversation_partner')
        if not partner_id:
            await update.message.reply_text("Error: No conversation partner found.")
            return
        
        # Get role information
        is_initiator = user_data.get('is_initiator', False)
        partner_data = get_user_data(partner_id)
        
        # Handle text messages
        if update.message.text:
            message_text = update.message.text
            message_length = len(message_text)
            
            if is_initiator:
                # Initiator pays for their messages
                user_points = user_data.get('points', 0)
                
                # Check if initiator has enough points
                if user_points < message_length:
                    await update.message.reply_text(
                        f"❌ Not enough points to send this message. "
                        f"Please check your balance with /points"
                    )
                    return
                
                # Deduct points from initiator
                update_user_data(user_id, {'points': user_points - message_length})
                
                # Add points to partner (who was selected from pool)
                partner_points = partner_data.get('points', 0)
                update_user_data(partner_id, {'points': partner_points + message_length})
                
                # Forward message to partner (without caption)
                await context.bot.send_message(
                    chat_id=partner_id,
                    text=message_text  # Just the message text, no caption
                )
                
                # No success notification for sender
            else:
                # Partner (selected from pool) sends messages for FREE
                # No point deduction or earning for partner's messages
                
                # Forward message to initiator (without caption)
                await context.bot.send_message(
                    chat_id=partner_id,
                    text=message_text  # Just the message text, no caption
                )
        
        # Handle photos
        elif update.message.photo:
            if is_initiator:
                # Initiator pays for photos
                user_points = user_data.get('points', 0)
                photo_cost = 150
                
                # Check if initiator has enough points
                if user_points < photo_cost:
                    await update.message.reply_text(
                        f"❌ Not enough points to send this image. "
                        f"You need {photo_cost} points but have only {user_points}."
                    )
                    return
                
                # Deduct points from initiator
                update_user_data(user_id, {'points': user_points - photo_cost})
                
                # Add points to partner
                partner_points = partner_data.get('points', 0)
                update_user_data(partner_id, {'points': partner_points + photo_cost})
                
                # Forward photo to partner
                photo_file = await update.message.photo[-1].get_file()
                await context.bot.send_photo(
                    chat_id=partner_id,
                    photo=photo_file.file_id
                )
            else:
                # Partner sends photos for FREE
                photo_file = await update.message.photo[-1].get_file()
                await context.bot.send_photo(
                    chat_id=partner_id,
                    photo=photo_file.file_id
                )
        
        # Handle videos
        elif update.message.video:
            if is_initiator:
                # Initiator pays for videos
                user_points = user_data.get('points', 0)
                video_cost = 250
                
                # Check if initiator has enough points
                if user_points < video_cost:
                    await update.message.reply_text(
                        f"❌ Not enough points to send this video. "
                        f"You need {video_cost} points but have only {user_points}."
                    )
                    return
                
                # Deduct points from initiator
                update_user_data(user_id, {'points': user_points - video_cost})
                
                # Add points to partner
                partner_points = partner_data.get('points', 0)
                update_user_data(partner_id, {'points': partner_points + video_cost})
                
                # Forward video to partner
                video_file = await update.message.video.get_file()
                await context.bot.send_video(
                    chat_id=partner_id,
                    video=video_file.file_id
                )
            else:
                # Partner sends videos for FREE
                video_file = await update.message.video.get_file()
                await context.bot.send_video(
                    chat_id=partner_id,
                    video=video_file.file_id
                )
    
    else:
        await update.message.reply_text(
            "You're not in a conversation. Use /find to find a partner to chat with."
        )

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)

# Main function
def main():
    # Initialize database
    init_db()
    
    # Create application
    application = Application.builder().token(TOKEN).build()
    
    # Add handlers
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
    
    # Handle text messages
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Handle photo messages
    application.add_handler(MessageHandler(filters.PHOTO, handle_message))
    
    # Handle video messages
    application.add_handler(MessageHandler(filters.VIDEO, handle_message))
    
    application.add_error_handler(error_handler)
    
    # Start the bot
    print("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
