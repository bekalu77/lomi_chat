import logging
import os
import random
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# Import your database class
from database import Database

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Conversation states
SEX, NICKNAME, AGE_GROUP = range(3)
FIND_SEX, FIND_AGE = range(2)

# Initialize database
db = Database()

# Chat Manager
class ChatManager:
    def __init__(self):
        # {user_id: {'conversation_id': int, 'partner_id': int, 'char_count': int}}
        self.active_conversations = {}

    def get_chat_keyboard(self):
        keyboard = [
            ["❌ End Chat", "💰 My Points"],
            ["ℹ️ Help"]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    def get_main_keyboard(self):
        keyboard = [
            ["🔍 Find Partner", "💰 My Points"],
            ["✅ Join Pool", "📊 My Stats"],
            ["ℹ️ Help", "👤 Edit Profile"]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    def get_help_keyboard(self):
        keyboard = [
            ["🔍 Find Partner", "💰 My Points"],
            ["✅ Join Pool", "❌ Leave Pool"],
            ["📊 My Stats", "❌ End Chat"],
            ["👤 Edit Profile", "⚠️ Report User"]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    def get_sex_keyboard(self):
        keyboard = [["Male", "Female"], ["Any"]]
        return ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

    def get_age_keyboard(self):
        keyboard = [["18-24", "25-30"], ["31-45", "45+"], ["Any"]]
        return ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)

    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = """
🤖 *LomiTalk Help Guide*

💡 *How it works:*
- Complete your profile with sex, nickname, and age group
- Join the pool ✅ for FREE to earn points
- Find a partner 🔍 based on your preferences
- Chat with your partner 💬
- Partners earn 1 point per character from client's messages
- Clients pay 1 point per character for their messages

💰 *Payment System:*
- Clients: PAY 1 point per character sent
- Partners: EARN 1 point per character received
- No points created or destroyed - only transferred

⚡ *Commands:*
/start - Register and get 1000 free points
/profile - Edit your profile
/points - Check your points balance
/join - Join the earning pool (FREE)
/leave - Leave the pool
/find - Find a partner
/end - End current conversation
/help - Show this help menu
/report - Report a user for inappropriate behavior

🎯 *Tips:*
- Complete your profile to get better matches
- Stay in the pool to earn while you wait
- Be active to get found by others
- Longer conversations = more earnings!
        """
        await update.message.reply_text(help_text, parse_mode='Markdown', reply_markup=self.get_help_keyboard())

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message:
            return
        user = update.effective_user
        user_data = db.get_user(user.id)
        if not user_data:
            await update.message.reply_text("Please /start to register first.")
            return

        text = update.message.text
        # Ignore commands here, handled elsewhere
        if text and text.startswith('/'):
            return

        # Check if user is in an active conversation
        partner_id = user_data.get('partner_id')
        if not partner_id:
            await update.message.reply_text(
                "You're not in a conversation. Use 🔍 Find Partner to start one!",
                reply_markup=self.get_main_keyboard()
            )
            return

        # Get partner info
        partner_data = db.get_user(partner_id)
        if not partner_data:
            await update.message.reply_text("Partner not found. Ending conversation.")
            await self.end_conversation(update, context)
            return

        # Get conversation ID
        conv_id = self.active_conversations.get(user.id, {}).get('conversation_id')
        if not conv_id:
            conv_id = self.find_conversation(user.id, partner_id)
            if conv_id:
                self.start_conversation(user.id, partner_id, conv_id)
            else:
                await update.message.reply_text("Conversation not found. Ending.")
                await self.end_conversation(update, context)
                return

        message_text = update.message.text
        user_role = user_data.get('role')

        # Deduct points immediately if client
        if user_role == 'client':
            current_points = user_data.get('points', 0)
            message_len = len(message_text)
            if current_points >= message_len:
                db.update_user(user.id, {'points': current_points - message_len})
                # update total_chars
                total_chars = user_data.get('total_chars', 0)
                db.update_user(user.id, {'total_chars': total_chars + message_len})
            else:
                await update.message.reply_text("You don't have enough points to send this message.")
                return

        # Log message in DB
        try:
            db.log_message(conv_id, user_data['id'], message_text)
        except Exception as e:
            logger.error(f"Error logging message: {e}")

        # Forward to partner
        try:
            await context.bot.send_message(chat_id=partner_id, text=message_text)
        except Exception as e:
            logger.error(f"Error forwarding message: {e}")
            await update.message.reply_text("Failed to send message. Partner may have left.")
            await self.end_conversation(update, context)
            return

    async def end_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_data = db.get_user(user.id)

    if not user_data or not user_data.get('partner_id'):
        await update.message.reply_text("You're not in an active conversation.")
        return

    partner_id = user_data.get('partner_id')

    # Find the conversation
    conversation_id = self.find_conversation(user.id, partner_id)
    if not conversation_id:
        await update.message.reply_text("Conversation not found. Resetting your status.")
        db.update_user(user.id, {'status': 'inactive', 'partner_id': None, 'role': None})
        if partner_id:
            db.update_user(partner_id, {'partner_id': None, 'role': None})
        return

    # Get char_count from active conversations or default to 0
    char_count = 0
    if user.id in self.active_conversations:
        char_count = self.active_conversations[user.id]['char_count']

    logger.info(f"Ending conversation {conversation_id} with {char_count} characters")

    # Get partner data before ending conversation
    partner_data = db.get_user(partner_id) if partner_id else None

    # End the conversation in the database
    success = db.end_conversation(conversation_id, char_count)

    if success:
        # Update both users' partner_id to None
        db.update_user(user.id, {'partner_id': None, 'role': None})
        if partner_id:
            db.update_user(partner_id, {'partner_id': None, 'role': None})

        partner_nickname = partner_data.get('nickname', f"user{partner_id}") if partner_data else "Unknown"

        # Get updated user data after point transfer
        user_data = db.get_user(user.id)

        # Notify both users
        if user_data.get('role') == 'client' and partner_id:
            await update.message.reply_text(
                f"✅ Conversation with {partner_nickname} ended!\n"
                f"📝 Your characters sent: {char_count}\n"
                f"💰 Points transferred to partner: -{char_count}\n"
                f"💎 Remaining points: {user_data['points']}",
                reply_markup=self.get_main_keyboard()
            )

            # Notify partner
            try:
                partner_data = db.get_user(partner_id)
                if partner_data:
                    await context.bot.send_message(
                        chat_id=partner_id,
                        text=f"✅ Conversation with {user_data.get('nickname', 'Unknown')} ended!\n"
                             f"📝 Characters received: {char_count}\n"
                             f"💰 Points earned: +{char_count}\n"
                             f"💎 New balance: {partner_data['points']}",
                        reply_markup=self.get_main_keyboard()
                    )
            except Exception as e:
                logger.error(f"Could not notify partner: {e}")
        else:
            # Partner ended the conversation or no partner found
            await update.message.reply_text(
                f"✅ Conversation ended!\n"
                f"📝 Characters received: {char_count}\n"
                f"💰 Points earned: +{char_count}\n"
                f"💎 New balance: {user_data['points']}",
                reply_markup=self.get_main_keyboard()
            )
    else:
        await update.message.reply_text(
            "❌ Error ending conversation. Please try again or contact admin.",
            reply_markup=self.get_main_keyboard()
        )

    # Remove from active conversations
    if user.id in self.active_conversations:
        del self.active_conversations[user.id]
    if partner_id and partner_id in self.active_conversations:
        del self.active_conversations[partner_id]

    def find_conversation(self, user1_id, user2_id):
        # Search in DB for an active conversation
        try:
            cursor = db.conn.cursor()
            cursor.execute("""
                SELECT id FROM conversations WHERE 
                ((client_id = ? AND partner_id = ?) OR (client_id = ? AND partner_id = ?))
                AND ended_at IS NULL
            """, (user1_id, user2_id, user2_id, user1_id))
            result = cursor.fetchone()
            return result['id'] if result else None
        except Exception as e:
            logger.error(f"Error finding conversation: {e}")
            return None

    def start_conversation(self, user_id, partner_id, conversation_id):
        self.active_conversations[user_id] = {
            'conversation_id': conversation_id,
            'partner_id': partner_id,
            'char_count': 0
        }

# Match Pool
class MatchPool:
    def __init__(self, chat_manager):
        self.chat_manager = chat_manager

    async def join_pool(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_data = db.get_user(user.id)
        if not user_data:
            await update.message.reply_text("Please /start to register first.")
            return

        # Check if user is already in a conversation
        if user_data.get('partner_id'):
            await update.message.reply_text(
                "❌ You're already in a conversation! End it first before joining the pool.",
                reply_markup=self.chat_manager.get_main_keyboard()
            )
            return

        # Check if profile is complete
        if not user_data.get('sex') or not user_data.get('age_group'):
            await update.message.reply_text(
                "❌ Please complete your profile first using /profile before joining the pool.",
                reply_markup=self.chat_manager.get_main_keyboard()
            )
            return

        # Update user status to active in pool
        try:
            db.update_user(user.id, {'status': 'active'})
            await update.message.reply_text(
                "✅ You've joined the earning pool!\n"
                "📈 You'll earn 1 point per character when someone finds you!\n"
                "💰 No cost to stay in the pool - you only earn!\n\n"
                "Wait for someone to find you and start earning!",
                reply_markup=self.chat_manager.get_main_keyboard()
            )
        except Exception as e:
            logger.error(f"Error updating user status: {e}")
            await update.message.reply_text("Error joining the pool. Please try again.")
            
            
    async def start_find_partner(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_data = db.get_user(user.id)
        if not user_data:
            await update.message.reply_text("Please /start to register first.")
            return FIND_SEX
        if user_data.get('partner_id'):
            await update.message.reply_text(
                "❌ You're already in a conversation! End it first before finding a new partner.",
                reply_markup=self.chat_manager.get_chat_keyboard()
            )
            return ConversationHandler.END
        await update.message.reply_text(
            "👥 What gender are you looking for?",
            reply_markup=self.chat_manager.get_sex_keyboard()
        )
        return FIND_SEX

    async def find_partner_sex(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        sex_pref = update.message.text
        context.user_data['find_sex'] = sex_pref
        await update.message.reply_text(
            "🎂 What age group are you looking for?",
            reply_markup=self.chat_manager.get_age_keyboard()
        )
        return FIND_AGE

    async def find_partner_age(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        age_pref = update.message.text
        sex_pref = context.user_data.get('find_sex', 'Any')
        context.user_data.pop('find_sex', None)

        user = update.effective_user
        user_data = db.get_user(user.id)

        # Find available partners
        available_partners = db.find_available_partners(
            exclude_telegram_id=user.id,
            sex=sex_pref,
            age_group=age_pref
        )

        if not available_partners:
            await update.message.reply_text(
                "😔 No partners found with your preferences.\n\n"
                "Would you like to:\n"
                "1. Try again with different preferences\n"
                "2. Search without preferences\n"
                "3. Join the pool to be found by others",
                reply_markup=ReplyKeyboardMarkup([
                    ["Try Again", "Search Any"],
                    ["Join Pool"]
                ], resize_keyboard=True)
            )
            return ConversationHandler.END

        partner = random.choice(available_partners)
        partner_id = partner['telegram_id']
        partner_nick = partner.get('nickname', f"user{partner_id}")

        # Start conversation
        conv_id = db.start_conversation(user.id, partner_id)
        if not conv_id:
            await update.message.reply_text("Error starting conversation. Please try again.")
            return ConversationHandler.END

        # Set roles
        db.update_user(user.id, {'role': 'client', 'partner_id': partner_id, 'status': 'inactive'})
        db.update_user(partner_id, {'role': 'partner', 'partner_id': user.id, 'status': 'inactive'})

        # Track conversation
        self.start_conversation(user.id, partner_id, conv_id)

        # Notify users
        await update.message.reply_text(
            f"🎉 You found {partner_nick}!\n\nStart chatting! Type your messages below.",
            reply_markup=self.get_chat_keyboard()
        )
        try:
            await context.bot.send_message(
                chat_id=partner_id,
                text=f"🎉 {user_data.get('nickname', 'User')} found you!\n\nStart chatting!",
                reply_markup=self.get_chat_keyboard()
            )
        except:
            pass
        return ConversationHandler.END

    async def cancel_find(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Search cancelled.", reply_markup=self.chat_manager.get_main_keyboard())
        return ConversationHandler.END

# User Profile Management
class UserManager:
    def __init__(self, chat_manager):
        self.chat_manager = chat_manager
    
    async def start_profile_setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        existing = db.get_user(user.id)
        if existing:
            await update.message.reply_text(
                "You are already registered. Use /profile to update your profile.",
                reply_markup=self.chat_manager.get_main_keyboard()
            )
            return
        await update.message.reply_text(
            "Let's set up your profile!\n\nWhat is your sex?",
            reply_markup=ReplyKeyboardMarkup([["Male", "Female"]], one_time_keyboard=True, resize_keyboard=True)
        )
        return SEX

    async def set_sex(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        sex = update.message.text.lower()
        if sex not in ['male', 'female']:
            await update.message.reply_text("Please select 'Male' or 'Female'.")
            return SEX
        context.user_data['profile_sex'] = sex
        await update.message.reply_text(
            "What nickname would you like to use?",
            reply_markup=ReplyKeyboardRemove()
        )
        return NICKNAME

    async def set_nickname(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        nickname = update.message.text.strip()
        if not nickname or len(nickname) > 20:
            await update.message.reply_text("Please enter a nickname (1-20 characters).")
            return NICKNAME
        # Check if nickname exists
        existing = self.get_user_by_nickname(nickname)
        if existing and existing['telegram_id'] != update.effective_user.id:
            await update.message.reply_text("Nickname taken. Choose another.")
            return NICKNAME
        context.user_data['profile_nickname'] = nickname
        await update.message.reply_text(
            "What is your age group?",
            reply_markup=ReplyKeyboardMarkup([
                ["18-24", "25-30"],
                ["31-45", "45+"]
            ], one_time_keyboard=True, resize_keyboard=True)
        )
        return AGE_GROUP

    async def set_age_group(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        age_group = update.message.text
        if age_group not in ['18-24', '25-30', '31-45', '45+']:
            await update.message.reply_text("Please select a valid age group.")
            return AGE_GROUP
        sex = context.user_data.get('profile_sex')
        nickname = context.user_data.get('profile_nickname')
        user_id = update.effective_user.id
        db.update_user(user_id, {
            'sex': sex,
            'nickname': nickname,
            'age_group': age_group,
            'points': 1000,  # starting points
            'total_chars': 0,
            'status': 'inactive'
        })
        await update.message.reply_text(
            "✅ Your profile has been updated!\n\n"
            f"👤 Nickname: {nickname}\n"
            f"👥 Sex: {sex}\n"
            f"🎂 Age Group: {age_group}",
            reply_markup=self.chat_manager.get_main_keyboard()
        )
        return

    def get_user_by_nickname(self, nickname):
        try:
            res = db.conn.execute("SELECT * FROM users WHERE nickname = ?", (nickname,))
            row = res.fetchone()
            return dict(row) if row else None
        except:
            return None

    async def show_points(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        user = db.get_user(user_id)
        if user:
            await update.message.reply_text(
                f"💰 Your Points: {user['points']}\n"
                f"📝 Total Characters: {user['total_chars']}"
            )
        else:
            await update.message.reply_text("Please /start to register.")

    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        user = db.get_user(user_id)
        if user:
            await update.message.reply_text(
                f"📊 Your Stats:\n"
                f"👤 Nickname: {user['nickname']}\n"
                f"👥 Sex: {user['sex']}\n"
                f"🎂 Age Group: {user['age_group']}\n"
                f"💰 Points: {user['points']}\n"
                f"💬 Conversations: {user.get('conversation_count', 0)}\n"
                f"📝 Total Characters: {user['total_chars']}"
            )
        else:
            await update.message.reply_text("Please /start to register.")

    async def handle_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        user = db.get_user(user_id)
        if not user:
            await update.message.reply_text("Please /start to register first.")
            return
        partner_id = user.get('partner_id')
        if not partner_id:
            await update.message.reply_text("You can only report users you're currently chatting with.")
            return
        partner = db.get_user(partner_id)
        if not partner:
            await update.message.reply_text("Partner not found.")
            return
        await update.message.reply_text("Please describe the reason for your report:", reply_markup=ReplyKeyboardRemove())
        context.user_data['report_user_id'] = partner_id
        return 1

    async def complete_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        reason = update.message.text
        reported_user_id = self.context.user_data.get('report_user_id')
        if not reported_user_id:
            await update.message.reply_text("Report session expired.")
            return
        db.create_report(update.effective_user.id, reported_user_id, reason)
        await update.message.reply_text("Thank you for your report.")
        return
    async def debug_db(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_data = db.get_user(user.id)
    
    if not user_data:
        await update.message.reply_text("Please /start to register first.")
        return
    
    # Get all active conversations
    try:
        cursor = db.conn.cursor()
        cursor.execute("SELECT * FROM conversations WHERE ended_at IS NULL")
        active_convos = cursor.fetchall()
        
        # Get user's partner info if any
        partner_info = "None"
        if user_data.get('partner_id'):
            partner_data = db.get_user(user_data['partner_id'])
            partner_info = f"{partner_data.get('nickname')} (ID: {partner_data['telegram_id']})" if partner_data else "Unknown"
        
        debug_info = f"""
    🔧 DEBUG INFO:

    👤 Your Data:
    - ID: {user_data['id']}
    - Telegram ID: {user_data['telegram_id']}
    - Nickname: {user_data.get('nickname', 'Not set')}
    - Points: {user_data['points']}
    - Role: {user_data.get('role', 'Not set')}
    - Partner: {partner_info}
    - Status: {user_data['status']}

    💬 Active Conversations: {len(active_convos)}
    """
        await update.message.reply_text(debug_info)
        
    except Exception as e:
        logger.error(f"Debug error: {e}")
        await update.message.reply_text(f"Error getting debug info: {e}")
# Main setup
chat_manager = ChatManager()
match_pool = MatchPool(chat_manager)
user_manager = UserManager(chat_manager)

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await user_manager.start_profile_setup(update, context)

async def points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await user_manager.show_points(update, context)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await user_manager.show_stats(update, context)

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await match_pool.join_pool(update, context)

async def leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.update_user(user.id, {'status': 'inactive', 'partner_id': None})
    await update.message.reply_text("✅ You've left the pool.", reply_markup=chat_manager.get_main_keyboard())

async def find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await match_pool.start_find_partner(update, context)

async def end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await chat_manager.end_conversation(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await chat_manager.show_help(update, context)

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await user_manager.handle_report(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await chat_manager.handle_message(update, context)

async def error(update, context):
    logger.error(f"Update {update} caused error {context.error}")

def main():
    app = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("points", points))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("leave", leave))
    app.add_handler(CommandHandler("find", find))
    app.add_handler(CommandHandler("end", end))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("debug", user_manager.debug_db))
    # Profile setup conversation
    from telegram.ext import ConversationHandler
    profile_conv = ConversationHandler(
        entry_points=[CommandHandler('profile', user_manager.start_profile_setup)],
        states={
            SEX: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_manager.set_sex)],
            NICKNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_manager.set_nickname)],
            AGE_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_manager.set_age_group)],
        },
        fallbacks=[CommandHandler('cancel', lambda u, c: None)]
    )
    app.add_handler(profile_conv)

    # Find partner conversation
    find_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex('^🔍 Find Partner$'), match_pool.start_find_partner)],
        states={
            FIND_SEX: [MessageHandler(filters.TEXT & ~filters.COMMAND, match_pool.find_partner_sex)],
            FIND_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, match_pool.find_partner_age)],
        },
        fallbacks=[CommandHandler('cancel', lambda u, c: None)]
    )
    app.add_handler(find_conv)

    # Message handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Error
    app.add_error_handler(error)

    # Run
    app.run_polling()

if __name__ == '__main__':
    main()
